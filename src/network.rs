//! HTTPS-only, public-DNS-pinned baseline dialer. Correctness-first, not a global pool.
//! Caller MUST check source permission/robots via its policy service before calling.
//! No redirects, proxy inheritance, cookies, client auth or unbounded response bodies.
use crate::ip_policy::is_public_candidate;
use bytes::{Bytes, BytesMut};
use futures_util::StreamExt;
use reqwest::header::{CONTENT_ENCODING, CONTENT_TYPE, ETAG, IF_NONE_MATCH};
use std::{
    collections::HashSet,
    fmt,
    net::{SocketAddr, ToSocketAddrs},
    sync::Arc,
    time::Duration,
};
use tokio::sync::OwnedSemaphorePermit;
use url::Url;

async fn blocking_resolve<R, F>(
    admission: Option<Arc<OwnedSemaphorePermit>>,
    lookup: F,
) -> Result<R, FetchError>
where
    R: Send + 'static,
    F: FnOnce() -> std::io::Result<R> + Send + 'static,
{
    tokio::task::spawn_blocking(move || {
        // OS resolution cannot be forcibly cancelled once its blocking job starts.
        let _admission = admission;
        lookup()
    })
    .await
    .map_err(|_| FetchError::ResolveFailed)?
    .map_err(|_| FetchError::ResolveFailed)
}

#[derive(Debug)]
pub enum FetchError {
    InvalidUrl,
    DeniedHost,
    DeniedAddress,
    ResolveFailed,
    TooManyAddresses,
    TooLarge,
    UnsupportedContentEncoding,
    InvalidValidator,
    InvalidPolicy,
    Timeout,
    Network(reqwest::Error),
}
impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for FetchError {}

#[derive(Debug, Clone)]
pub struct FetchPolicy {
    /// Exact DNS names after URL IDNA normalization; never wildcards in this MVP.
    pub allowed_hosts: HashSet<String>,
    pub policy_id: String,
    pub max_body_bytes: usize,
    pub max_dns_answers: usize,
    pub connect_timeout: Duration,
    pub request_timeout: Duration,
}
impl FetchPolicy {
    pub fn for_host(host: &str) -> Self {
        Self {
            allowed_hosts: HashSet::from([host.to_ascii_lowercase()]),
            policy_id: format!("host-only-v1:{}", host.to_ascii_lowercase()),
            max_body_bytes: 4 * 1024 * 1024,
            max_dns_answers: 16,
            connect_timeout: Duration::from_secs(3),
            request_timeout: Duration::from_secs(12),
        }
    }
}
#[derive(Debug, Clone)]
pub struct Fetched {
    pub final_url: Url,
    pub status: u16,
    pub content_type: Option<String>,
    pub etag: Option<String>,
    pub payload: Bytes,
}

fn verified_host<'a>(url: &'a Url, policy: &FetchPolicy) -> Result<&'a str, FetchError> {
    if url.scheme() != "https"
        || url.port_or_known_default() != Some(443)
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(FetchError::InvalidUrl);
    }
    let host = match url.host() {
        Some(url::Host::Domain(host)) => host,
        Some(_) => return Err(FetchError::DeniedHost),
        None => return Err(FetchError::InvalidUrl),
    };
    if !policy.allowed_hosts.contains(host) {
        return Err(FetchError::DeniedHost);
    }
    Ok(host)
}

pub(crate) fn validate_request(url: &Url, policy: &FetchPolicy) -> Result<(), FetchError> {
    verified_host(url, policy)?;
    if policy.max_body_bytes == 0
        || policy.max_dns_answers == 0
        || policy.connect_timeout.is_zero()
        || policy.request_timeout.is_zero()
    {
        return Err(FetchError::InvalidPolicy);
    }
    Ok(())
}

/// Low-level dialer; callers own concurrency admission. Engine also guards DNS jobs.
pub async fn fetch_https(
    url: &Url,
    policy: &FetchPolicy,
    conditional_etag: Option<&str>,
) -> Result<Fetched, FetchError> {
    fetch_https_with_admission(url, policy, conditional_etag, None).await
}

pub(crate) async fn fetch_https_with_admission(
    url: &Url,
    policy: &FetchPolicy,
    conditional_etag: Option<&str>,
    admission: Option<Arc<OwnedSemaphorePermit>>,
) -> Result<Fetched, FetchError> {
    validate_request(url, policy)?;
    let validator = conditional_etag
        .map(reqwest::header::HeaderValue::from_str)
        .transpose()
        .map_err(|_| FetchError::InvalidValidator)?;
    tokio::time::timeout(
        policy.request_timeout,
        fetch_pinned(url, policy, validator, admission),
    )
    .await
    .map_err(|_| FetchError::Timeout)?
}

