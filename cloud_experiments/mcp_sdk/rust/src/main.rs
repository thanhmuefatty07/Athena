#![forbid(unsafe_code)]

use std::{
    borrow::Cow,
    env,
    error::Error,
    path::PathBuf,
    process::Stdio,
    sync::{
        Arc, OnceLock,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use axum::{
    Router,
    body::{Body, to_bytes},
    extract::Request,
    http::{HeaderValue, StatusCode, header::AUTHORIZATION},
    middleware::{self, Next},
    response::{IntoResponse, Response},
};
use rmcp::{
    ErrorData as McpError, RoleServer, ServerHandler, ServiceExt,
    handler::server::wrapper::Parameters,
    model::{
        CallToolResult, ContentBlock, Implementation, ProgressNotificationParam, ProtocolVersion,
        ServerCapabilities, ServerConfig,
    },
    schemars,
    service::RequestContext,
    tool, tool_handler, tool_router,
    transport::{
        stdio,
        streamable_http_server::{
            StreamableHttpServerConfig, StreamableHttpService, session::local::LocalSessionManager,
        },
    },
};
use serde::Deserialize;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    process::Command,
};

const DEFAULT_MAX_INFLIGHT_PROBES: usize = 2;
const MAX_ALLOWED_INFLIGHT_PROBES: usize = 8;

struct InflightLimiter {
    active: AtomicUsize,
    max_inflight: usize,
}

struct ProbePermit {
    limiter: Arc<InflightLimiter>,
}

impl InflightLimiter {
    fn new(max_inflight: usize) -> Self {
        Self {
            active: AtomicUsize::new(0),
            max_inflight,
        }
    }

    fn try_acquire(self: &Arc<Self>) -> Option<ProbePermit> {
        self.active
            .try_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                (current < self.max_inflight).then_some(current + 1)
            })
            .ok()
            .map(|_| ProbePermit {
                limiter: Arc::clone(self),
            })
    }
}

impl Drop for ProbePermit {
    fn drop(&mut self) {
        self.limiter.active.fetch_sub(1, Ordering::AcqRel);
    }
}

fn parse_max_inflight_probes() -> Result<usize, Box<dyn Error>> {
    match env::var("ATHENA_MCP_MAX_INFLIGHT") {
        Ok(raw) => {
            let parsed = raw
                .parse::<usize>()
                .map_err(|_| "invalid ATHENA_MCP_MAX_INFLIGHT")?;
            if !(1..=MAX_ALLOWED_INFLIGHT_PROBES).contains(&parsed) {
                return Err("ATHENA_MCP_MAX_INFLIGHT out of range 1..=8".into());
            }
            Ok(parsed)
        }
        Err(env::VarError::NotPresent) => Ok(DEFAULT_MAX_INFLIGHT_PROBES),
        Err(_) => Err("invalid ATHENA_MCP_MAX_INFLIGHT".into()),
    }
}

#[derive(Clone)]
struct FixtureServer {
    limiter: Arc<InflightLimiter>,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "lowercase")]
enum FixtureCase {
    Long,
    Short,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
struct FixtureArgs {
    fixture_case: FixtureCase,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct TaskArgs {
    task_id: String,
    source_id: String,
    revision: String,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct ExpandArgs {
    task_id: String,
    source_id: String,
    revision: String,
    handle: String,
}

#[derive(Debug, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SearchArgs {
    task_id: String,
    query: String,
}

#[tool_router]
impl FixtureServer {
    fn new(limiter: Arc<InflightLimiter>) -> Self {
        Self { limiter }
    }

    #[tool(
        description = "Run a bounded synthetic document through Athena's Rust core and return AAE output."
    )]
    async fn athena_fixture(
        &self,
        Parameters(args): Parameters<FixtureArgs>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        let Some(permit) = self.limiter.try_acquire() else {
            return Ok(CallToolResult::error(vec![ContentBlock::text(
                "CORE_PROBE_ADMISSION_SATURATED".to_string(),
            )]));
        };
        let fixture_case = match args.fixture_case {
            FixtureCase::Long => "long",
            FixtureCase::Short => "short",
        };
        let result = async {
            notify_core_started(&context).await?;
            run_core_probe(fixture_case, &context, permit).await
        }
        .await;
        Ok(match result {
            Ok(payload) => CallToolResult::success(vec![ContentBlock::text(payload)]),
            Err(error) => {
                CallToolResult::error(vec![ContentBlock::text(error.message.to_string())])
            }
        })
    }

