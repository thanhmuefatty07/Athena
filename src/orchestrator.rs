//! Query-independent source coalescing + query-dependent representation flights.
//! Distinct agent queries against the same authorized URL may share network+parse
//! while overlapping, without accidentally sharing wrong query-specific highlights.
//! An upstream enforcement service MUST authorize permissions/robots/rate BEFORE call.
use crate::{
    aae,
    network::{FetchError, FetchPolicy, fetch_https_with_admission, validate_request},
    projection::{self, Projection},
    rank::PreparedRank,
    singleflight::{FlightError, SingleFlight},
};
use std::{
    fmt,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicUsize, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::Semaphore;
use url::Url;

#[derive(Clone, Hash, PartialEq, Eq)]
pub struct RequestKey {
    pub url: String,
    pub tenant_partition: String,
    pub query: String,
    // In production also include response Vary headers, locale, auth/cookie
    // identity, representation, source policy version and freshness mode.
}

impl fmt::Debug for RequestKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RequestKey")
            .field("url", &"[REDACTED]")
            .field("tenant_partition", &"[REDACTED]")
            .field("query", &"[REDACTED]")
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct RetrieveOptions {
    pub include_links: bool,
    pub link_mode: aae::LinkMode,
    pub highlight_limit: usize,
    pub bypass_cache: bool,
}

impl RetrieveOptions {
    pub const fn effective_link_mode(&self) -> aae::LinkMode {
        if !self.include_links {
            aae::LinkMode::None
        } else {
            self.link_mode
        }
    }
}

impl Default for RetrieveOptions {
    fn default() -> Self {
        Self {
            include_links: true,
            link_mode: aae::LinkMode::All,
            highlight_limit: 6,
            bypass_cache: false,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct CacheConfig {
    pub max_entries: usize,
    pub max_bytes: usize,
    pub ttl: Duration,
}

impl CacheConfig {
    pub const fn disabled() -> Self {
        Self {
            max_entries: 0,
            max_bytes: 0,
            ttl: Duration::ZERO,
        }
    }
}

impl Default for CacheConfig {
    fn default() -> Self {
        Self {
            max_entries: 128,
            max_bytes: 16 * 1024 * 1024,
            ttl: Duration::from_secs(60),
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct EngineConfig {
    pub max_origin_inflight: usize,
    pub max_cpu_workers: usize,
    pub max_html_nodes: usize,
    pub max_pending: usize,
    pub cache: CacheConfig,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CacheStats {
    pub hits: usize,
    pub revalidated_304: usize,
    pub misses: usize,
    pub entries: usize,
    pub retained_bytes: usize,
}

#[derive(Debug, Clone, Hash, PartialEq, Eq)]
struct RepresentationKey {
    request: RequestKey,
    policy: Arc<PolicyKey>,
    link_mode: aae::LinkMode,
    highlight_limit: usize,
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
    SoftBlocked(&'static str),
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
    page: Arc<Projection>,
    prepared_rank: Arc<OnceLock<PreparedRank>>,
    query_evaluations: Arc<AtomicUsize>,
    etag: Option<String>,
    links_extracted: bool,
    status: u16,
    bytes_downloaded: usize,
    observed_at_ms: u64,
    approx_bytes: usize,
}

impl SourceContent {
    fn cached_hit(&self) -> Self {
        Self {
            page: Arc::clone(&self.page),
            prepared_rank: Arc::clone(&self.prepared_rank),
            query_evaluations: Arc::clone(&self.query_evaluations),
            etag: self.etag.clone(),
            links_extracted: self.links_extracted,
            status: self.status,
            bytes_downloaded: 0,
            observed_at_ms: self.observed_at_ms,
            approx_bytes: self.approx_bytes,
        }
    }
}

pub fn projection_retained_bytes(projection: &Projection) -> usize {
    let chunk_bytes: usize = projection
        .chunks
        .iter()
        .map(|c| {
            std::mem::size_of::<projection::Chunk>()
                .saturating_add(c.text.capacity())
                .saturating_add(c.links.capacity() * std::mem::size_of::<u32>())
        })
        .sum();
    let link_bytes: usize = projection
        .links
        .iter()
        .map(|l| std::mem::size_of::<String>().saturating_add(l.capacity()))
        .sum();
    projection
        .dom
        .approx_bytes()
        .saturating_add(chunk_bytes)
        .saturating_add(link_bytes)
}

pub fn cached_source_estimated_bytes(projection: &Projection) -> usize {
    let prepared_rank = PreparedRank::new(&projection.chunks);
    projection_retained_bytes(&projection) + prepared_rank.retained_payload_bytes()
}

const CACHE_SHARDS: usize = 16;

struct CachedSourceEntry {
    key: SourceKey,
    content: Arc<SourceContent>,
    cached_at: Instant,
    last_accessed: Instant,
}

struct SourceCache {
    config: CacheConfig,
    shards: [Mutex<Vec<CachedSourceEntry>>; CACHE_SHARDS],
    eviction_lock: Mutex<()>,
    hits: AtomicUsize,
    revalidated_304: AtomicUsize,
    misses: AtomicUsize,
    entries: AtomicUsize,
    retained_bytes: AtomicUsize,
}

impl SourceCache {
    fn new(config: CacheConfig) -> Self {
        Self {
            config,
            shards: std::array::from_fn(|_| Mutex::new(Vec::new())),
            eviction_lock: Mutex::new(()),
            hits: AtomicUsize::new(0),
            revalidated_304: AtomicUsize::new(0),
            misses: AtomicUsize::new(0),
            entries: AtomicUsize::new(0),
            retained_bytes: AtomicUsize::new(0),
        }
    }

    fn shard_index(key: &SourceKey) -> usize {
        use std::hash::{DefaultHasher, Hash, Hasher};
        let mut hasher = DefaultHasher::new();
        key.hash(&mut hasher);
        (hasher.finish() as usize) & (CACHE_SHARDS - 1)
    }

    fn is_enabled(&self) -> bool {
        self.config.max_entries > 0 && self.config.max_bytes > 0
    }

    fn lookup(
        &self,
        key: &SourceKey,
        need_links: bool,
    ) -> (Option<Arc<SourceContent>>, Option<Arc<SourceContent>>) {
        if !self.is_enabled() {
            return (None, None);
        }
        let now = Instant::now();
        let shard_idx = Self::shard_index(key);
        let Ok(mut guard) = self.shards[shard_idx].lock() else {
            return (None, None);
        };
        if let Some(pos) = guard.iter().position(|e| &e.key == key) {
            if need_links && !guard[pos].content.links_extracted {
                self.misses.fetch_add(1, Ordering::Relaxed);
                return (None, None);
            }
            let is_fresh = !self.config.ttl.is_zero()
                && now.duration_since(guard[pos].cached_at) < self.config.ttl;
            if is_fresh {
                let mut entry = guard.remove(pos);
                entry.last_accessed = now;
                let content = Arc::clone(&entry.content);
                guard.push(entry);
                self.hits.fetch_add(1, Ordering::Relaxed);
                return (Some(Arc::new(content.cached_hit())), None);
            }
            return (None, Some(Arc::clone(&guard[pos].content)));
        }
        self.misses.fetch_add(1, Ordering::Relaxed);
        (None, None)
    }

    fn record_revalidated(
        &self,
        key: &SourceKey,
        updated_etag: Option<String>,
    ) -> Option<Arc<SourceContent>> {
        if !self.is_enabled() {
            return None;
        }
        let now = Instant::now();
        let shard_idx = Self::shard_index(key);
        let Ok(mut guard) = self.shards[shard_idx].lock() else {
            return None;
        };
        if let Some(pos) = guard.iter().position(|e| &e.key == key) {
            let mut entry = guard.remove(pos);
            let mut hit = entry.content.cached_hit();
            if updated_etag.is_some() {
                hit.etag = updated_etag;
            }
            let refreshed = Arc::new(hit);
            entry.content = Arc::clone(&refreshed);
            entry.cached_at = now;
            entry.last_accessed = now;
            guard.push(entry);
            self.revalidated_304.fetch_add(1, Ordering::Relaxed);
            return Some(refreshed);
        }
        None
    }

    fn evict_oldest_across_shards(&self) -> bool {
        let mut oldest: Option<(Instant, std::sync::MutexGuard<'_, Vec<CachedSourceEntry>>)> = None;
        for shard in &self.shards {
            if let Ok(guard) = shard.lock()
                && let Some(first) = guard.first()
                && oldest
                    .as_ref()
                    .is_none_or(|(best_time, _)| first.last_accessed < *best_time)
            {
                oldest = Some((first.last_accessed, guard));
            }
        }
        if let Some((_, mut guard)) = oldest {
            let evicted = guard.remove(0);
            self.entries.fetch_sub(1, Ordering::Relaxed);
            self.retained_bytes
                .fetch_sub(evicted.content.approx_bytes, Ordering::Relaxed);
            return true;
        }
        false
    }

    fn insert(&self, key: SourceKey, content: Arc<SourceContent>) {
        if !self.is_enabled() || content.approx_bytes > self.config.max_bytes {
            return;
        }
        let now = Instant::now();
        let Ok(_evict_guard) = self.eviction_lock.lock() else {
            return;
        };
        let shard_idx = Self::shard_index(&key);
        if let Ok(mut guard) = self.shards[shard_idx].lock()
            && let Some(pos) = guard.iter().position(|e| e.key == key)
        {
            let existing_fresh = !self.config.ttl.is_zero()
                && now.duration_since(guard[pos].cached_at) < self.config.ttl;
            if guard[pos].content.links_extracted
                && !content.links_extracted
                && (existing_fresh
                    || guard[pos].content.observed_at_ms >= content.observed_at_ms
                    || (content.etag.is_some() && content.etag == guard[pos].content.etag))
            {
                return;
            }
            let removed = guard.remove(pos);
            self.entries.fetch_sub(1, Ordering::Relaxed);
            self.retained_bytes
                .fetch_sub(removed.content.approx_bytes, Ordering::Relaxed);
        }
        while self.entries.load(Ordering::Relaxed) > 0
            && (self.entries.load(Ordering::Relaxed) >= self.config.max_entries
                || self
                    .retained_bytes
                    .load(Ordering::Relaxed)
                    .saturating_add(content.approx_bytes)
                    > self.config.max_bytes)
        {
            if !self.evict_oldest_across_shards() {
                break;
            }
        }
        if let Ok(mut guard) = self.shards[shard_idx].lock() {
            self.entries.fetch_add(1, Ordering::Relaxed);
            self.retained_bytes
                .fetch_add(content.approx_bytes, Ordering::Relaxed);
            guard.push(CachedSourceEntry {
                key,
                content,
                cached_at: now,
                last_accessed: now,
            });
        }
    }

    fn stats(&self) -> CacheStats {
        CacheStats {
            hits: self.hits.load(Ordering::Relaxed),
            revalidated_304: self.revalidated_304.load(Ordering::Relaxed),
            misses: self.misses.load(Ordering::Relaxed),
            entries: self.entries.load(Ordering::Relaxed),
            retained_bytes: self.retained_bytes.load(Ordering::Relaxed),
        }
    }
}

#[cfg(test)]
type TestTransport = Arc<
    dyn Fn(
            &Url,
            Option<&str>,
            Arc<tokio::sync::OwnedSemaphorePermit>,
        ) -> std::pin::Pin<
            Box<
                dyn std::future::Future<Output = Result<crate::network::Fetched, FetchError>>
                    + Send,
            >,
        > + Send
        + Sync,
>;

pub struct Engine {
    source_flights: SingleFlight<SourceKey, SourceContent, PipelineError>,
    upgraded_source_flights: SingleFlight<SourceKey, SourceContent, PipelineError>,
    representation_flights: SingleFlight<RepresentationKey, Retrieval, PipelineError>,
    network_permits: Arc<Semaphore>,
    cpu_permits: Arc<Semaphore>,
    pending_permits: Semaphore,
    max_html_nodes: usize,
    cache: Arc<SourceCache>,
    #[cfg(test)]
    test_transport: Option<TestTransport>,
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
        Self::with_config(EngineConfig {
            max_origin_inflight,
            max_cpu_workers: max_origin_inflight,
            max_html_nodes,
            max_pending,
            cache: CacheConfig::disabled(),
        })
    }

    pub fn with_cache(
        max_origin_inflight: usize,
        max_html_nodes: usize,
        cache: CacheConfig,
    ) -> Self {
        let pending = max_origin_inflight
            .max(1)
            .saturating_mul(32)
            .min(Semaphore::MAX_PERMITS);
        Self::with_config(EngineConfig {
            max_origin_inflight,
            max_cpu_workers: max_origin_inflight,
            max_html_nodes,
            max_pending: pending,
            cache,
        })
    }

    pub fn with_config(config: EngineConfig) -> Self {
        Self {
            source_flights: SingleFlight::new(),
            upgraded_source_flights: SingleFlight::new(),
            representation_flights: SingleFlight::new(),
            network_permits: Arc::new(Semaphore::new(config.max_origin_inflight.max(1))),
            cpu_permits: Arc::new(Semaphore::new(config.max_cpu_workers.max(1))),
            pending_permits: Semaphore::new(config.max_pending),
            max_html_nodes: config.max_html_nodes.max(1),
            cache: Arc::new(SourceCache::new(config.cache)),
            #[cfg(test)]
            test_transport: None,
        }
    }

    pub fn cache_stats(&self) -> CacheStats {
        self.cache.stats()
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

    async fn resolve_source_flight(
        &self,
        flights: &SingleFlight<SourceKey, SourceContent, PipelineError>,
        source_key: SourceKey,
        raw_url: Url,
        policy: FetchPolicy,
        bypass_cache: bool,
        need_links: bool,
    ) -> Result<Arc<SourceContent>, PipelineError> {
        let permits = Arc::clone(&self.network_permits);
        let cache = Arc::clone(&self.cache);
        let max_nodes = self.max_html_nodes;
        flights
            .call(source_key.clone(), move || async move {
                let (fresh_hit, stale_candidate) = if bypass_cache {
                    (None, None)
                } else {
                    cache.lookup(&source_key, need_links)
                };
                if let Some(hit) = fresh_hit {
                    return Ok(hit.cached_hit());
                }
                let conditional_etag = stale_candidate
                    .as_ref()
                    .and_then(|entry| entry.etag.clone());
                let permit = Arc::new(
                    permits
                        .try_acquire_owned()
                        .map_err(|_| PipelineError::Overloaded)?,
                );
                #[cfg(test)]
                let response = if let Some(transport) = &self.test_transport {
                    transport(&raw_url, conditional_etag.as_deref(), Arc::clone(&permit))
                        .await
                        .map_err(PipelineError::Transport)?
                } else {
                    fetch_https_with_admission(
                        &raw_url,
                        &policy,
                        conditional_etag.as_deref(),
                        Some(Arc::clone(&permit)),
                    )
                    .await
                    .map_err(PipelineError::Transport)?
                };
                #[cfg(not(test))]
                let response = fetch_https_with_admission(
                    &raw_url,
                    &policy,
                    conditional_etag.as_deref(),
                    Some(Arc::clone(&permit)),
                )
                .await
                .map_err(PipelineError::Transport)?;
                // Release network admission immediately once HTTP bytes arrive,
                // before scheduling CPU-bound HTML projection on cpu_permits.
                drop(permit);
                if response.status == 304
                    && let Some(stale) = stale_candidate
                {
                    if let Some(revalidated) = cache.record_revalidated(&source_key, response.etag)
                    {
                        return Ok(revalidated.cached_hit());
                    }
                    return Ok(stale.cached_hit());
                }
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
                let response_etag = response.etag;
                let extract_links = need_links;
                let parse_limits = projection::ParseLimits {
                    max_html_bytes: policy.max_body_bytes,
                    max_text_bytes: policy.max_body_bytes,
                    max_nodes,
                    max_parser_nodes: max_nodes,
                    max_words_per_chunk: 1000,
                    max_link_bytes: policy.max_body_bytes,
                    include_links: extract_links,
                    ..projection::ParseLimits::default()
                };
                let observed_at_ms = SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_millis()
                    .min(u64::MAX as u128) as u64;
                let page_url = raw_url;
                let (page, prepared_rank, approx_bytes) = self
                    .run_blocking(move || {
                        let html = std::str::from_utf8(&response.payload)
                            .map_err(|_| PipelineError::InvalidUtf8)?;
                        if html.len() <= 65_536
                            && let Some(reason) = crate::network::detect_soft_block(html.as_bytes())
                        {
                            return Err(PipelineError::SoftBlocked(reason));
                        }
                        let page =
                            projection::parse_html_with_limits(html, &page_url, parse_limits)
                                .map_err(PipelineError::Parse)?;
                        if page.chunks.is_empty() {
                            return Err(PipelineError::SoftBlocked(
                                "empty_projected_chunks_js_shell",
                            ));
                        }
                        let prepared = OnceLock::new();
                        let approx = bytes_downloaded
                            .saturating_add(projection_retained_bytes(&page))
                            .max(64);
                        Ok::<_, PipelineError>((Arc::new(page), Arc::new(prepared), approx))
                    })
                    .await??;
                let content = SourceContent {
                    page,
                    prepared_rank,
                    query_evaluations: Arc::new(AtomicUsize::new(0)),
                    etag: response_etag,
                    links_extracted: extract_links,
                    status: response.status,
                    bytes_downloaded,
                    observed_at_ms,
                    approx_bytes,
                };
                if !bypass_cache && cache.is_enabled() {
                    cache.insert(source_key, Arc::new(content.cached_hit()));
                }
                Ok(content)
            })
            .await
            .map_err(|e| match e {
                FlightError::Fetch(source_error) => PipelineError::SharedSource(source_error),
                FlightError::Cancelled => PipelineError::SharedSourceCancelled,
            })
    }

    /// Upstream source permission must be checked for EACH caller, including
    /// followers, before invoking this method; this is not an auth boundary.
    pub async fn retrieve(
        &self,
        key: RequestKey,
        policy: FetchPolicy,
    ) -> Result<Arc<Retrieval>, FlightError<PipelineError>> {
        self.retrieve_with_options(key, policy, RetrieveOptions::default())
            .await
    }

    /// Upstream source permission must be checked for EACH caller, including
    /// followers, before invoking this method; this is not an auth boundary.
    pub async fn retrieve_with_options(
        &self,
        mut key: RequestKey,
        policy: FetchPolicy,
        options: RetrieveOptions,
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
        let original = key.clone();
        let effective_link_mode = options.effective_link_mode();
        let need_links = effective_link_mode != aae::LinkMode::None;
        let rep_key = RepresentationKey {
            request: key,
            policy: policy_key,
            link_mode: effective_link_mode,
            highlight_limit: options.highlight_limit,
        };
        let deadline = policy.request_timeout;
        tokio::time::timeout(
            deadline,
            self.representation_flights
                .call(rep_key, move || async move {
                    // The source is independent of query when anonymous and the same
                    // source-policy+tenant+response variant applies to both calls.
                    let mut source = self
                        .resolve_source_flight(
                            &self.source_flights,
                            source_key.clone(),
                            url.clone(),
                            policy.clone(),
                            options.bypass_cache,
                            need_links,
                        )
                        .await?;
                    if need_links && !source.links_extracted {
                        source = self
                            .resolve_source_flight(
                                &self.upgraded_source_flights,
                                source_key,
                                url.clone(),
                                policy,
                                options.bypass_cache,
                                true,
                            )
                            .await?;
                    }
                    let query = original.query;
                    let page_source = Arc::clone(&source);
                    let output = self
                        .run_blocking(move || {
                            let eval_idx = page_source
                                .query_evaluations
                                .fetch_add(1, Ordering::Relaxed);
                            let prepared_ref =
                                if eval_idx > 0 {
                                    Some(page_source.prepared_rank.get_or_init(|| {
                                        PreparedRank::new(&page_source.page.chunks)
                                    }))
                                } else {
                                    page_source.prepared_rank.get()
                                };
                            aae::export_jsonl_with_link_mode_and_prepared(
                                &page_source.page,
                                &url,
                                page_source.observed_at_ms,
                                Some(&query),
                                options.highlight_limit,
                                16 * 1024 * 1024,
                                effective_link_mode,
                                prepared_ref,
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
    use bytes::Bytes;
    fn request(query: &str) -> RequestKey {
        RequestKey {
            url: "https://example.org/".into(),
            tenant_partition: "tenant".into(),
            query: query.into(),
        }
    }
    #[test]
    fn request_key_debug_redacts_url_tenant_and_query() {
        let secret = "athena-request-secret-sentinel";
        let key = RequestKey {
            url: format!("https://example.org/path?token={secret}"),
            tenant_partition: format!("tenant-{secret}"),
            query: format!("search-{secret}"),
        };

        let debug = format!("{key:?}");
        assert!(debug.contains("RequestKey"));
        assert!(!debug.contains(secret), "Debug output exposed request data");
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
    #[tokio::test]
    async fn network_permit_is_released_before_cpu_blocking_parse() {
        let mut engine = Engine::new(1, 100);
        let (net_done_tx, net_done_rx) = tokio::sync::oneshot::channel();
        let net_done_tx = Arc::new(Mutex::new(Some(net_done_tx)));
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            let tx = Arc::clone(&net_done_tx);
            Box::pin(async move {
                if let Some(sender) = tx.lock().unwrap().take() {
                    let _ = sender.send(());
                }
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html; charset=utf-8".into()),
                    etag: Some("\"v1\"".into()),
                    payload: Bytes::from_static(b"<main><p>Hello world</p></main>"),
                })
            })
        }));
        let engine = Arc::new(engine);
        let blocker_engine = Arc::clone(&engine);
        let (cpu_started_tx, cpu_started_rx) = tokio::sync::oneshot::channel();
        let (cpu_release_tx, cpu_release_rx) = std::sync::mpsc::sync_channel(0);
        let blocker = tokio::spawn(async move {
            blocker_engine
                .run_blocking(move || {
                    cpu_started_tx.send(()).unwrap();
                    cpu_release_rx.recv().unwrap();
                })
                .await
        });
        cpu_started_rx.await.unwrap();
        let retrieve_engine = Arc::clone(&engine);
        let policy = FetchPolicy::for_host("example.org");
        let call = tokio::spawn(async move {
            retrieve_engine
                .retrieve(request("Hello"), policy)
                .await
                .unwrap()
        });
        net_done_rx.await.unwrap();
        tokio::task::yield_now().await;
        // Network permit must be released even while CPU worker is still blocked!
        assert_eq!(engine.network_permits.available_permits(), 1);
        cpu_release_tx.send(()).unwrap();
        blocker.await.unwrap().unwrap();
        let out = call.await.unwrap();
        assert_eq!(out.status, 200);
    }
    #[tokio::test]
    async fn source_cache_ttl_hit_and_etag_304_revalidation_zero_network_bytes() {
        let mut engine = Engine::with_cache(
            2,
            100,
            CacheConfig {
                max_entries: 8,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_millis(40),
            },
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_ref = Arc::clone(&calls);
        engine.test_transport = Some(Arc::new(move |url, etag, _permit| {
            let url = url.clone();
            let call_no = calls_ref.fetch_add(1, Ordering::SeqCst);
            let etag = etag.map(str::to_owned);
            Box::pin(async move {
                if call_no > 0 && etag.as_deref() == Some("\"etag-v1\"") {
                    return Ok(crate::network::Fetched {
                        final_url: url,
                        status: 304,
                        content_type: None,
                        etag: Some("\"etag-v1\"".into()),
                        payload: Bytes::new(),
                    });
                }
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"etag-v1\"".into()),
                    payload: Bytes::from_static(
                        b"<main><h1>Athena</h1><p>Read <a href='/docs'>docs</a> and <a href='/api'>api</a>.</p></main>",
                    ),
                })
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        let first = engine
            .retrieve(request("docs"), policy.clone())
            .await
            .unwrap();
        assert!(first.bytes_downloaded > 0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        // Fresh TTL hit on a distinct query: 0 network calls, 0 bytes downloaded, PreparedRank reused!
        let second = engine
            .retrieve(request("Athena"), policy.clone())
            .await
            .unwrap();
        assert_eq!(second.bytes_downloaded, 0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(engine.cache_stats().hits, 1);

        // Wait for TTL expiry -> conditional If-None-Match -> 304 Not Modified revalidation!
        tokio::time::sleep(Duration::from_millis(55)).await;
        let third = engine
            .retrieve_with_options(
                request("api"),
                policy.clone(),
                RetrieveOptions {
                    include_links: false,
                    ..RetrieveOptions::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(third.bytes_downloaded, 0);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(engine.cache_stats().revalidated_304, 1);
        assert!(third.aae_jsonl.len() < first.aae_jsonl.len());
    }

    #[tokio::test]
    async fn concurrent_linkless_leader_does_not_strip_links_from_linked_follower() {
        let mut engine = Engine::new(2, 100);
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = tokio::sync::oneshot::channel();
        let started_tx = Arc::new(Mutex::new(Some(started_tx)));
        let release_rx = Arc::new(Mutex::new(Some(release_rx)));
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            let started = Arc::clone(&started_tx);
            let release = Arc::clone(&release_rx);
            Box::pin(async move {
                if let Some(tx) = started.lock().unwrap().take() {
                    let _ = tx.send(());
                }
                let rx_opt = release.lock().unwrap().take();
                if let Some(rx) = rx_opt {
                    let _ = rx.await;
                }
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"v1\"".into()),
                    payload: Bytes::from_static(
                        b"<main><p>Read <a href='https://example.org/docs'>docs</a> now.</p></main>",
                    ),
                })
            })
        }));
        let engine = Arc::new(engine);
        let policy = FetchPolicy::for_host("example.org");
        let e1 = Arc::clone(&engine);
        let p1 = policy.clone();
        let no_links_task = tokio::spawn(async move {
            e1.retrieve_with_options(
                request("docs"),
                p1,
                RetrieveOptions {
                    include_links: false,
                    ..RetrieveOptions::default()
                },
            )
            .await
            .unwrap()
        });
        started_rx.await.unwrap();
        let e2 = Arc::clone(&engine);
        let p2 = policy.clone();
        let with_links_task = tokio::spawn(async move {
            e2.retrieve_with_options(request("docs"), p2, RetrieveOptions::default())
                .await
                .unwrap()
        });
        tokio::task::yield_now().await;
        let _ = release_tx.send(());
        let no_links_res = no_links_task.await.unwrap();
        let with_links_res = with_links_task.await.unwrap();
        assert!(!no_links_res.aae_jsonl.contains("\"type\":\"link\""));
        assert!(
            with_links_res
                .aae_jsonl
                .contains("https://example.org/docs")
        );
    }

    #[tokio::test]
    async fn cached_engine_skips_link_extraction_when_include_links_false_and_upgrades_on_demand() {
        let mut engine = Engine::with_cache(
            2,
            100,
            CacheConfig {
                max_entries: 16,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_secs(30),
            },
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_ref = Arc::clone(&calls);
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            calls_ref.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move {
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"etag-links\"".into()),
                    payload: Bytes::from_static(
                        b"<main><p>Reference <a href='https://example.org/ref'>ref</a></p></main>",
                    ),
                })
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        let first = engine
            .retrieve_with_options(
                request("Reference"),
                policy.clone(),
                RetrieveOptions {
                    include_links: false,
                    ..RetrieveOptions::default()
                },
            )
            .await
            .unwrap();
        assert!(!first.aae_jsonl.contains("\"type\":\"link\""));
        assert_eq!(calls.load(Ordering::SeqCst), 1);

        // Second linkless call hits the linkless cached entry with 0 network calls:
        let second = engine
            .retrieve_with_options(
                request("ref"),
                policy.clone(),
                RetrieveOptions {
                    include_links: false,
                    ..RetrieveOptions::default()
                },
            )
            .await
            .unwrap();
        assert_eq!(second.bytes_downloaded, 0);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert_eq!(engine.cache_stats().hits, 1);

        // Third call with include_links: true upgrades the cached entry with links:
        let third = engine
            .retrieve(request("Reference"), policy.clone())
            .await
            .unwrap();
        assert!(third.aae_jsonl.contains("https://example.org/ref"));
        assert_eq!(calls.load(Ordering::SeqCst), 2);
        assert_eq!(engine.cache_stats().entries, 1);
    }

    #[tokio::test]
    async fn sharded_source_cache_enforces_global_max_entries_across_shards() {
        let mut engine = Engine::with_cache(
            4,
            100,
            CacheConfig {
                max_entries: 5,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_secs(60),
            },
        );
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            Box::pin(async move {
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"v1\"".into()),
                    payload: Bytes::from_static(b"<main><p>Shard test payload</p></main>"),
                })
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        for i in 0..24 {
            let key = RequestKey {
                url: format!("https://example.org/page-{i}"),
                tenant_partition: "tenant".into(),
                query: "Shard".into(),
            };
            let out = engine.retrieve(key, policy.clone()).await.unwrap();
            assert_eq!(out.status, 200);
            assert!(engine.cache_stats().entries <= 5);
        }
        assert_eq!(engine.cache_stats().entries, 5);
    }

    #[tokio::test]
    async fn orch_01_02_linkless_insert_preserves_linked_entry_and_lazy_prepared_rank() {
        let mut engine = Engine::with_cache(
            2,
            100,
            CacheConfig {
                max_entries: 4,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_secs(60),
            },
        );
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            Box::pin(async move {
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"v1\"".into()),
                    payload: Bytes::from_static(
                        b"<main><p>Consensus <a href='https://example.org/paper'>paper</a></p></main>",
                    ),
                })
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        let first = engine
            .retrieve(request("Consensus"), policy.clone())
            .await
            .unwrap();
        assert!(first.aae_jsonl.contains("https://example.org/paper"));

        let source_key = SourceKey {
            url: request("Consensus").url,
            tenant_partition: "tenant".into(),
            policy: Arc::new(PolicyKey::from(&policy)),
        };
        // ORCH-02: First evaluation must keep PreparedRank uninitialized (lazy until eval_idx > 0)
        let (cached_after_first, _) = engine.cache.lookup(&source_key, true);
        let cached_after_first = cached_after_first.expect("linked cache entry must exist");
        assert!(
            cached_after_first.prepared_rank.get().is_none(),
            "PreparedRank must not be eagerly constructed on the first query evaluation"
        );

        // ORCH-01: Attempting to insert a slower/stale linkless entry for the same key must NOT downgrade the linked entry
        let mut linkless_clone = cached_after_first.cached_hit();
        linkless_clone.links_extracted = false;
        engine
            .cache
            .insert(source_key.clone(), Arc::new(linkless_clone));
        let (still_linked, _) = engine.cache.lookup(&source_key, true);
        assert!(
            still_linked.is_some(),
            "linkless insert must not downgrade an existing fresh linked cache entry"
        );

        // Second evaluation initializes PreparedRank lazily:
        let _second = engine.retrieve(request("paper"), policy).await.unwrap();
        let (cached_after_second, _) = engine.cache.lookup(&source_key, true);
        assert!(
            cached_after_second.unwrap().prepared_rank.get().is_some(),
            "PreparedRank must be initialized on the second evaluation"
        );
    }

    #[tokio::test]
    async fn soft_blocked_http_200_challenge_does_not_poison_source_cache() {
        let mut engine = Engine::with_cache(
            2,
            100,
            CacheConfig {
                max_entries: 4,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_secs(60),
            },
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_ref = Arc::clone(&calls);
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            let call_idx = calls_ref.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move {
                if call_idx == 0 {
                    Ok(crate::network::Fetched {
                        final_url: url,
                        status: 200,
                        content_type: Some("text/html".into()),
                        etag: Some("\"cf-trap\"".into()),
                        payload: Bytes::from_static(
                            b"<html><head><title>Just a moment...</title></head><body><div class=\"cf-turnstile\">Verify</div></body></html>",
                        ),
                    })
                } else {
                    Ok(crate::network::Fetched {
                        final_url: url,
                        status: 200,
                        content_type: Some("text/html".into()),
                        etag: Some("\"real-v1\"".into()),
                        payload: Bytes::from_static(
                            b"<main><article><h1>Real Consensus Paper</h1><p>Byzantine quorum safety verified.</p></article></main>",
                        ),
                    })
                }
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        let first_attempt = engine.retrieve(request("Byzantine"), policy.clone()).await;
        assert!(
            first_attempt.is_err(),
            "HTTP 200 soft-block must be rejected"
        );
        assert_eq!(
            engine.cache_stats().entries,
            0,
            "Soft-block must not poison SourceCache"
        );

        let second_attempt = engine.retrieve(request("Byzantine"), policy).await.unwrap();
        assert!(
            second_attempt
                .aae_jsonl
                .contains("Byzantine quorum safety verified")
        );
        assert_eq!(engine.cache_stats().entries, 1);
    }

    #[tokio::test]
    async fn zero_byte_and_empty_js_shell_http_200_do_not_poison_source_cache() {
        let mut engine = Engine::with_cache(
            2,
            100,
            CacheConfig {
                max_entries: 4,
                max_bytes: 1024 * 1024,
                ttl: Duration::from_secs(60),
            },
        );
        let calls = Arc::new(AtomicUsize::new(0));
        let calls_ref = Arc::clone(&calls);
        engine.test_transport = Some(Arc::new(move |url, _etag, _permit| {
            let url = url.clone();
            let call_idx = calls_ref.fetch_add(1, Ordering::SeqCst);
            Box::pin(async move {
                let payload = match call_idx {
                    0 => Bytes::new(),
                    1 => Bytes::from_static(
                        b"<html><body><div id=\"app\"></div><script src=\"/bundle.js\"></script></body></html>",
                    ),
                    _ => Bytes::from_static(
                        b"<main><p>Hello world</p></main>",
                    ),
                };
                Ok(crate::network::Fetched {
                    final_url: url,
                    status: 200,
                    content_type: Some("text/html".into()),
                    etag: Some("\"etag-v2\"".into()),
                    payload,
                })
            })
        }));
        let policy = FetchPolicy::for_host("example.org");
        assert!(engine.retrieve(request("Hello"), policy.clone()).await.is_err());
        assert_eq!(engine.cache_stats().entries, 0);
        assert!(engine.retrieve(request("Hello"), policy.clone()).await.is_err());
        assert_eq!(engine.cache_stats().entries, 0);
        let ok = engine.retrieve(request("Hello"), policy).await.unwrap();
        assert!(ok.aae_jsonl.contains("Hello world"));
        assert_eq!(engine.cache_stats().entries, 1);
    }
}