async fn fetch_pinned(
    url: &Url,
    policy: &FetchPolicy,
    validator: Option<reqwest::header::HeaderValue>,
    admission: Option<Arc<OwnedSemaphorePermit>>,
) -> Result<Fetched, FetchError> {
    let host = verified_host(url, policy)?;
    // Validate EVERY answer and pin ONLY validated socket addresses to the actual request.
    // Never validate with one DNS lookup and let reqwest perform a second uncontrolled lookup.
    let resolver_host = host.to_owned();
    let answers = blocking_resolve(admission, move || {
        (resolver_host.as_str(), 443).to_socket_addrs()
    })
    .await?;
    let mut seen = HashSet::new();
    let mut addrs: Vec<SocketAddr> = Vec::new();
    for addr in answers {
        if !is_public_candidate(addr.ip()) {
            return Err(FetchError::DeniedAddress);
        }
        if seen.contains(&addr) {
            continue;
        }
        if addrs.len() == policy.max_dns_answers {
            return Err(FetchError::TooManyAddresses);
        }
        seen.insert(addr);
        addrs.push(addr);
    }
    addrs.sort_unstable();
    if addrs.is_empty() {
        return Err(FetchError::ResolveFailed);
    }
    // New client per validated resolution has a pooling cost; a production resolver-aware
    // connector should reuse pools keyed by host+validated IP+policy epoch.
    let client = reqwest::Client::builder()
        .https_only(true)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(policy.connect_timeout)
        .timeout(policy.request_timeout)
        .resolve_to_addrs(host, &addrs)
        .user_agent("AthenaPermittedFetcher/0.1 (contact: operator-configured)")
        .build()
        .map_err(FetchError::Network)?;
    let mut request = client
        .get(url.clone())
        .header(reqwest::header::ACCEPT_ENCODING, "identity")
        .header(
            reqwest::header::ACCEPT,
            "text/html,application/json;q=0.9,text/plain;q=0.5",
        );
    if let Some(value) = validator {
        request = request.header(IF_NONE_MATCH, value);
    }
    let response = request.send().await.map_err(FetchError::Network)?;
    let status = response.status().as_u16();
    let content_type = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let etag = response
        .headers()
        .get(ETAG)
        .and_then(|v| v.to_str().ok())
        .map(str::to_owned);
    let encoding = response
        .headers()
        .get(CONTENT_ENCODING)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("identity");
    if !encoding.eq_ignore_ascii_case("identity") {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    if let Some(len) = response.content_length()
        && len > policy.max_body_bytes as u64
    {
        return Err(FetchError::TooLarge);
    }
    // A non-2xx response is observable evidence (including 304 and 429), not
    // automatically a successful content fetch. Upper layer classifies it.
    let mut stream = response.bytes_stream();
    let mut data = BytesMut::new();
    while let Some(next) = stream.next().await {
        let part = next.map_err(FetchError::Network)?;
        if part.len() > policy.max_body_bytes.saturating_sub(data.len()) {
            return Err(FetchError::TooLarge);
        }
        data.extend_from_slice(&part);
    }
    Ok(Fetched {
        final_url: url.clone(),
        status,
        content_type,
        etag,
        payload: data.freeze(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn cancelled_dns_worker_retains_origin_admission_until_exit() {
        let semaphore = Arc::new(tokio::sync::Semaphore::new(1));
        let permit = Arc::new(Arc::clone(&semaphore).try_acquire_owned().unwrap());
        let (started_tx, started_rx) = tokio::sync::oneshot::channel();
        let (release_tx, release_rx) = std::sync::mpsc::sync_channel(0);
        let task = tokio::spawn(async move {
            blocking_resolve(Some(permit), move || {
                started_tx.send(()).unwrap();
                release_rx.recv().unwrap();
                Ok(())
            })
            .await
        });
        started_rx.await.unwrap();
        task.abort();
        let _ = task.await;
        let remaining = semaphore.available_permits();
        release_tx.send(()).unwrap();
        assert_eq!(remaining, 0);
    }
    #[tokio::test]
    async fn invalid_validator_is_rejected_before_dns() {
        let policy = FetchPolicy::for_host("localhost");
        let url = Url::parse("https://localhost/").unwrap();
        assert!(matches!(
            fetch_https(&url, &policy, Some("bad\r\nheader")).await,
            Err(FetchError::InvalidValidator)
        ));
    }
    #[test]
    fn ipv6_literals_are_denied_even_if_listed_as_hosts() {
        let policy = FetchPolicy::for_host("[::1]");
        assert!(matches!(
            verified_host(&Url::parse("https://[::1]/").unwrap(), &policy),
            Err(FetchError::DeniedHost)
        ));
    }
    #[test]
    fn whitelist_and_scheme() {
        let p = FetchPolicy::for_host("example.com");
        assert!(verified_host(&Url::parse("https://example.com/a").unwrap(), &p).is_ok());
        assert!(verified_host(&Url::parse("http://example.com/a").unwrap(), &p).is_err());
        assert!(verified_host(&Url::parse("https://evil.com/").unwrap(), &p).is_err());
        assert!(verified_host(&Url::parse("https://127.0.0.1/").unwrap(), &p).is_err());
        assert!(verified_host(&Url::parse("https://example.com:444/").unwrap(), &p).is_err());
        assert!(
            verified_host(
                &Url::parse("https://user:password@example.com/").unwrap(),
                &p
            )
            .is_err()
        );
    }
}
