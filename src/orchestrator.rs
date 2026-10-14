//! Query-independent source coalescing + query-dependent representation flights.
//! Distinct agent queries against the same authorized URL may share network+parse
//! while overlapping, without accidentally sharing wrong query-specific highlights.
//! An upstream enforcement service MUST authorize permissions/robots/rate BEFORE call.
use crate::{aae, network::{fetch_https, FetchPolicy, FetchError}, projection::{self, Projection},
            singleflight::{FlightError,SingleFlight}};
use std::{sync::Arc, time::{SystemTime, UNIX_EPOCH}};
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
    source_policy_id: String,
}
#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct SourceKey {
    url: String,
    tenant_partition: String,
    policy_id: String,
}
#[derive(Debug)]
pub enum PipelineError {
    BadUrl, Transport(FetchError), HttpStatus(u16), UnsupportedMediaType,
    InvalidUtf8, Parse(&'static str), Serialization, WorkerFailure, Overloaded,
    SharedSource(Arc<PipelineError>), SharedSourceCancelled,
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
    max_html_nodes: usize,
}
impl Engine {
    pub fn new(max_origin_inflight: usize, max_html_nodes: usize) -> Self {
        Self { source_flights:SingleFlight::new(),
            representation_flights: SingleFlight::new(),
            network_permits:Arc::new(Semaphore::new(max_origin_inflight.max(1))),
            max_html_nodes:max_html_nodes.max(1) }
    }

    /// Upstream source permission must be checked for EACH caller, including
    /// followers, before invoking this method; this is not an auth boundary.
    pub async fn retrieve(&self, key: RequestKey, policy: FetchPolicy)
      -> Result<Arc<Retrieval>, FlightError<PipelineError>> {
        let source_key = SourceKey { url: key.url.clone(),
            tenant_partition: key.tenant_partition.clone(),
            policy_id: policy.policy_id.clone() };
        let permits = Arc::clone(&self.network_permits);
        let max_nodes = self.max_html_nodes;
        let original = key.clone();
        let rep_key = RepresentationKey {
            request: key,
            source_policy_id: policy.policy_id.clone(),
        };
        self.representation_flights.call(rep_key, move || async move {
            if original.url.len() > 8192 || original.query.len() > 4096
                || original.tenant_partition.is_empty() { return Err(PipelineError::BadUrl); }
            let url = Url::parse(&original.url).map_err(|_| PipelineError::BadUrl)?;
            // The source is independent of query when anonymous and the same
            // source-policy+tenant+response variant applies to both calls.
            let raw_url = url.clone();
            let source = self.source_flights.call(source_key, move || async move {
                let _permit = permits.try_acquire_owned().map_err(|_| PipelineError::Overloaded)?;
                let response = fetch_https(&raw_url, &policy, None).await
                    .map_err(PipelineError::Transport)?;
                if response.status != 200 { return Err(PipelineError::HttpStatus(response.status)); }
                let content_type = response.content_type.as_deref().unwrap_or("");
                if !content_type.to_ascii_lowercase().starts_with("text/html") {
                    return Err(PipelineError::UnsupportedMediaType);
                }
                let html = std::str::from_utf8(&response.payload)
                    .map_err(|_| PipelineError::InvalidUtf8)?.to_owned();
                let bytes_downloaded = html.len();
                let observed_at_ms = SystemTime::now().duration_since(UNIX_EPOCH)
                    .unwrap_or_default().as_millis().min(u64::MAX as u128) as u64;
                let page_url=raw_url;
                let page = tokio::task::spawn_blocking(move ||
                    projection::parse_html(&html, &page_url, max_nodes, 1000)
                        .map_err(PipelineError::Parse)
                ).await.map_err(|_| PipelineError::WorkerFailure)??;
                Ok(SourceContent { page,status:response.status,
                    bytes_downloaded,observed_at_ms })
            }).await.map_err(|e| match e {
                FlightError::Fetch(source_error) => PipelineError::SharedSource(source_error),
                FlightError::Cancelled => PipelineError::SharedSourceCancelled,
            })?;
            let query=original.query;
            let page_source=Arc::clone(&source);
            let output = tokio::task::spawn_blocking(move ||
                aae::export_jsonl(&page_source.page,&url,page_source.observed_at_ms,
                    Some(&query), 6).map_err(|_| PipelineError::Serialization)
            ).await.map_err(|_| PipelineError::WorkerFailure)??;
            Ok(Retrieval { status:source.status, aae_jsonl:output,
                bytes_downloaded:source.bytes_downloaded })
        }).await
    }
}