    #[tool(
        description = "Search frozen research task candidates through Athena's Rust core with supervisor-bound tenant, task and query checks. No live network access."
    )]
    async fn athena_search_task(
        &self,
        Parameters(args): Parameters<SearchArgs>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        let Some(permit) = self.limiter.try_acquire() else {
            return Ok(CallToolResult::error(vec![ContentBlock::text(
                "CORE_PROBE_ADMISSION_SATURATED".to_string(),
            )]));
        };
        let result = async {
            let tenant = env::var("ATHENA_L03_TENANT")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            let bound_task = env::var("ATHENA_L03_BOUND_TASK")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            notify_core_started(&context).await?;
            let input = serde_json::to_vec(&serde_json::json!({
                "schema":"athena-task-search/1", "principal_tenant":tenant,
                "bound_task_id":bound_task, "task_id":args.task_id,
                "query":args.query
            }))
            .map_err(|_| McpError::internal_error("SCOPED_TASK_REQUEST_INVALID", None))?;
            let payload = run_core_probe_input("task-search", input, &context, permit).await?;
            if serde_json::from_str::<serde_json::Value>(&payload)
                .is_ok_and(|value| value["schema"] == "athena-task-denial/1")
            {
                return Err(McpError::internal_error("SCOPED_TASK_DENIED", None));
            }
            Ok(payload)
        }
        .await;
        Ok(match result {
            Ok(payload) => CallToolResult::success(vec![ContentBlock::text(payload)]),
            Err(error) => {
                CallToolResult::error(vec![ContentBlock::text(error.message.to_string())])
            }
        })
    }

    #[tool(
        description = "Extract a frozen research task document with supervisor-bound tenant, task and revision checks. No live network access."
    )]
    async fn athena_extract_document(
        &self,
        Parameters(args): Parameters<TaskArgs>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        let Some(permit) = self.limiter.try_acquire() else {
            return Ok(CallToolResult::error(vec![ContentBlock::text(
                "CORE_PROBE_ADMISSION_SATURATED".to_string(),
            )]));
        };
        let result = async {
            let tenant = env::var("ATHENA_L03_TENANT")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            let bound_task = env::var("ATHENA_L03_BOUND_TASK")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            notify_core_started(&context).await?;
            let input = serde_json::to_vec(&serde_json::json!({
                "schema":"athena-task-extract/1", "principal_tenant":tenant,
                "bound_task_id":bound_task, "task_id":args.task_id,
                "source_id":args.source_id, "expected_revision":args.revision
            }))
            .map_err(|_| McpError::internal_error("SCOPED_TASK_REQUEST_INVALID", None))?;
            let payload = run_core_probe_input("task-export", input, &context, permit).await?;
            if serde_json::from_str::<serde_json::Value>(&payload)
                .is_ok_and(|value| value["schema"] == "athena-task-denial/1")
            {
                return Err(McpError::internal_error("SCOPED_TASK_DENIED", None));
            }
            Ok(payload)
        }
        .await;
        Ok(match result {
            Ok(payload) => CallToolResult::success(vec![ContentBlock::text(payload)]),
            Err(error) => {
                CallToolResult::error(vec![ContentBlock::text(error.message.to_string())])
            }
        })
    }

    #[tool(
        description = "Expand a compact evidence handle through Athena's Rust core with supervisor-bound tenant, task, revision and handle checks."
    )]
    async fn athena_expand_handle(
        &self,
        Parameters(args): Parameters<ExpandArgs>,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        let Some(permit) = self.limiter.try_acquire() else {
            return Ok(CallToolResult::error(vec![ContentBlock::text(
                "CORE_PROBE_ADMISSION_SATURATED".to_string(),
            )]));
        };
        let result = async {
            let tenant = env::var("ATHENA_L03_TENANT")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            let bound_task = env::var("ATHENA_L03_BOUND_TASK")
                .ok()
                .filter(|value| !value.is_empty())
                .ok_or_else(|| McpError::internal_error("SCOPED_TASK_CONTEXT_MISSING", None))?;
            notify_core_started(&context).await?;
            let input = serde_json::to_vec(&serde_json::json!({
                "schema":"athena-task-expand/1", "principal_tenant":tenant,
                "bound_task_id":bound_task, "task_id":args.task_id,
                "source_id":args.source_id, "expected_revision":args.revision,
                "handle":args.handle
            }))
            .map_err(|_| McpError::internal_error("SCOPED_TASK_REQUEST_INVALID", None))?;
            let payload = run_core_probe_input("task-expand", input, &context, permit).await?;
            if serde_json::from_str::<serde_json::Value>(&payload)
                .is_ok_and(|value| value["schema"] == "athena-task-denial/1")
            {
                return Err(McpError::internal_error("SCOPED_TASK_DENIED", None));
            }
            Ok(payload)
        }
        .await;
        Ok(match result {
            Ok(payload) => CallToolResult::success(vec![ContentBlock::text(payload)]),
            Err(error) => {
                CallToolResult::error(vec![ContentBlock::text(error.message.to_string())])
            }
        })
    }
}

