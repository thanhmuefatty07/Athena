//! Query-independent source coalescing + query-dependent representation flights.
//! Distinct agent queries against the same authorized URL may share network+parse
//! while overlapping, without accidentally sharing wrong query-specific highlights.
//! An upstream enforcement service MUST authorize permissions/robots/rate BEFORE call.
use crate::{
    aae,
    network::{FetchError, FetchPolicy, fetch_https_with_admission, validate_request},
    projection::{self, Projection},
    singleflight::{FlightError, SingleFlight},
};
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;
use url::Url;

#[derive(Debug, Clone, Hash, PartialEq, Eq)]
pub struct RequestKey {
    pub url: String,
    pub tenant_partition: String,
    pub query: String,
    // In production also include response Vary headers, locale, auth/cookie
    // identity, representation, source policy version and freshness mode.
}
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct RepresentationKey {
    request: RequestKey,
    policy: Arc<PolicyKey>,
}
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct PolicyKey {
    id: String,
    allowed_hosts: Vec<String>,
    max_body_bytes: usize,
    max_dns_answers: usize,
    connect_timeout: Duration,
    request_timeout: Duration,
}
impl From<&FetchPolicy> for PolicyKey {
    fn from(policy: &FetchPolicy) -> Self {
        let mut allowed_hosts: Vec<_> = policy.allowed_hosts.iter().cloned().collect();
        allowed_hosts.sort_unstable();
        Self {
            id: policy.policy_id.clone(),
            allowed_hosts,
            max_body_bytes: policy.max_body_bytes,
            max_dns_answers: policy.max_dns_answers,
            connect_timeout: policy.connect_timeout,
            request_timeout: policy.request_timeout,
        }
    }
}
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct SourceKey {
    url: String,
    tenant_partition: String,
    policy: Arc<PolicyKey>,
}
#[derive(Debug)]
pub enum PipelineError {
    BadUrl,
    Transport(FetchError),
    HttpStatus(u16),
    UnsupportedMediaType,
    InvalidUtf8,
    Parse(&'static str),
    Serialization,
    OutputTooLarge,
    WorkerFailure,
    Overloaded,
    DeadlineExceeded,
    SharedSource(Arc<PipelineError>),
    SharedSourceCancelled,
}
#[derive(Debug)]
pub struct Retrieval {
    pub status: u16,
    pub aae_jsonl: String,
    pub bytes_downloaded: usize,
}
struct SourceContent {
    page: Projection,
    status: u16,
    bytes_downloaded: usize,
    observed_at_ms: u64,
}
pub struct Engine {
    source_flights: SingleFlight<SourceKey, SourceContent, PipelineError>,
    representation_flights: SingleFlight<RepresentationKey, Retrieval, PipelineError>,
    network_permits: Arc<Semaphore>,
    cpu_permits: Arc<Semaphore>,
    pending_permits: Semaphore,
    max_html_nodes: usize,
}
impl Engine {
    pub fn new(max_origin_inflight: usize, max_html_nodes: usize) -> Self {
        let pending = max_origin_inflight
            .max(1)
            .saturating_mul(32)
            .min(Semaphore::MAX_PERMITS);
        Self::with_pending_limit(max_origin_inflight, max_html_nodes, pending)
    }
    /// max_pending includes coalesced followers; zero denies all admission.
    pub fn with_pending_limit(
        max_origin_inflight: usize,
        max_html_nodes: usize,
        max_pending: usize,
    ) -> Self {
        Self {
            source_flights: SingleFlight::new(),
            representation_flights: SingleFlight::new(),
            network_permits: Arc::new(Semaphore::new(max_origin_inflight.max(1))),
            cpu_permits: Arc::new(Semaphore::new(max_origin_inflight.max(1))),
            pending_permits: Semaphore::new(max_pending),
            max_html_nodes: max_html_nodes.max(1),
        }
    }

    async fn run_blocking<R, F>(&self, job: F) -> Result<R, PipelineError>
    where
        R: Send + 'static,
        F: FnOnce() -> R + Send + 'static,
    {
        let permit = Arc::clone(&self.cpu_permits)
            .acquire_owned()
            .await
            .map_err(|_| PipelineError::Overloaded)?;
        tokio::task::spawn_blocking(move || {
            // Started blocking jobs survive cancellation of their awaiting task.
            let _permit = permit;
            job()
        })
        .await
        .map_err(|_| PipelineError::WorkerFailure)
    }

