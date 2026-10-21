#![forbid(unsafe_code)]
//! Research-only HTTP/1.1 loopback TLS adapter using the locked reqwest and core
//! SingleFlight. This executable does not weaken or replace production networking.

use athena_core_v2::{
    aae,
    network::{FetchError, FetchPolicy, fetch_https},
    orchestrator::{Engine, PipelineError, RequestKey},
    projection::{self, Projection},
    singleflight::{FlightError, SingleFlight},
};
use futures_util::StreamExt;
use reqwest::{Certificate, Client, header, tls::TlsInfo};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashSet, VecDeque},
    fs,
    net::SocketAddr,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore},
    time::Instant,
};
use url::Url;

const PROTOCOL: &str = "athena-core-research-service/1";
const LAB_HOST: &str = "athena-lab.invalid";
const MAX_INPUT_BYTES: u64 = 1024 * 1024;

fn default_variant() -> String {
    "policy_pool".into()
}
fn default_queue() -> String {
    "reject".into()
}
fn default_policy() -> String {
    "lab-policy".into()
}
fn default_privacy() -> String {
    "anonymous".into()
}
fn default_cache() -> String {
    "bypass".into()
}
fn default_epoch() -> u64 {
    1
}
fn yes() -> bool {
    true
}
fn pending_limit() -> usize {
    32
}
fn origin_limit() -> usize {
    4
}
fn entry_limit() -> usize {
    4
}
fn body_limit() -> usize {
    4096
}
fn deadline() -> u64 {
    1000
}
fn lifetime() -> u64 {
    2000
}
fn freshness() -> u64 {
    100
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Lab {
    scope: String,
    host: String,
    address: String,
    ca_pem_path: String,
    peer_der_path: String,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Arrival {
    task_id: String,
    at_us: u64,
    source: String,
    tenant: String,
    #[serde(default)]
    query: String,
    #[serde(default = "default_epoch")]
    epoch: u64,
    #[serde(default = "default_policy")]
    policy_id: String,
    #[serde(default = "default_privacy")]
    privacy: String,
    #[serde(default = "default_cache")]
    cache_mode: String,
    #[serde(default)]
    max_body_bytes: Option<usize>,
    #[serde(default)]
    cancel_after_us: Option<u64>,
    #[serde(default)]
    revoke_before: bool,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Revocation {
    at_us: u64,
    tenant: String,
    epoch: u64,
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    protocol: String,
    mode: String,
    #[serde(default)]
    lab: Option<Lab>,
    #[serde(default = "default_variant")]
    variant: String,
    #[serde(default = "default_queue")]
    queue_mode: String,
    #[serde(default)]
    arrivals: Vec<Arrival>,
    #[serde(default)]
    revocations: Vec<Revocation>,
    #[serde(default = "pending_limit")]
    max_pending: usize,
    #[serde(default = "origin_limit")]
    max_origin_inflight: usize,
    #[serde(default = "entry_limit")]
    max_pool_entries: usize,
    #[serde(default = "entry_limit")]
    max_cache_entries: usize,
    #[serde(default = "body_limit")]
    max_body_bytes: usize,
    #[serde(default = "deadline")]
    deadline_ms: u64,
    #[serde(default = "lifetime")]
    pool_lifetime_ms: u64,
    #[serde(default = "freshness")]
    fresh_for_ms: u64,
    #[serde(default = "yes")]
    source_singleflight: bool,
    #[serde(default = "yes")]
    representation_singleflight: bool,
}

fn token(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 48
        && value
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}

fn validate(input: &Input) -> Result<(), String> {
    if input.protocol != PROTOCOL
        || !matches!(
            input.mode.as_str(),
            "load" | "journey" | "cancel_leader" | "cancel_follower" | "contracts"
        )
    {
        return Err("unknown protocol or mode".into());
    }
    if input.mode == "contracts" {
        return Ok(());
    }
    let lab = input
        .lab
        .as_ref()
        .ok_or("load requires an explicit local lab")?;
    let address: SocketAddr = lab.address.parse().map_err(|_| "invalid lab address")?;
    if lab.scope != "synthetic-loopback-tls-v1"
        || lab.host != LAB_HOST
        || !address.ip().is_loopback()
        || !address.is_ipv4()
        || address.port() == 0
    {
        return Err("research transport only permits the named synthetic IPv4 loopback lab".into());
    }
    if !matches!(input.variant.as_str(), "client_per_fetch" | "policy_pool")
        || !matches!(input.queue_mode.as_str(), "reject" | "bounded_wait")
        || input.arrivals.is_empty()
        || input.arrivals.len() > 256
        || input.max_pending > 256
        || !(1..=16).contains(&input.max_origin_inflight)
        || !(1..=16).contains(&input.max_pool_entries)
        || !(1..=16).contains(&input.max_cache_entries)
        || !(1..=16384).contains(&input.max_body_bytes)
        || !(1..=10000).contains(&input.deadline_ms)
        || !(1..=30000).contains(&input.pool_lifetime_ms)
        || input.fresh_for_ms > 10000
        || input.revocations.len() > 32
    {
        return Err("invalid or excessive workload/resource bounds".into());
    }
    let mut ids = HashSet::new();
    for request in &input.arrivals {
        if !token(&request.task_id)
            || !ids.insert(&request.task_id)
            || !token(&request.source)
            || !token(&request.tenant)
            || !token(&request.policy_id)
            || !token(&request.privacy)
            || request.query.len() > 256
            || request.at_us > 30_000_000
            || request.cancel_after_us.is_some_and(|n| n > 10_000_000)
            || request
                .max_body_bytes
                .is_some_and(|n| !(1..=16384).contains(&n))
            || !matches!(
                request.cache_mode.as_str(),
                "bypass" | "revalidate" | "fresh"
            )
        {
            return Err("invalid request identity, freshness mode or workload bound".into());
        }
    }
    if input
        .revocations
        .iter()
        .any(|r| r.at_us > 30_000_000 || !token(&r.tenant))
    {
        return Err("invalid revocation timeline".into());
    }
    Ok(())
}

fn read_small(path: impl AsRef<Path>, max: u64) -> Result<Vec<u8>, String> {
    let path = path.as_ref();
    let meta = fs::symlink_metadata(path).map_err(|_| "input is unavailable")?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > max {
        return Err("input is not a bounded regular file".into());
    }
    let bytes = fs::read(path).map_err(|_| "input read failed")?;
    if bytes.len() as u64 > max {
        return Err("input grew beyond its cap".into());
    }
    Ok(bytes)
}

#[derive(Clone, Hash, PartialEq, Eq)]
struct Identity {
    tenant: String,
    privacy: String,
    policy_id: String,
    epoch: u64,
    max_body_bytes: usize,
    deadline_ms: u64,
    address: SocketAddr,
    certificate: String,
}

#[derive(Clone, Hash, PartialEq, Eq)]
struct ContentKey {
    identity: Identity,
    source: String,
}
#[derive(Clone, Hash, PartialEq, Eq)]
struct FlightKey {
    content: ContentKey,
    cache_mode: String,
}
#[derive(Clone, Hash, PartialEq, Eq)]
struct RepresentationKey {
    source: FlightKey,
    query: String,
}

#[derive(Clone)]
struct Content {
    body: String,
    page: Arc<Projection>,
    etag: String,
    status: u16,
    origin_id: Option<usize>,
    peer_verified: bool,
    cache_state: String,
}
#[derive(Clone)]
struct Representation {
    content: Content,
    text: String,
}
struct StoredBody {
    key: ContentKey,
    body: Content,
    stored_at: Instant,
}
struct PooledClient {
    identity: Identity,
    client: Client,
    created_at: Instant,
}

#[derive(Serialize)]
struct OriginTrace {
    attempt_id: usize,
    attempted: bool,
    task_id: String,
    tenant: String,
    epoch: u64,
    source: String,
    queue_start_ns: u64,
    service_start_ns: Option<u64>,
    headers_ns: Option<u64>,
    terminal_ns: u64,
    outcome: String,
    peer: Option<String>,
    peer_verified: bool,
    status: Option<u16>,
    bytes_received: usize,
}

fn ns(start: Instant) -> u64 {
    start.elapsed().as_nanos().min(u64::MAX as u128) as u64
}

struct TraceGuard {
    trace: Option<OriginTrace>,
    traces: Arc<Mutex<Vec<OriginTrace>>>,
    start: Instant,
}
impl TraceGuard {
    fn row(&mut self) -> &mut OriginTrace {
        self.trace.as_mut().expect("owned trace")
    }
}
impl Drop for TraceGuard {
    fn drop(&mut self) {
        if let Some(mut trace) = self.trace.take() {
            trace.terminal_ns = ns(self.start);
            self.traces.lock().unwrap().push(trace);
        }
    }
}

struct ActivePermit {
    _permit: OwnedSemaphorePermit,
    active: Arc<AtomicUsize>,
}
impl ActivePermit {
    fn new(permit: OwnedSemaphorePermit, active: Arc<AtomicUsize>, peak: &AtomicUsize) -> Self {
        let now = active.fetch_add(1, Ordering::SeqCst) + 1;
        peak.fetch_max(now, Ordering::SeqCst);
        Self {
            _permit: permit,
            active,
        }
    }
}
impl Drop for ActivePermit {
    fn drop(&mut self) {
        self.active.fetch_sub(1, Ordering::SeqCst);
    }
}

struct Service {
    input: Input,
    address: SocketAddr,
    ca: Certificate,
    peer_der: Vec<u8>,
    certificate_identity: String,
    start: Instant,
    pending: Arc<Semaphore>,
    origins: Arc<Semaphore>,
    active_pending: Arc<AtomicUsize>,
    active_origins: Arc<AtomicUsize>,
    peak_pending: AtomicUsize,
    peak_origins: AtomicUsize,
    physical_attempts: AtomicUsize,
    source_leaders: AtomicUsize,
    representation_leaders: AtomicUsize,
    client_builds: AtomicUsize,
    authorization_checks: AtomicUsize,
    pool_evictions: AtomicUsize,
    cache_hits: AtomicUsize,
    revoked: Mutex<HashSet<(String, u64)>>,
    cpu: Arc<Semaphore>,
    active_cpu: Arc<AtomicUsize>,
    peak_cpu: AtomicUsize,
    workers: Mutex<Vec<tokio::task::JoinHandle<()>>>,
    cpu_traces: Arc<Mutex<Vec<Value>>>,
    pool: Mutex<VecDeque<PooledClient>>,
    cache: Mutex<VecDeque<StoredBody>>,
    sources: SingleFlight<FlightKey, Content, String>,
    representations: SingleFlight<RepresentationKey, Representation, String>,
    traces: Arc<Mutex<Vec<OriginTrace>>>,
}

impl Service {
    fn new(input: Input) -> Result<Self, String> {
        let lab = input.lab.as_ref().ok_or("missing lab")?;
        let pem = read_small(&lab.ca_pem_path, 65536)?;
        let ca = Certificate::from_pem(&pem).map_err(|_| "invalid lab CA certificate")?;
        let peer_der = read_small(&lab.peer_der_path, 65536)?;
        if peer_der.is_empty() {
            return Err("empty peer certificate pin".into());
        }
        let certificate_identity = format!(
            "{}:{}",
            blake3::hash(&pem).to_hex(),
            blake3::hash(&peer_der).to_hex()
        );
        let address = lab.address.parse().map_err(|_| "invalid lab address")?;
        Ok(Self {
            pending: Arc::new(Semaphore::new(input.max_pending)),
            origins: Arc::new(Semaphore::new(input.max_origin_inflight)),
            cpu: Arc::new(Semaphore::new(input.max_origin_inflight)),
            input,
            address,
            ca,
            peer_der,
            certificate_identity,
            start: Instant::now(),
            active_pending: Arc::new(AtomicUsize::new(0)),
            active_origins: Arc::new(AtomicUsize::new(0)),
            peak_pending: AtomicUsize::new(0),
            peak_origins: AtomicUsize::new(0),
            physical_attempts: AtomicUsize::new(0),
            source_leaders: AtomicUsize::new(0),
            representation_leaders: AtomicUsize::new(0),
            client_builds: AtomicUsize::new(0),
            authorization_checks: AtomicUsize::new(0),
            pool_evictions: AtomicUsize::new(0),
            cache_hits: AtomicUsize::new(0),
            revoked: Mutex::new(HashSet::new()),
            pool: Mutex::new(VecDeque::new()),
            active_cpu: Arc::new(AtomicUsize::new(0)),
            peak_cpu: AtomicUsize::new(0),
            workers: Mutex::new(Vec::new()),
            cpu_traces: Arc::new(Mutex::new(Vec::new())),
            cache: Mutex::new(VecDeque::new()),
            sources: SingleFlight::new(),
            representations: SingleFlight::new(),
            traces: Arc::new(Mutex::new(Vec::new())),
        })
    }

    fn identity(&self, request: &Arrival) -> Identity {
        Identity {
            tenant: request.tenant.clone(),
            privacy: request.privacy.clone(),
            policy_id: request.policy_id.clone(),
            epoch: request.epoch,
            max_body_bytes: request.max_body_bytes.unwrap_or(self.input.max_body_bytes),
            deadline_ms: self.input.deadline_ms,
            address: self.address,
            certificate: self.certificate_identity.clone(),
        }
    }

    fn authorize(&self, request: &Arrival) -> Result<(), String> {
        self.authorization_checks.fetch_add(1, Ordering::SeqCst);
        let elapsed_us = self.start.elapsed().as_micros() as u64;
        if self
            .revoked
            .lock()
            .unwrap()
            .contains(&(request.tenant.clone(), request.epoch))
            || self.input.revocations.iter().any(|r| {
                r.tenant == request.tenant && r.epoch == request.epoch && elapsed_us >= r.at_us
            })
        {
            self.pool.lock().unwrap().retain(|p| {
                p.identity.tenant != request.tenant || p.identity.epoch != request.epoch
            });
            self.cache.lock().unwrap().retain(|p| {
                p.key.identity.tenant != request.tenant || p.key.identity.epoch != request.epoch
            });
            return Err("revoked".into());
        }
        Ok(())
    }

    fn build_client(&self) -> Result<Client, String> {
        self.client_builds.fetch_add(1, Ordering::SeqCst);
        Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .https_only(true)
            .http1_only()
            .tls_info(true)
            .tls_certs_only([self.ca.clone()])
            .resolve_to_addrs(LAB_HOST, &[self.address])
            .connect_timeout(Duration::from_millis(self.input.deadline_ms))
            .timeout(Duration::from_millis(self.input.deadline_ms))
            .pool_max_idle_per_host(1)
            .pool_idle_timeout(Duration::from_millis(self.input.pool_lifetime_ms))
            .build()
            .map_err(|_| "client_build_error".into())
    }

    fn client(&self, identity: &Identity) -> Result<Client, String> {
        if self.input.variant == "client_per_fetch" {
            return self.build_client();
        }
        let mut pool = self.pool.lock().unwrap();
        let prior = pool.len();
        pool.retain(|p| {
            p.created_at.elapsed() < Duration::from_millis(self.input.pool_lifetime_ms)
        });
        self.pool_evictions
            .fetch_add(prior - pool.len(), Ordering::SeqCst);
        if let Some(index) = pool.iter().position(|p| &p.identity == identity) {
            let entry = pool.remove(index).unwrap();
            let client = entry.client.clone();
            pool.push_back(entry);
            return Ok(client);
        }
        let client = self.build_client()?;
        if pool.len() == self.input.max_pool_entries {
            pool.pop_front();
            self.pool_evictions.fetch_add(1, Ordering::SeqCst);
        }
        pool.push_back(PooledClient {
            identity: identity.clone(),
            client: client.clone(),
            created_at: Instant::now(),
        });
        Ok(client)
    }

    fn cached(&self, key: &ContentKey) -> Option<(Content, Duration)> {
        let mut cache = self.cache.lock().unwrap();
        let index = cache.iter().position(|entry| &entry.key == key)?;
        let entry = cache.remove(index).unwrap();
        let result = (entry.body.clone(), entry.stored_at.elapsed());
        cache.push_back(entry);
        Some(result)
    }

    fn store(&self, key: ContentKey, body: Content) {
        let mut cache = self.cache.lock().unwrap();
        cache.retain(|entry| entry.key != key);
        if cache.len() == self.input.max_cache_entries {
            cache.pop_front();
        }
        cache.push_back(StoredBody {
            key,
            body,
            stored_at: Instant::now(),
        });
    }

    async fn run_cpu<R, F>(
        &self,
        task_id: String,
        phase: &'static str,
        input_bytes: usize,
        origin: Option<Arc<ActivePermit>>,
        job: F,
    ) -> Result<R, String>
    where
        R: Send + 'static,
        F: FnOnce() -> Result<(R, usize), String> + Send + 'static,
    {
        let queue_start = ns(self.start);
        let permit = Arc::clone(&self.cpu)
            .acquire_owned()
            .await
            .map_err(|_| "cpu_closed")?;
        let permit = ActivePermit::new(permit, Arc::clone(&self.active_cpu), &self.peak_cpu);
        let start = self.start;
        let traces = Arc::clone(&self.cpu_traces);
        let (sender, receiver) = tokio::sync::oneshot::channel();
        let worker = tokio::task::spawn_blocking(move || {
            let _cpu_permit = permit;
            let _origin_permit = origin;
            let execution_start = ns(start);
            let result = job();
            let execution_end = ns(start);
            traces.lock().unwrap().push(json!({"task_id":task_id,"phase":phase,
                "queue_start_ns":queue_start,"execution_start_ns":execution_start,"execution_end_ns":execution_end,
                "input_bytes":input_bytes,"output_bytes":result.as_ref().map(|(_,bytes)| *bytes).unwrap_or(0),
                "outcome":if result.is_ok() {"completed"} else {"error"}}));
            result.map(|(value, _)| value)
        });
        let supervisor = tokio::spawn(async move {
            let _ = sender.send(worker.await);
        });
        self.workers.lock().unwrap().push(supervisor);
        receiver
            .await
            .map_err(|_| "cpu_supervisor_failed")?
            .map_err(|_| "cpu_worker_panicked")?
    }

    async fn fetch(&self, request: &Arrival, key: &ContentKey) -> Result<Content, String> {
        self.authorize(request)?;
        let id = self.source_leaders.fetch_add(1, Ordering::SeqCst);
        let old = if request.cache_mode == "bypass" {
            None
        } else {
            self.cached(key)
        };
        if request.cache_mode == "fresh"
            && let Some((ref content, age)) = old
            && age < Duration::from_millis(self.input.fresh_for_ms)
        {
            let mut content = content.clone();
            content.cache_state = "fresh_hit".into();
            content.origin_id = None;
            self.cache_hits.fetch_add(1, Ordering::SeqCst);
            self.authorize(request)?;
            return Ok(content);
        }
        let mut trace = TraceGuard {
            trace: Some(OriginTrace {
                attempt_id: id,
                attempted: false,
                task_id: request.task_id.clone(),
                tenant: request.tenant.clone(),
                epoch: request.epoch,
                source: request.source.clone(),
                queue_start_ns: ns(self.start),
                service_start_ns: None,
                headers_ns: None,
                terminal_ns: 0,
                outcome: "cancelled_before_origin".into(),
                peer: None,
                peer_verified: false,
                status: None,
                bytes_received: 0,
            }),
            traces: Arc::clone(&self.traces),
            start: self.start,
        };
        let permit = if self.input.queue_mode == "reject" {
            Arc::clone(&self.origins)
                .try_acquire_owned()
                .map_err(|_| "origin_busy")
        } else {
            Arc::clone(&self.origins)
                .acquire_owned()
                .await
                .map_err(|_| "origin_closed")
        };
        let permit = match permit {
            Ok(permit) => permit,
            Err(error) => {
                trace.row().outcome = error.into();
                return Err(error.into());
            }
        };
        let permit = Arc::new(ActivePermit::new(
            permit,
            Arc::clone(&self.active_origins),
            &self.peak_origins,
        ));
        self.authorize(request)?;
        trace.row().service_start_ns = Some(ns(self.start));
        trace.row().outcome = "cancelled_during_transport".into();
        let result = self
            .fetch_transport(request, key, old, id, &mut trace, permit)
            .await;
        trace.row().outcome = match &result {
            Ok(_) => "completed".into(),
            Err(error) => error.clone(),
        };
        result
    }

    async fn fetch_transport(
        &self,
        request: &Arrival,
        key: &ContentKey,
        old: Option<(Content, Duration)>,
        id: usize,
        trace: &mut TraceGuard,
        permit: Arc<ActivePermit>,
    ) -> Result<Content, String> {
        let client = self.client(&key.identity)?;
        self.physical_attempts.fetch_add(1, Ordering::SeqCst);
        trace.row().attempted = true;
        let url = format!(
            "https://{}:{}/{}",
            LAB_HOST,
            self.address.port(),
            request.source
        );
        let mut operation = client
            .get(url)
            .header(header::ACCEPT_ENCODING, "identity")
            .header("x-lab-tenant", &request.tenant)
            .header("x-lab-epoch", request.epoch.to_string())
            .header("x-lab-attempt", id.to_string());
        if let Some((ref previous, _)) = old {
            operation = operation.header(header::IF_NONE_MATCH, &previous.etag);
        }
        let response = operation.send().await.map_err(|error| {
            if error.is_timeout() {
                "transport_timeout"
            } else {
                "transport_error"
            }
            .to_string()
        })?;
        trace.row().headers_ns = Some(ns(self.start));
        let peer = response.remote_addr();
        trace.row().peer = peer.map(|p| p.to_string());
        if peer != Some(self.address) {
            return Err("peer_mismatch".into());
        }
        if response
            .extensions()
            .get::<TlsInfo>()
            .and_then(TlsInfo::peer_certificate)
            != Some(self.peer_der.as_slice())
        {
            return Err("tls_peer_certificate_mismatch".into());
        }
        trace.row().peer_verified = true;
        let status = response.status().as_u16();
        trace.row().status = Some(status);
        if status == 200
            && response
                .headers()
                .get(header::CONTENT_TYPE)
                .and_then(|h| h.to_str().ok())
                .is_none_or(|value| {
                    !value
                        .split(';')
                        .next()
                        .unwrap_or("")
                        .trim()
                        .eq_ignore_ascii_case("text/html")
                })
        {
            return Err("unsupported_media_type".into());
        }
        if response
            .headers()
            .get(header::CONTENT_ENCODING)
            .and_then(|h| h.to_str().ok())
            .is_some_and(|h| !h.eq_ignore_ascii_case("identity"))
        {
            return Err("unsupported_encoding".into());
        }
        if response
            .content_length()
            .is_some_and(|n| n > key.identity.max_body_bytes as u64)
        {
            return Err("too_large".into());
        }
        let etag = response
            .headers()
            .get(header::ETAG)
            .and_then(|h| h.to_str().ok())
            .unwrap_or("")
            .to_owned();
        let mut stream = response.bytes_stream();
        let mut bytes = Vec::new();
        while let Some(part) = stream.next().await {
            let part = part.map_err(|_| "body_error")?;
            if part.len() > key.identity.max_body_bytes.saturating_sub(bytes.len()) {
                return Err("too_large".into());
            }
            bytes.extend_from_slice(&part);
            trace.row().bytes_received = bytes.len();
        }
        self.authorize(request)?;
        let content = if status == 304 {
            let (mut previous, _) = old.ok_or("orphan_304")?;
            if !bytes.is_empty() || etag.is_empty() || previous.etag != etag {
                return Err("invalid_304".into());
            }
            previous.status = 304;
            previous.origin_id = Some(id);
            previous.cache_state = "revalidated".into();
            previous
        } else if status == 200 {
            let body = String::from_utf8(bytes).map_err(|_| "invalid_utf8")?;
            let html = body.clone();
            let page_url = Url::parse(&format!("https://{LAB_HOST}/{}", request.source))
                .map_err(|_| "logical_url")?;
            let max_bytes = key.identity.max_body_bytes;
            let page = self
                .run_cpu(
                    request.task_id.clone(),
                    "parse_projection",
                    html.len(),
                    Some(permit),
                    move || {
                        let page = projection::parse_html_with_limits(
                            &html,
                            &page_url,
                            projection::ParseLimits {
                                max_html_bytes: max_bytes,
                                max_text_bytes: max_bytes,
                                max_nodes: 10000,
                                max_parser_nodes: 10000,
                                max_words_per_chunk: 1000,
                                max_link_bytes: max_bytes,
                                ..projection::ParseLimits::default()
                            },
                        )
                        .map_err(|_| "parse_error".to_string())?;
                        let output_bytes = page.chunks.iter().map(|chunk| chunk.text.len()).sum();
                        Ok((Arc::new(page), output_bytes))
                    },
                )
                .await?;
            Content {
                body,
                page,
                etag,
                status,
                origin_id: Some(id),
                peer_verified: true,
                cache_state: "origin_200".into(),
            }
        } else {
            return Err(format!("http_{status}"));
        };
        self.authorize(request)?;
        if request.cache_mode != "bypass" && !content.etag.is_empty() {
            self.store(key.clone(), content.clone());
        }
        Ok(content)
    }

    async fn source(&self, request: &Arrival, key: FlightKey) -> Result<Arc<Content>, String> {
        if self.input.source_singleflight {
            self.sources
                .call(key.clone(), || self.fetch(request, &key.content))
                .await
                .map_err(flight_error)
        } else {
            self.fetch(request, &key.content).await.map(Arc::new)
        }
    }

    async fn render(
        &self,
        request: &Arrival,
        source_key: FlightKey,
    ) -> Result<Representation, String> {
        self.representation_leaders.fetch_add(1, Ordering::SeqCst);
        let content = self.source(request, source_key).await?;
        self.authorize(request)?;
        let page = Arc::clone(&content.page);
        let query = request.query.clone();
        let page_url = Url::parse(&format!("https://{LAB_HOST}/{}", request.source))
            .map_err(|_| "logical_url")?;
        let text = self
            .run_cpu(
                request.task_id.clone(),
                "rank_export_aae",
                content.body.len(),
                None,
                move || {
                    let output = aae::export_jsonl_with_limit(
                        &page,
                        &page_url,
                        0,
                        Some(&query),
                        6,
                        256 * 1024,
                    )
                    .map_err(|_| "aae_export_error".to_string())?;
                    let bytes = output.len();
                    Ok((output, bytes))
                },
            )
            .await?;
        Ok(Representation {
            text,
            content: (*content).clone(),
        })
    }

    async fn retrieve(&self, request: &Arrival) -> Result<Arc<Representation>, String> {
        self.authorize(request)?;
        let key = FlightKey {
            content: ContentKey {
                identity: self.identity(request),
                source: request.source.clone(),
            },
            cache_mode: request.cache_mode.clone(),
        };
        let result = if self.input.representation_singleflight {
            self.representations
                .call(
                    RepresentationKey {
                        source: key.clone(),
                        query: request.query.clone(),
                    },
                    || self.render(request, key),
                )
                .await
                .map_err(flight_error)
        } else {
            self.render(request, key).await.map(Arc::new)
        };
        self.authorize(request)?;
        result
    }
}

fn flight_error(error: FlightError<String>) -> String {
    match error {
        FlightError::Fetch(error) => (*error).clone(),
        FlightError::Cancelled => "shared_cancelled".into(),
    }
}

#[derive(Serialize)]
struct Row {
    task_id: String,
    scheduled_ns: u64,
    input_at_us: u64,
    offered_ns: u64,
    accepted_ns: Option<u64>,
    terminal_ns: u64,
    latency_ns: u64,
    outcome: String,
    body: Option<String>,
    representation: Option<String>,
    source_status: Option<u16>,
    origin_id: Option<usize>,
    cache_state: Option<String>,
    peer_verified: bool,
}

impl Row {
    fn control_offer(service: &Service, request: &Arrival) -> Self {
        let now = ns(service.start);
        Self {
            task_id: request.task_id.clone(),
            scheduled_ns: now,
            input_at_us: request.at_us,
            offered_ns: now,
            accepted_ns: None,
            terminal_ns: 0,
            latency_ns: 0,
            outcome: String::new(),
            body: None,
            representation: None,
            source_status: None,
            origin_id: None,
            cache_state: None,
            peer_verified: false,
        }
    }

    fn control_finish(&mut self, service: &Service, result: Result<Arc<Representation>, String>) {
        match result {
            Ok(result) => {
                self.outcome = "completed".into();
                self.body = Some(result.content.body.clone());
                self.representation = Some(result.text.clone());
                self.source_status = Some(result.content.status);
                self.origin_id = result.content.origin_id;
                self.cache_state = Some(result.content.cache_state.clone());
                self.peer_verified = result.content.peer_verified;
            }
            Err(error) => self.outcome = error,
        }
        self.terminal_ns = ns(service.start);
        self.latency_ns = self.terminal_ns.saturating_sub(self.scheduled_ns);
    }
}

async fn control_request(
    service: &Service,
    request: &Arrival,
    row: &mut Row,
) -> Result<Arc<Representation>, String> {
    let permit = Arc::clone(&service.pending)
        .try_acquire_owned()
        .map_err(|_| "pending_rejected")?;
    let _permit = ActivePermit::new(
        permit,
        Arc::clone(&service.active_pending),
        &service.peak_pending,
    );
    row.accepted_ns = Some(ns(service.start));
    service.retrieve(request).await
}

async fn controlled_cancellation(service: &Service) -> Result<(Vec<Row>, Value), String> {
    let requests = &service.input.arrivals;
    let leader_cancel = service.input.mode == "cancel_leader";
    if requests.len() != if leader_cancel { 3 } else { 2 }
        || requests.iter().any(|r| {
            r.at_us != 0
                || r.cancel_after_us.is_some()
                || r.cache_mode != "bypass"
                || r.source != requests[0].source
                || service.identity(r) != service.identity(&requests[0])
        })
        || requests[0].query == requests[1].query
    {
        return Err("cancellation control requires one partition/source, distinct queries and explicit dependent steps".into());
    }
    let mut leader_row = Row::control_offer(service, &requests[0]);
    let mut leader = Box::pin(control_request(service, &requests[0], &mut leader_row));
    let leader_pending = futures_util::poll!(&mut leader).is_pending();
    if !leader_pending {
        return Err("leader did not enter a pending real retrieval".into());
    }
    let mut follower_row = Row::control_offer(service, &requests[1]);
    let mut follower = Box::pin(control_request(service, &requests[1], &mut follower_row));
    let follower_pending = futures_util::poll!(&mut follower).is_pending();
    if !follower_pending {
        return Err("follower did not enter a pending real retrieval".into());
    }
    let control = json!({"leader_polled_pending": leader_pending, "follower_polled_pending": follower_pending,
        "source_leaders_before_cancel": service.source_leaders.load(Ordering::SeqCst),
        "cancel_at_ns": ns(service.start),
        "scope": "Manually polled actual retrieval futures; cancellation follows follower subscription. TLS need not have completed before cancellation; not an offered-load or latency experiment."});
    let bound = Duration::from_millis(service.input.deadline_ms);
    if leader_cancel {
        drop(leader);
        leader_row.control_finish(service, Err("cancelled".into()));
        let result = tokio::time::timeout(bound, follower)
            .await
            .map_err(|_| "controlled follower timed out")?;
        follower_row.control_finish(service, result);
    } else {
        drop(follower);
        follower_row.control_finish(service, Err("cancelled".into()));
        let result = tokio::time::timeout(bound, leader)
            .await
            .map_err(|_| "controlled leader timed out")?;
        leader_row.control_finish(service, result);
    }
    let mut rows = vec![leader_row, follower_row];
    if leader_cancel {
        let mut row = Row::control_offer(service, &requests[2]);
        let result = tokio::time::timeout(bound, control_request(service, &requests[2], &mut row))
            .await
            .map_err(|_| "controlled recovery timed out")?;
        row.control_finish(service, result);
        rows.push(row);
    }
    Ok((rows, control))
}

async fn arrive(service: Arc<Service>, request: Arrival, input_at_us: u64) -> Row {
    let scheduled = service.start + Duration::from_micros(request.at_us);
    tokio::time::sleep_until(scheduled).await;
    let mut row = Row {
        task_id: request.task_id.clone(),
        scheduled_ns: request.at_us * 1000,
        input_at_us,
        offered_ns: ns(service.start),
        accepted_ns: None,
        terminal_ns: 0,
        latency_ns: 0,
        outcome: String::new(),
        body: None,
        representation: None,
        source_status: None,
        origin_id: None,
        cache_state: None,
        peer_verified: false,
    };
    if request.revoke_before {
        service
            .revoked
            .lock()
            .unwrap()
            .insert((request.tenant.clone(), request.epoch));
    }
    if let Err(error) = service.authorize(&request) {
        row.outcome = error;
    } else if let Ok(permit) = Arc::clone(&service.pending).try_acquire_owned() {
        let _permit = ActivePermit::new(
            permit,
            Arc::clone(&service.active_pending),
            &service.peak_pending,
        );
        row.accepted_ns = Some(ns(service.start));
        let deadline = scheduled + Duration::from_millis(service.input.deadline_ms);
        let cancel = request
            .cancel_after_us
            .map(|n| scheduled + Duration::from_micros(n));
        let terminal = cancel.map_or(deadline, |at| at.min(deadline));
        match tokio::time::timeout_at(terminal, service.retrieve(&request)).await {
            Ok(Ok(result)) => {
                row.outcome = "completed".into();
                row.body = Some(result.content.body.clone());
                row.representation = Some(result.text.clone());
                row.source_status = Some(result.content.status);
                row.origin_id = result.content.origin_id;
                row.cache_state = Some(result.content.cache_state.clone());
                row.peer_verified = result.content.peer_verified;
            }
            Ok(Err(error)) => row.outcome = error,
            Err(_) => {
                row.outcome = if cancel.is_some_and(|at| at < deadline) {
                    "cancelled"
                } else {
                    "timed_out"
                }
                .into()
            }
        }
    } else {
        row.outcome = "pending_rejected".into();
    }
    row.terminal_ns = ns(service.start);
    row.latency_ns = row.terminal_ns.saturating_sub(row.scheduled_ns);
    row
}

fn limits() -> Value {
    json!({"boundary": "research-only loopback TLS + actual core SingleFlight; production Engine success is not exercised",
        "transport": "locked reqwest HTTP/1.1, exact lab CA roots, hostname verification, exact leaf DER and socket peer pin; no redirects or inherited proxy",
        "not_verified": ["production permitted-public-DNS/TLS integration and egress containment", "authoritative DNS TTL", "HTTP/2 or HTTP/3", "production authorization service and robots", "production Engine success/cache integration", "deployment topology or distributed load", "long-run RSS/allocator stability and hard response-header cap", "result authenticity against project-controlled OS authority"],
        "representation": "actual core bounded HTML parse/Projection and ranked AAE/1 export; fixture observed_at_ms=0 and stable logical source URL; production Engine success integration remains unverified",
        "resource_caps": {"arrivals": 256, "offered_horizon_us": 30000000, "deadline_ms": 10000,
            "pending": 256, "physical_origins": 16, "pool_entries": 16, "cache_entries": 16,
            "body_bytes_per_request": 16384, "input_bytes": MAX_INPUT_BYTES},
        "timing": "monotonic raw ns; load uses independent absolute arrivals; journey uses dependent steps with at_us as delay after previous completion; row latency starts at actual scheduled offer; traces separate origin queue/service and CPU parse/export",
        "claims": "No service p99, throughput winner, production readiness or performance adoption from these finite controls"})
}

async fn load(input: Input) -> Result<Value, String> {
    let setup = Instant::now();
    let service = Arc::new(Service::new(input)?);
    let setup_ns = ns(setup);
    let mut rows = Vec::new();
    let mut cancellation_control = Value::Null;
    if matches!(
        service.input.mode.as_str(),
        "cancel_leader" | "cancel_follower"
    ) {
        (rows, cancellation_control) = controlled_cancellation(&service).await?;
    } else if service.input.mode == "journey" {
        for request in &service.input.arrivals {
            let mut step = request.clone();
            step.at_us += ns(service.start).div_ceil(1000);
            rows.push(arrive(Arc::clone(&service), step, request.at_us).await);
        }
    } else {
        let mut tasks = Vec::new();
        for request in &service.input.arrivals {
            tasks.push(tokio::spawn(arrive(
                Arc::clone(&service),
                request.clone(),
                request.at_us,
            )));
        }
        for task in tasks {
            rows.push(task.await.map_err(|_| "arrival worker panicked")?);
        }
    }
    rows.sort_by(|a, b| a.task_id.cmp(&b.task_id));
    let all_terminal_ns = ns(service.start);
    let active_origins_when_callers_terminal = service.active_origins.load(Ordering::SeqCst);
    let active_cpu_when_callers_terminal = service.active_cpu.load(Ordering::SeqCst);
    let workers = std::mem::take(&mut *service.workers.lock().unwrap());
    let worker_count = workers.len();
    for worker in workers {
        worker
            .await
            .map_err(|_| "CPU supervisor failed while draining")?;
    }
    let pool_entries_before_drop = service.pool.lock().unwrap().len();
    let cache_entries_before_drop = service.cache.lock().unwrap().len();
    service.pool.lock().unwrap().clear();
    service.cache.lock().unwrap().clear();
    let mut traces = std::mem::take(&mut *service.traces.lock().unwrap());
    traces.sort_by_key(|trace| trace.attempt_id);
    let mut outcomes = serde_json::Map::new();
    for row in &rows {
        let value = outcomes.entry(row.outcome.clone()).or_insert(json!(0));
        *value = json!(value.as_u64().unwrap() + 1);
    }
    let accepted = rows.iter().filter(|row| row.accepted_ns.is_some()).count();
    Ok(
        json!({"protocol": PROTOCOL, "mode": service.input.mode, "variant": service.input.variant,
        "queue_mode": service.input.queue_mode, "setup_ns": setup_ns, "all_terminal_ns": all_terminal_ns,
        "cancellation_control": cancellation_control,
        "cleanup_ns": ns(service.start).saturating_sub(all_terminal_ns), "capabilities": limits(),
        "revocations": service.input.revocations, "certificate_identity_blake3": service.certificate_identity,
        "counters": {"offered": rows.len(), "accepted": accepted,
            "completed": rows.iter().filter(|row| row.outcome == "completed").count(),
            "outcomes": outcomes, "origin_attempts": service.physical_attempts.load(Ordering::SeqCst),
            "source_leaders": service.source_leaders.load(Ordering::SeqCst),
            "representation_leaders": service.representation_leaders.load(Ordering::SeqCst),
            "client_builds": service.client_builds.load(Ordering::SeqCst),
            "authorization_checks": service.authorization_checks.load(Ordering::SeqCst),
            "cache_hits": service.cache_hits.load(Ordering::SeqCst),
            "pool_evictions": service.pool_evictions.load(Ordering::SeqCst),
            "peak_pending": service.peak_pending.load(Ordering::SeqCst),
            "peak_origins": service.peak_origins.load(Ordering::SeqCst),
            "peak_cpu": service.peak_cpu.load(Ordering::SeqCst),
            "active_origins_when_callers_terminal": active_origins_when_callers_terminal,
            "active_cpu_when_callers_terminal": active_cpu_when_callers_terminal,
            "actual_cpu_workers_joined": worker_count,
            "active_cpu_at_end": service.active_cpu.load(Ordering::SeqCst),
            "available_cpu_at_end": service.cpu.available_permits(),
            "active_pending_at_end": service.active_pending.load(Ordering::SeqCst),
            "active_origins_at_end": service.active_origins.load(Ordering::SeqCst),
            "available_pending_at_end": service.pending.available_permits(),
            "available_origin_at_end": service.origins.available_permits(),
            "pool_entries_before_drop": pool_entries_before_drop, "cache_entries_before_drop": cache_entries_before_drop,
            "pool_entries_after_drop": service.pool.lock().unwrap().len(), "cache_entries_after_drop": service.cache.lock().unwrap().len()},
        "rows": rows, "origin_attempts": traces,"cpu_traces":service.cpu_traces.lock().unwrap().as_slice()}),
    )
}

async fn permit_control(worker_owns: bool) -> Result<Value, String> {
    let start = Instant::now();
    let semaphore = Arc::new(Semaphore::new(1));
    let permits = Arc::clone(&semaphore);
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let (handle_tx, handle_rx) = tokio::sync::oneshot::channel();
    let (release_tx, release_rx) = std::sync::mpsc::sync_channel(0);
    let caller = tokio::spawn(async move {
        let permit = Arc::new(permits.acquire_owned().await.unwrap());
        let worker_permit = if worker_owns {
            Some(Arc::clone(&permit))
        } else {
            None
        };
        let handle = tokio::task::spawn_blocking(move || {
            let _permit = worker_permit;
            let _ = started_tx.send(());
            release_rx.recv_timeout(Duration::from_secs(2)).is_ok()
        });
        let _ = handle_tx.send(handle);
        let _caller_permit = permit;
        std::future::pending::<()>().await;
    });
    let handle = handle_rx.await.map_err(|_| "missing blocking worker")?;
    started_rx.await.map_err(|_| "worker did not start")?;
    caller.abort();
    let _ = caller.await;
    let before_exit = semaphore.available_permits();
    release_tx.send(()).map_err(|_| "worker release failed")?;
    let released = handle.await.map_err(|_| "blocking worker panicked")?;
    Ok(
        json!({"worker_owns_permit": worker_owns, "available_before_worker_exit": before_exit,
        "available_after_worker_exit": semaphore.available_permits(), "actual_worker_joined": released,
        "retention_invariant": before_exit == 0, "duration_ns": ns(start)}),
    )
}

async fn contracts() -> Result<Value, String> {
    let mut checks = Vec::new();
    let mut policy = FetchPolicy::for_host("localhost");
    policy.request_timeout = Duration::from_secs(1);
    let invalid = fetch_https(
        &Url::parse("https://localhost/").unwrap(),
        &policy,
        Some("bad\r\nvalidator"),
    )
    .await;
    checks.push(json!({"id":"production_invalid_validator_before_dns", "passed":matches!(invalid, Err(FetchError::InvalidValidator)),
        "observed":format!("{invalid:?}"), "boundary":"actual core fetch_https validation"}));
    let loopback = fetch_https(&Url::parse("https://localhost/").unwrap(), &policy, None).await;
    checks.push(json!({"id":"production_loopback_denial", "passed":matches!(loopback, Err(FetchError::DeniedAddress)),
        "observed":format!("{loopback:?}"), "boundary":"actual core OS localhost resolution and address classification; no HTTPS connection allowed"}));
    let literal = fetch_https(
        &Url::parse("https://127.0.0.1/").unwrap(),
        &FetchPolicy::for_host("127.0.0.1"),
        None,
    )
    .await;
    checks.push(json!({"id":"production_ip_literal_denial", "passed":matches!(literal, Err(FetchError::DeniedHost)), "observed":format!("{literal:?}")}));
    let port = fetch_https(
        &Url::parse("https://localhost:444/").unwrap(),
        &policy,
        None,
    )
    .await;
    checks.push(json!({"id":"production_non443_denial", "passed":matches!(port, Err(FetchError::InvalidUrl)), "observed":format!("{port:?}")}));
    let engine = Engine::with_pending_limit(1, 100, 0);
    let denied = engine
        .retrieve(
            RequestKey {
                url: "https://localhost/".into(),
                tenant_partition: "lab".into(),
                query: "q".into(),
            },
            policy,
        )
        .await;
    let overloaded = matches!(&denied, Err(FlightError::Fetch(error)) if matches!(**error, PipelineError::Overloaded));
    checks.push(json!({"id":"production_engine_zero_admission", "passed":overloaded, "observed":format!("{denied:?}"), "boundary":"actual core Engine::retrieve, zero pending permits"}));

    let flight = Arc::new(SingleFlight::<u32, u32, ()>::new());
    let clone = Arc::clone(&flight);
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let leader = tokio::spawn(async move {
        clone
            .call(7, || async {
                let _ = started_tx.send(());
                std::future::pending::<Result<u32, ()>>().await
            })
            .await
    });
    started_rx.await.map_err(|_| "leader did not start")?;
    let mut follower = Box::pin(flight.call(7, || async { Ok(999) }));
    let follower_waited = futures_util::poll!(&mut follower).is_pending();
    leader.abort();
    let _ = leader.await;
    let cancelled = matches!(follower.await, Err(FlightError::Cancelled));
    let recovered = flight
        .call(7, || async { Ok(42) })
        .await
        .map(|value| *value)
        .ok();
    checks.push(json!({"id":"core_leader_cancel_wakes_follower_and_recovers", "passed":follower_waited && cancelled && recovered == Some(42),
        "follower_waited":follower_waited, "follower_cancelled":cancelled, "retry_value":recovered, "boundary":"actual core SingleFlight, deterministic leader start and follower poll"}));

    let clone = Arc::clone(&flight);
    let (release_tx, release_rx) = tokio::sync::oneshot::channel();
    let (started_tx, started_rx) = tokio::sync::oneshot::channel();
    let leader = tokio::spawn(async move {
        clone
            .call(8, || async {
                let _ = started_tx.send(());
                let _ = release_rx.await;
                Ok(73)
            })
            .await
    });
    started_rx
        .await
        .map_err(|_| "second leader did not start")?;
    let mut follower = Box::pin(flight.call(8, || async { Ok(999) }));
    let follower_waited = futures_util::poll!(&mut follower).is_pending();
    drop(follower);
    let _ = release_tx.send(());
    let leader_value = leader
        .await
        .map_err(|_| "leader join failure")?
        .map(|value| *value)
        .ok();
    checks.push(json!({"id":"core_follower_cancel_preserves_leader", "passed":follower_waited && leader_value == Some(73), "leader_value":leader_value}));
    let sequential = flight
        .call(8, || async { Ok(74) })
        .await
        .map(|value| *value)
        .ok();
    checks.push(json!({"id":"core_completed_flight_is_not_cache", "passed":sequential == Some(74), "sequential_value":sequential}));

    let correct = permit_control(true).await?;
    let wrong = permit_control(false).await?;
    let correct_ok = correct["retention_invariant"] == true
        && correct["actual_worker_joined"] == true
        && correct["available_after_worker_exit"] == 1;
    let wrong_rejected =
        wrong["retention_invariant"] == false && wrong["actual_worker_joined"] == true;
    checks.push(json!({"id":"blocking_worker_permit_and_negative_control", "passed":correct_ok && wrong_rejected,
        "correct":correct, "early_release_negative_control":wrong, "negative_control_rejected":wrong_rejected,
        "boundary":"actual Tokio blocking worker and awaited worker JoinHandle; research wrapper, not private Engine::run_blocking"}));
    let passed = checks.iter().all(|check| check["passed"] == true);
    Ok(
        json!({"protocol":PROTOCOL,"mode":"contracts","passed":passed,"checks":checks,"capabilities":limits()}),
    )
}

fn execute() -> Result<Value, String> {
    let args: Vec<_> = std::env::args_os().collect();
    if args.len() != 2 {
        return Err("usage: service_probe <input.json>".into());
    }
    let input: Input = serde_json::from_slice(&read_small(&args[1], MAX_INPUT_BYTES)?)
        .map_err(|e| format!("input JSON rejected: {e}"))?;
    validate(&input)?;
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(4)
        .enable_all()
        .build()
        .map_err(|_| "runtime creation failed")?;
    runtime.block_on(async move {
        let result = tokio::time::timeout(Duration::from_secs(45), async {
            if input.mode == "contracts" {
                contracts().await
            } else {
                load(input).await
            }
        })
        .await;
        result.map_err(|_| "probe wall deadline exceeded".to_string())?
    })
}

fn main() {
    match execute() {
        Ok(result) => {
            let failed = result.get("passed").is_some_and(|value| value == false);
            let output = serde_json::to_string(&result).expect("JSON result serialization");
            if output.len() > 4 * 1024 * 1024 {
                println!(
                    "{}",
                    json!({"protocol":PROTOCOL,"mode":"error","error":"result byte cap exceeded; no partial task result accepted"})
                );
                std::process::exit(2);
            }
            println!("{output}");
            if failed {
                std::process::exit(1);
            }
        }
        Err(error) => {
            println!(
                "{}",
                json!({"protocol":PROTOCOL,"mode":"error","error":error,"capabilities":limits()})
            );
            std::process::exit(2);
        }
    }
}