#[tool_handler]
impl ServerHandler for FixtureServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("athena-mcp-fixture", "0.1.0"))
            .with_protocol_version(ProtocolVersion::V_2026_07_28)
    }

    fn supported_protocol_versions(&self) -> Cow<'static, [ProtocolVersion]> {
        Cow::Borrowed(&[ProtocolVersion::V_2026_07_28])
    }
}

fn core_fixture_input(fixture_case: &str) -> Result<Vec<u8>, McpError> {
    static FIXTURES: OnceLock<serde_json::Value> = OnceLock::new();
    let fixtures = FIXTURES.get_or_init(|| {
        serde_json::from_str(include_str!("../../core_fixtures.json"))
            .expect("pinned core fixtures must be valid JSON")
    });
    let input = fixtures
        .get(fixture_case)
        .and_then(|fixture| fixture.get("input"))
        .ok_or_else(|| McpError::invalid_params("unknown fixture case", None))?;
    serde_json::to_vec(input)
        .map_err(|_| McpError::internal_error("core fixture serialization failed", None))
}

async fn notify_core_started(context: &RequestContext<RoleServer>) -> Result<(), McpError> {
    if let Some(progress_token) = context.meta.get_progress_token() {
        context
            .peer
            .notify_progress(
                ProgressNotificationParam::new(progress_token, 0.0)
                    .with_message("Core operation started"),
            )
            .await
            .map_err(|_| McpError::internal_error("MCP_PROGRESS_SEND_FAILED", None))?;
    }
    Ok(())
}

async fn run_core_probe(
    fixture_case: &str,
    context: &RequestContext<RoleServer>,
    permit: ProbePermit,
) -> Result<String, McpError> {
    run_core_probe_input("export", core_fixture_input(fixture_case)?, context, permit).await
}