    /// Upstream source permission must be checked for EACH caller, including
    /// followers, before invoking this method; this is not an auth boundary.
    pub async fn retrieve(
        &self,
        mut key: RequestKey,
        policy: FetchPolicy,
    ) -> Result<Arc<Retrieval>, FlightError<PipelineError>> {
        if key.url.len() > 8192
            || key.query.len() > 4096
            || key.tenant_partition.is_empty()
            || key.tenant_partition.len() > 1024
        {
            return Err(FlightError::Fetch(Arc::new(PipelineError::BadUrl)));
        }
        let _request_permit = self
            .pending_permits
            .try_acquire()
            .map_err(|_| FlightError::Fetch(Arc::new(PipelineError::Overloaded)))?;
        let url = Url::parse(&key.url)
            .map_err(|_| FlightError::Fetch(Arc::new(PipelineError::BadUrl)))?;
        validate_request(&url, &policy)
            .map_err(|e| FlightError::Fetch(Arc::new(PipelineError::Transport(e))))?;
        key.url = url.as_str().to_owned();
        let policy_key = Arc::new(PolicyKey::from(&policy));
        let source_key = SourceKey {
            url: key.url.clone(),
            tenant_partition: key.tenant_partition.clone(),
            policy: Arc::clone(&policy_key),
        };
        let permits = Arc::clone(&self.network_permits);
        let max_nodes = self.max_html_nodes;
        let original = key.clone();
        let rep_key = RepresentationKey {
            request: key,
            policy: policy_key,
        };
        let deadline = policy.request_timeout;
        tokio::time::timeout(
            deadline,
            self.representation_flights
                .call(rep_key, move || async move {
                    // The source is independent of query when anonymous and the same
                    // source-policy+tenant+response variant applies to both calls.
                    let raw_url = url.clone();
                    let source = self
                        .source_flights
                        .call(source_key, move || async move {
                            let permit = Arc::new(
                                permits
                                    .try_acquire_owned()
                                    .map_err(|_| PipelineError::Overloaded)?,
                            );
                            let response = fetch_https_with_admission(
                                &raw_url,
                                &policy,
                                None,
                                Some(Arc::clone(&permit)),
                            )
                            .await
                            .map_err(PipelineError::Transport)?;
                            if response.status != 200 {
                                return Err(PipelineError::HttpStatus(response.status));
                            }
                            let content_type = response.content_type.as_deref().unwrap_or("");
                            if !content_type
                                .split(';')
                                .next()
                                .unwrap_or("")
                                .trim()
                                .eq_ignore_ascii_case("text/html")
                            {
                                return Err(PipelineError::UnsupportedMediaType);
                            }
                            let bytes_downloaded = response.payload.len();
                            let parse_limits = projection::ParseLimits {
                                max_html_bytes: policy.max_body_bytes,
                                max_text_bytes: policy.max_body_bytes,
                                max_nodes,
                                max_parser_nodes: max_nodes,
                                max_words_per_chunk: 1000,
                                max_link_bytes: policy.max_body_bytes,
                                ..projection::ParseLimits::default()
                            };
                            let observed_at_ms = SystemTime::now()
                                .duration_since(UNIX_EPOCH)
                                .unwrap_or_default()
                                .as_millis()
                                .min(u64::MAX as u128)
                                as u64;
                            let page_url = raw_url;
                            let page = self
                                .run_blocking(move || {
                                    let _permit = permit;
                                    let html = std::str::from_utf8(&response.payload)
                                        .map_err(|_| PipelineError::InvalidUtf8)?;
                                    projection::parse_html_with_limits(
                                        html,
                                        &page_url,
                                        parse_limits,
                                    )
                                    .map_err(PipelineError::Parse)
                                })
                                .await??;
                            Ok(SourceContent {
                                page,
                                status: response.status,
                                bytes_downloaded,
                                observed_at_ms,
                            })
                        })
                        .await
                        .map_err(|e| match e {
                            FlightError::Fetch(source_error) => {
                                PipelineError::SharedSource(source_error)
                            }
                            FlightError::Cancelled => PipelineError::SharedSourceCancelled,
                        })?;
                    let query = original.query;
                    let page_source = Arc::clone(&source);
                    let output = self
                        .run_blocking(move || {
                            aae::export_jsonl(
                                &page_source.page,
                                &url,
                                page_source.observed_at_ms,
                                Some(&query),
                                6,
                            )
                            .map_err(|e| {
                                if e.is_io() {
                                    PipelineError::OutputTooLarge
                                } else {
                                    PipelineError::Serialization
                                }
                            })
                        })
                        .await??;
                    Ok(Retrieval {
                        status: source.status,
                        aae_jsonl: output,
                        bytes_downloaded: source.bytes_downloaded,
                    })
                }),
        )
        .await
        .map_err(|_| FlightError::Fetch(Arc::new(PipelineError::DeadlineExceeded)))?
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(query: &str) -> RequestKey {
        RequestKey {
            url: "https://example.org/".into(),
            tenant_partition: "tenant".into(),
            query: query.into(),
        }
    }
    #[tokio::test]
    async fn admitted_cpu_job_waits_for_worker_capacity() {
        let engine = Arc::new(Engine::new(1, 100));
        let first_engine = Arc::clone(&engine);
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(0);
        let first = tokio::spawn(async move {
            first_engine
                .run_blocking(move || {
                    started_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                })
                .await
        });
        started_rx.await.unwrap();
        let mut second = Box::pin(engine.run_blocking(|| 42));
        let state = futures_util::poll!(&mut second);
        release_tx.send(()).unwrap();
        first.await.unwrap().unwrap();
        assert!(state.is_pending());
        assert_eq!(second.await.unwrap(), 42);
    }
    #[tokio::test]
    async fn caller_deadline_covers_waiting_for_a_shared_source() {
        let engine = Engine::new(1, 100);
        let mut policy = FetchPolicy::for_host("example.org");
        policy.request_timeout = Duration::from_millis(10);
        let source_key = SourceKey {
            url: request("").url,
            tenant_partition: "tenant".into(),
            policy: Arc::new(PolicyKey::from(&policy)),
        };
        let mut source = Box::pin(engine.source_flights.call(source_key, || {
            std::future::pending::<Result<SourceContent, PipelineError>>()
        }));
        assert!(futures_util::poll!(&mut source).is_pending());
        let result = tokio::time::timeout(
            Duration::from_secs(1),
            engine.retrieve(request("query"), policy),
        )
        .await;
        assert!(matches!(result, Ok(Err(FlightError::Fetch(error)))
            if matches!(*error, PipelineError::DeadlineExceeded)));
    }
    #[tokio::test]
    async fn pending_query_fanout_is_bounded_before_shared_source_wait() {
        let engine = Engine::with_pending_limit(1, 100, 2);
        let policy = FetchPolicy::for_host("example.org");
        let source_key = SourceKey {
            url: request("").url,
            tenant_partition: "tenant".into(),
            policy: Arc::new(PolicyKey::from(&policy)),
        };
        let mut source = Box::pin(engine.source_flights.call(source_key, || {
            std::future::pending::<Result<SourceContent, PipelineError>>()
        }));
        assert!(futures_util::poll!(&mut source).is_pending());
        let mut first = Box::pin(engine.retrieve(request("first"), policy.clone()));
        let mut second = Box::pin(engine.retrieve(request("second"), policy.clone()));
        assert!(futures_util::poll!(&mut first).is_pending());
        assert!(futures_util::poll!(&mut second).is_pending());
        let mut third = Box::pin(engine.retrieve(request("third"), policy));
        assert!(
            matches!(futures_util::poll!(&mut third), std::task::Poll::Ready(
            Err(FlightError::Fetch(error))) if matches!(*error, PipelineError::Overloaded))
        );
    }
    #[test]
    fn same_policy_label_cannot_hide_different_effective_limits() {
        let a = FetchPolicy::for_host("example.org");
        let mut b = a.clone();
        b.max_body_bytes /= 2;
        assert_ne!(PolicyKey::from(&a), PolicyKey::from(&b));
        let mut b = a.clone();
        b.request_timeout = Duration::from_secs(1);
        assert_ne!(PolicyKey::from(&a), PolicyKey::from(&b));
        let mut b = a.clone();
        b.allowed_hosts.clear();
        assert_ne!(PolicyKey::from(&a), PolicyKey::from(&b));
    }
    #[tokio::test]
    async fn cancelled_blocking_job_keeps_cpu_admission_until_exit() {
        let engine = Arc::new(Engine::new(1, 100));
        let worker_engine = Arc::clone(&engine);
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(0);
        let task = tokio::spawn(async move {
            worker_engine
                .run_blocking(move || {
                    started_tx.send(()).unwrap();
                    release_rx.recv().unwrap();
                })
                .await
        });
        started_rx.await.unwrap();
        task.abort();
        let _ = task.await;
        let remaining = engine.cpu_permits.available_permits();
        release_tx.send(()).unwrap();
        assert_eq!(remaining, 0);
    }
}