async fn run_core_probe_input(
    stage: &'static str,
    input: Vec<u8>,
    context: &RequestContext<RoleServer>,
    _permit: ProbePermit,
) -> Result<String, McpError> {
    const MAX_INPUT_BYTES: usize = 131_072;
    const MAX_OUTPUT_BYTES: usize = 131_072;
    let executable = env::var_os("ATHENA_CORE_PROBE_BIN")
        .map(PathBuf::from)
        .filter(|path| path.is_file())
        .ok_or_else(|| McpError::internal_error("core probe executable is unavailable", None))?;
    if input.len() > MAX_INPUT_BYTES {
        return Err(McpError::internal_error(
            "core fixture exceeds input limit",
            None,
        ));
    }
    let mut command = Command::new(executable);
    command
        .args([stage, "-", "0"])
        .env_clear()
        .env("RUST_LOG", "off")
        .kill_on_drop(true)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    for key in [
        "PATH",
        "SystemRoot",
        "WINDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
    ] {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
    let mut child = command
        .spawn()
        .map_err(|_| McpError::internal_error("core probe could not start", None))?;
    let result = tokio::select! {
        result = tokio::time::timeout(Duration::from_secs(5), async {
            let mut stdin = child
                .stdin
                .take()
                .ok_or_else(|| McpError::internal_error("CORE_PROBE_INPUT_FAILED", None))?;
            let mut stdout = child
                .stdout
                .take()
                .ok_or_else(|| McpError::internal_error("CORE_PROBE_OUTPUT_FAILED", None))?;
            let ((), output) = tokio::try_join!(
                async move {
                    stdin
                        .write_all(&input)
                        .await
                        .map_err(|_| McpError::internal_error("CORE_PROBE_INPUT_FAILED", None))?;
                    drop(stdin);
                    Ok::<(), McpError>(())
                },
                async move {
                    let mut output = Vec::new();
                    let mut chunk = [0_u8; 8192];
                    loop {
                        let remaining = (MAX_OUTPUT_BYTES - output.len() + 1).min(chunk.len());
                        let count = stdout
                            .read(&mut chunk[..remaining])
                            .await
                            .map_err(|_| McpError::internal_error("CORE_PROBE_OUTPUT_FAILED", None))?;
                        if count == 0 {
                            break;
                        }
                        if count > MAX_OUTPUT_BYTES - output.len() {
                            return Err(McpError::internal_error(
                                "CORE_PROBE_OUTPUT_TOO_LARGE",
                                None,
                            ));
                        }
                        output.extend_from_slice(&chunk[..count]);
                    }
                    Ok::<Vec<u8>, McpError>(output)
                }
            )?;
            let status = child
                .wait()
                .await
                .map_err(|_| McpError::internal_error("CORE_PROBE_FAILED", None))?;
            if !status.success() {
                return Err(McpError::internal_error("CORE_PROBE_FAILED", None));
            }
            Ok(output)
        }) => result.unwrap_or_else(|_| Err(McpError::internal_error("CORE_PROBE_TIMEOUT", None))),
        _ = context.ct.cancelled() => Err(McpError::internal_error("CORE_PROBE_CANCELLED", None)),
    };
    if result.is_err() {
        if child
            .try_wait()
            .map_err(|_| McpError::internal_error("CORE_PROBE_CLEANUP_FAILED", None))?
            .is_none()
        {
            let _ = child.kill().await;
        }
        child
            .wait()
            .await
            .map_err(|_| McpError::internal_error("CORE_PROBE_CLEANUP_FAILED", None))?;
    }
    let text = String::from_utf8(result?)
        .map_err(|_| McpError::internal_error("CORE_PROBE_OUTPUT_INVALID_UTF8", None))?;
    if !text.ends_with('\n') {
        return Err(McpError::internal_error("CORE_PROBE_OUTPUT_INVALID", None));
    }
    Ok(text)
}

async fn authorize(request: Request<Body>, next: Next) -> Response {
    let expected = match env::var("ATHENA_L03_AUTH_TOKEN") {
        Ok(value) if !value.is_empty() => format!("Bearer {value}"),
        _ => return StatusCode::SERVICE_UNAVAILABLE.into_response(),
    };
    let supplied = request
        .headers()
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if !constant_time_eq(supplied.as_bytes(), expected.as_bytes()) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    next.run(request).await
}

async fn require_modern_protocol(request: Request<Body>, next: Next) -> Response {
    let request_version = request
        .headers()
        .get("mcp-protocol-version")
        .and_then(|value| value.to_str().ok());
    if request_version == Some("2026-07-28") {
        return next.run(request).await;
    }

    let requested_version = request_version.map(str::to_owned);
    let (_, body) = request.into_parts();
    let body = match to_bytes(body, 65_536).await {
        Ok(body) => body,
        Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
    };
    let request_id = serde_json::from_slice::<serde_json::Value>(&body)
        .ok()
        .and_then(|value| value.get("id").cloned())
        .unwrap_or(serde_json::Value::Null);
    let body = serde_json::to_vec(&serde_json::json!({
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {
                "requested": requested_version,
                "supported": ["2026-07-28"]
            }
        }
    }))
    .unwrap_or_else(|_| {
        br#"{"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Internal error"}}"#
            .to_vec()
    });
    let mut response = StatusCode::BAD_REQUEST.into_response();
    response
        .headers_mut()
        .insert("content-type", HeaderValue::from_static("application/json"));
    *response.body_mut() = Body::from(body);
    response
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

async fn run_http(port: u16, limiter: Arc<InflightLimiter>) -> Result<(), Box<dyn Error>> {
    let origin = format!("http://127.0.0.1:{port}");
    let config = StreamableHttpServerConfig::default()
        .with_legacy_session_mode(false)
        .with_stateless_protocol_metadata_required(true)
        .with_json_response(true)
        .with_sse_keep_alive(None)
        .with_max_request_body_bytes(65_536)
        .with_allowed_origins([origin]);
    let service = StreamableHttpService::new(
        move || Ok(FixtureServer::new(Arc::clone(&limiter))),
        LocalSessionManager::default().into(),
        config,
    );
    let app = Router::new()
        .nest_service("/mcp", service)
        .layer(middleware::from_fn(require_modern_protocol))
        .layer(middleware::from_fn(authorize));
    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    eprintln!("MCP_READY {port}");
    axum::serve(listener, app).await?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let limiter = Arc::new(InflightLimiter::new(parse_max_inflight_probes()?));
    let mut args = env::args().skip(1);
    match args.next().as_deref() {
        Some("stdio") => {
            if env::var("ATHENA_L03_AUTH_TOKEN").is_err() {
                return Err(
                    "stdio fixture requires supervisor-scoped ATHENA_L03_AUTH_TOKEN".into(),
                );
            }
            let service = FixtureServer::new(limiter).serve(stdio()).await?;
            service.waiting().await?;
            Ok(())
        }
        Some("http") => {
            let port = args.next().ok_or("http requires a port")?.parse::<u16>()?;
            if args.next().is_some() {
                return Err("unexpected argument".into());
            }
            run_http(port, limiter).await
        }
        _ => Err("usage: athena-mcp-rust-fixture stdio | http <port>".into()),
    }
}
