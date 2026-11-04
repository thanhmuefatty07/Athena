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
    TlsProviderInitialization,
    Timeout,
    Network(reqwest::Error),
}

impl FetchError {
    fn diagnostic_name(&self) -> &'static str {
        match self {
            Self::InvalidUrl => "InvalidUrl",
            Self::DeniedHost => "DeniedHost",
            Self::DeniedAddress => "DeniedAddress",
            Self::ResolveFailed => "ResolveFailed",
            Self::TooManyAddresses => "TooManyAddresses",
            Self::TooLarge => "TooLarge",
            Self::UnsupportedContentEncoding => "UnsupportedContentEncoding",
            Self::InvalidValidator => "InvalidValidator",
            Self::InvalidPolicy => "InvalidPolicy",
            Self::TlsProviderInitialization => "TlsProviderInitialization",
            Self::Timeout => "Timeout",
            Self::Network(_) => "Network",
        }
    }
}

impl fmt::Debug for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.diagnostic_name())
    }
}

impl fmt::Display for FetchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.diagnostic_name())
    }
}
impl std::error::Error for FetchError {}

#[cfg(feature = "tls-ring")]
pub fn initialize_tls_provider() -> Result<(), &'static str> {
    static INITIALIZED: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    let initialized = INITIALIZED.get_or_init(|| {
        if rustls::crypto::CryptoProvider::get_default().is_some() {
            return true;
        }
        rustls::crypto::ring::default_provider()
            .install_default()
            .is_ok()
            || rustls::crypto::CryptoProvider::get_default().is_some()
    });
    if *initialized {
        Ok(())
    } else {
        Err("could not install the configured ring TLS provider")
    }
}

#[cfg(feature = "tls-aws-lc")]
pub fn initialize_tls_provider() -> Result<(), &'static str> {
    Ok(())
}

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

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct ClientPoolKey {
    policy_id: String,
    host: String,
    addrs: Vec<SocketAddr>,
    connect_timeout: Duration,
    request_timeout: Duration,
}

struct PooledClientEntry {
    key: ClientPoolKey,
    client: reqwest::Client,
    created_at: std::time::Instant,
}

const MAX_POOLED_CLIENTS: usize = 64;
const CLIENT_POOL_TTL: Duration = Duration::from_secs(60);

fn pooled_pinned_client(
    policy: &FetchPolicy,
    host: &str,
    addrs: &[SocketAddr],
) -> Result<reqwest::Client, FetchError> {
    static POOL: std::sync::OnceLock<std::sync::Mutex<Vec<PooledClientEntry>>> =
        std::sync::OnceLock::new();
    let key = ClientPoolKey {
        policy_id: policy.policy_id.clone(),
        host: host.to_owned(),
        addrs: addrs.to_vec(),
        connect_timeout: policy.connect_timeout,
        request_timeout: policy.request_timeout,
    };
    let now = std::time::Instant::now();
    let pool = POOL.get_or_init(|| std::sync::Mutex::new(Vec::new()));
    if let Ok(mut guard) = pool.lock() {
        guard.retain(|entry| now.duration_since(entry.created_at) < CLIENT_POOL_TTL);
        if let Some(pos) = guard.iter().position(|entry| entry.key == key) {
            let entry = guard.remove(pos);
            let client = entry.client.clone();
            guard.push(entry);
            return Ok(client);
        }
    }
    let mut builder = reqwest::Client::builder()
        .https_only(true)
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(policy.connect_timeout)
        .timeout(policy.request_timeout)
        .pool_max_idle_per_host(4)
        .pool_idle_timeout(CLIENT_POOL_TTL)
        .user_agent("AthenaPermittedFetcher/0.1 (contact: operator-configured)");
    if !addrs.is_empty() {
        builder = builder.resolve_to_addrs(host, addrs);
    }
    let client = builder
        .build()
        .map_err(|error| FetchError::Network(error.without_url()))?;
    if let Ok(mut guard) = pool.lock() {
        guard.retain(|entry| {
            now.duration_since(entry.created_at) < CLIENT_POOL_TTL && entry.key != key
        });
        if guard.len() >= MAX_POOLED_CLIENTS {
            guard.remove(0);
        }
        guard.push(PooledClientEntry {
            key,
            client: client.clone(),
            created_at: now,
        });
    }
    Ok(client)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentEncodingMode {
    Identity,
    Gzip,
    Deflate,
}

impl ContentEncodingMode {
    pub fn parse(raw: Option<&str>) -> Result<Self, FetchError> {
        let value = raw.unwrap_or("identity").trim();
        if value.is_empty() || value.eq_ignore_ascii_case("identity") {
            Ok(Self::Identity)
        } else if value.eq_ignore_ascii_case("gzip") || value.eq_ignore_ascii_case("x-gzip") {
            Ok(Self::Gzip)
        } else if value.eq_ignore_ascii_case("deflate") {
            Ok(Self::Deflate)
        } else {
            Err(FetchError::UnsupportedContentEncoding)
        }
    }
}

/// Pre-allocation bounded output buffer that enforces `max_body_bytes` on every
/// decompressed byte or back-reference slice before allocation or copying.
pub struct BoundedOutputBuf {
    buf: BytesMut,
    max_bytes: usize,
}

impl BoundedOutputBuf {
    pub fn new(max_bytes: usize) -> Self {
        Self {
            buf: BytesMut::with_capacity(max_bytes.min(16 * 1024)),
            max_bytes,
        }
    }

    pub fn len(&self) -> usize {
        self.buf.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buf.is_empty()
    }

    pub fn push_byte(&mut self, byte: u8) -> Result<(), FetchError> {
        if self.buf.len() >= self.max_bytes {
            return Err(FetchError::TooLarge);
        }
        self.buf.extend_from_slice(&[byte]);
        Ok(())
    }

    pub fn push_slice(&mut self, slice: &[u8]) -> Result<(), FetchError> {
        if slice.len() > self.max_bytes.saturating_sub(self.buf.len()) {
            return Err(FetchError::TooLarge);
        }
        self.buf.extend_from_slice(slice);
        Ok(())
    }

    pub fn copy_backref(&mut self, distance: usize, length: usize) -> Result<(), FetchError> {
        if distance == 0 || distance > self.buf.len() {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        if length > self.max_bytes.saturating_sub(self.buf.len()) {
            return Err(FetchError::TooLarge);
        }
        self.buf.reserve(length);
        if distance == 1 {
            let b = self.buf[self.buf.len() - 1];
            self.buf.resize(self.buf.len() + length, b);
        } else if distance >= length {
            let start = self.buf.len() - distance;
            self.buf.extend_from_within(start..start + length);
        } else {
            for _ in 0..length {
                let idx = self.buf.len() - distance;
                let b = self.buf[idx];
                self.buf.extend_from_slice(&[b]);
            }
        }
        Ok(())
    }

    pub fn freeze(self) -> Bytes {
        self.buf.freeze()
    }
}

struct BitReader<'a> {
    data: &'a [u8],
    byte_pos: usize,
    bit_buf: u64,
    bit_count: u8,
}

impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self {
            data,
            byte_pos: 0,
            bit_buf: 0,
            bit_count: 0,
        }
    }

    fn fill(&mut self) {
        while self.bit_count <= 56 && self.byte_pos < self.data.len() {
            self.bit_buf |= u64::from(self.data[self.byte_pos]) << self.bit_count;
            self.byte_pos += 1;
            self.bit_count += 8;
        }
    }

    fn read_bits(&mut self, n: u8) -> Result<u32, FetchError> {
        if n == 0 {
            return Ok(0);
        }
        self.fill();
        if self.bit_count < n {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        let mask = (1u64 << n) - 1;
        let val = (self.bit_buf & mask) as u32;
        self.bit_buf >>= n;
        self.bit_count -= n;
        Ok(val)
    }

    fn read_aligned_bytes(&mut self, len: usize) -> Result<&'a [u8], FetchError> {
        let drop_bits = self.bit_count & 7;
        self.bit_buf >>= drop_bits;
        self.bit_count -= drop_bits;
        let rewind_bytes = usize::from(self.bit_count / 8);
        self.byte_pos = self.byte_pos.saturating_sub(rewind_bytes);
        self.bit_buf = 0;
        self.bit_count = 0;
        if len > self.data.len().saturating_sub(self.byte_pos) {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        let slice = &self.data[self.byte_pos..self.byte_pos + len];
        self.byte_pos += len;
        Ok(slice)
    }

    fn remaining_aligned_pos(&self) -> usize {
        let whole_bytes_buffered = usize::from(self.bit_count / 8);
        self.byte_pos.saturating_sub(whole_bytes_buffered)
    }
}

struct HuffmanTree {
    counts: [u16; 16],
    symbols: Vec<u16>,
    max_bits: u8,
    fast_lut: [u16; 512],
}

impl HuffmanTree {
    fn from_lengths(lengths: &[u8]) -> Result<Self, FetchError> {
        let mut counts = [0u16; 16];
        for &len in lengths {
            if len > 15 {
                return Err(FetchError::UnsupportedContentEncoding);
            }
            if len > 0 {
                counts[usize::from(len)] += 1;
            }
        }
        let mut left = 1i32;
        for &count in &counts[1..=15] {
            left <<= 1;
            left -= i32::from(count);
            if left < 0 {
                return Err(FetchError::UnsupportedContentEncoding);
            }
        }
        let mut offs = [0u16; 16];
        let mut next_code = [0u16; 16];
        let mut code = 0u16;
        let mut total = 0u16;
        let mut max_bits = 0u8;
        for len in 1..=15 {
            offs[len] = total;
            total += counts[len];
            code = (code + counts[len - 1]) << 1;
            next_code[len] = code;
            if counts[len] > 0 {
                max_bits = len as u8;
            }
        }
        let mut symbols = vec![0u16; usize::from(total)];
        let mut fast_lut = [0u16; 512];
        for (sym, &len) in lengths.iter().enumerate() {
            if len > 0 {
                let len_usize = usize::from(len);
                let idx = usize::from(offs[len_usize]);
                offs[len_usize] += 1;
                symbols[idx] = sym as u16;
                let c = next_code[len_usize];
                next_code[len_usize] = c.wrapping_add(1);
                if len <= 9 {
                    let rev = c.reverse_bits() >> (16 - len);
                    let entry = ((sym as u16) << 4) | u16::from(len);
                    let step = 1usize << len;
                    let mut slot = usize::from(rev);
                    while slot < 512 {
                        fast_lut[slot] = entry;
                        slot += step;
                    }
                }
            }
        }
        Ok(Self {
            counts,
            symbols,
            max_bits,
            fast_lut,
        })
    }

    #[inline(always)]
    fn decode(&self, reader: &mut BitReader<'_>) -> Result<u16, FetchError> {
        reader.fill();
        let entry = self.fast_lut[(reader.bit_buf & 0x1ff) as usize];
        let fast_len = (entry & 0x0f) as u8;
        if fast_len > 0 && fast_len <= reader.bit_count {
            reader.bit_buf >>= fast_len;
            reader.bit_count -= fast_len;
            return Ok(entry >> 4);
        }
        let mut code = 0i32;
        let mut first = 0i32;
        let mut index = 0i32;
        for len in 1..=usize::from(self.max_bits) {
            code |= reader.read_bits(1)? as i32;
            let count = i32::from(self.counts[len]);
            if code - count < first {
                return Ok(self.symbols[(index + (code - first)) as usize]);
            }
            index += count;
            first += count;
            first <<= 1;
            code <<= 1;
        }
        Err(FetchError::UnsupportedContentEncoding)
    }
}

fn build_fixed_trees() -> Result<(HuffmanTree, HuffmanTree), FetchError> {
    let mut lit_lengths = [0u8; 288];
    lit_lengths[..144].fill(8);
    lit_lengths[144..256].fill(9);
    lit_lengths[256..280].fill(7);
    lit_lengths[280..288].fill(8);
    let dist_lengths = [5u8; 32];
    Ok((
        HuffmanTree::from_lengths(&lit_lengths)?,
        HuffmanTree::from_lengths(&dist_lengths)?,
    ))
}

fn fixed_trees() -> Result<&'static (HuffmanTree, HuffmanTree), FetchError> {
    static FIXED_TREES: std::sync::OnceLock<Result<(HuffmanTree, HuffmanTree), FetchError>> =
        std::sync::OnceLock::new();
    match FIXED_TREES.get_or_init(build_fixed_trees) {
        Ok(trees) => Ok(trees),
        Err(_) => Err(FetchError::UnsupportedContentEncoding),
    }
}

fn build_dynamic_trees(
    reader: &mut BitReader<'_>,
) -> Result<(HuffmanTree, HuffmanTree), FetchError> {
    const CL_ORDER: [usize; 19] = [
        16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15,
    ];
    let hlit = (reader.read_bits(5)? as usize) + 257;
    let hdist = (reader.read_bits(5)? as usize) + 1;
    let hclen = (reader.read_bits(4)? as usize) + 4;
    if hlit > 286 || hdist > 30 {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    let mut cl_lengths = [0u8; 19];
    for &idx in &CL_ORDER[..hclen] {
        cl_lengths[idx] = reader.read_bits(3)? as u8;
    }
    let cl_tree = HuffmanTree::from_lengths(&cl_lengths)?;
    let total_codes = hlit + hdist;
    let mut lengths = vec![0u8; total_codes];
    let mut i = 0;
    while i < total_codes {
        let sym = cl_tree.decode(reader)?;
        match sym {
            0..=15 => {
                lengths[i] = sym as u8;
                i += 1;
            }
            16 => {
                if i == 0 {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                let prev = lengths[i - 1];
                let repeat = (reader.read_bits(2)? as usize) + 3;
                if i + repeat > total_codes {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                lengths[i..i + repeat].fill(prev);
                i += repeat;
            }
            17 => {
                let repeat = (reader.read_bits(3)? as usize) + 3;
                if i + repeat > total_codes {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                i += repeat;
            }
            18 => {
                let repeat = (reader.read_bits(7)? as usize) + 11;
                if i + repeat > total_codes {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                i += repeat;
            }
            _ => return Err(FetchError::UnsupportedContentEncoding),
        }
    }
    if lengths[256] == 0 {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    Ok((
        HuffmanTree::from_lengths(&lengths[..hlit])?,
        HuffmanTree::from_lengths(&lengths[hlit..])?,
    ))
}

fn inflate_raw(reader: &mut BitReader<'_>, out: &mut BoundedOutputBuf) -> Result<(), FetchError> {
    const LENGTH_BASE: [u16; 29] = [
        3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99, 115,
        131, 163, 195, 227, 258,
    ];
    const LENGTH_EXTRA: [u8; 29] = [
        0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
    ];
    const DIST_BASE: [u16; 30] = [
        1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025, 1537,
        2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
    ];
    const DIST_EXTRA: [u8; 30] = [
        0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12,
        13, 13,
    ];
    const MAX_DEFLATE_BLOCKS: usize = 65_536;
    const MAX_CONSECUTIVE_EMPTY_BLOCKS: usize = 64;
    let mut total_blocks = 0usize;
    let mut consecutive_empty_blocks = 0usize;
    loop {
        total_blocks += 1;
        if total_blocks > MAX_DEFLATE_BLOCKS {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        let before_len = out.len();
        let bfinal = reader.read_bits(1)?;
        let btype = reader.read_bits(2)?;
        match btype {
            0 => {
                let hdr = reader.read_aligned_bytes(4)?;
                let len = u16::from_le_bytes([hdr[0], hdr[1]]);
                let nlen = u16::from_le_bytes([hdr[2], hdr[3]]);
                if len != !nlen {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                let slice = reader.read_aligned_bytes(usize::from(len))?;
                out.push_slice(slice)?;
            }
            1 | 2 => {
                let dynamic_holder;
                let (lit_tree, dist_tree) = if btype == 1 {
                    let fixed = fixed_trees()?;
                    (&fixed.0, &fixed.1)
                } else {
                    dynamic_holder = build_dynamic_trees(reader)?;
                    (&dynamic_holder.0, &dynamic_holder.1)
                };
                loop {
                    let sym = lit_tree.decode(reader)?;
                    if sym < 256 {
                        out.push_byte(sym as u8)?;
                    } else if sym == 256 {
                        break;
                    } else {
                        let len_idx = usize::from(sym - 257);
                        if len_idx >= LENGTH_BASE.len() {
                            return Err(FetchError::UnsupportedContentEncoding);
                        }
                        let length = usize::from(LENGTH_BASE[len_idx])
                            + (reader.read_bits(LENGTH_EXTRA[len_idx])? as usize);
                        let dist_sym = usize::from(dist_tree.decode(reader)?);
                        if dist_sym >= DIST_BASE.len() {
                            return Err(FetchError::UnsupportedContentEncoding);
                        }
                        let distance = usize::from(DIST_BASE[dist_sym])
                            + (reader.read_bits(DIST_EXTRA[dist_sym])? as usize);
                        out.copy_backref(distance, length)?;
                    }
                }
            }
            _ => return Err(FetchError::UnsupportedContentEncoding),
        }
        if out.len() == before_len {
            consecutive_empty_blocks += 1;
            if consecutive_empty_blocks > MAX_CONSECUTIVE_EMPTY_BLOCKS {
                return Err(FetchError::UnsupportedContentEncoding);
            }
        } else {
            consecutive_empty_blocks = 0;
        }
        if bfinal != 0 {
            break;
        }
    }
    Ok(())
}

const CRC32_TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut i = 0usize;
    while i < 256 {
        let mut c = i as u32;
        let mut bit = 0;
        while bit < 8 {
            if (c & 1) != 0 {
                c = (c >> 1) ^ 0xedb8_8320;
            } else {
                c >>= 1;
            }
            bit += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
};

fn crc32_ieee(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &b in data {
        let idx = ((crc as u8) ^ b) as usize;
        crc = (crc >> 8) ^ CRC32_TABLE[idx];
    }
    !crc
}

fn adler32_rfc1950(data: &[u8]) -> u32 {
    const MOD_ADLER: u32 = 65_521;
    let mut a = 1u32;
    let mut b = 0u32;
    for chunk in data.chunks(5552) {
        for &byte in chunk {
            a += u32::from(byte);
            b += a;
        }
        a %= MOD_ADLER;
        b %= MOD_ADLER;
    }
    (b << 16) | a
}

fn parse_gzip_header_offset(data: &[u8]) -> Result<usize, FetchError> {
    if data.len() < 18 || data[0] != 0x1f || data[1] != 0x8b || data[2] != 8 {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    let flg = data[3];
    if (flg & 0xe0) != 0 {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    let mut pos = 10usize;
    if (flg & 0x04) != 0 {
        if pos + 2 > data.len() {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        let xlen = usize::from(u16::from_le_bytes([data[pos], data[pos + 1]]));
        pos = pos.saturating_add(2).saturating_add(xlen);
    }
    if (flg & 0x08) != 0 {
        while pos < data.len() && data[pos] != 0 {
            pos += 1;
        }
        if pos >= data.len() {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        pos += 1;
    }
    if (flg & 0x10) != 0 {
        while pos < data.len() && data[pos] != 0 {
            pos += 1;
        }
        if pos >= data.len() {
            return Err(FetchError::UnsupportedContentEncoding);
        }
        pos += 1;
    }
    if (flg & 0x02) != 0 {
        pos = pos.saturating_add(2);
    }
    if pos + 8 > data.len() {
        return Err(FetchError::UnsupportedContentEncoding);
    }
    Ok(pos)
}

pub fn decode_content_encoding(
    wire: &[u8],
    mode: ContentEncodingMode,
    max_body_bytes: usize,
) -> Result<Bytes, FetchError> {
    match mode {
        ContentEncodingMode::Identity => {
            let mut out = BoundedOutputBuf::new(max_body_bytes);
            out.push_slice(wire)?;
            Ok(out.freeze())
        }
        ContentEncodingMode::Gzip => {
            if wire.is_empty() {
                return Err(FetchError::UnsupportedContentEncoding);
            }
            let mut out = BoundedOutputBuf::new(max_body_bytes);
            let mut member_offset = 0usize;
            while member_offset < wire.len() {
                let member_out_start = out.len();
                let rel_start = parse_gzip_header_offset(&wire[member_offset..])?;
                let start = member_offset + rel_start;
                let mut reader = BitReader::new(&wire[start..]);
                inflate_raw(&mut reader, &mut out)?;
                let trailer_pos = start + reader.remaining_aligned_pos();
                if wire.len().saturating_sub(trailer_pos) < 8 {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                let expected_crc32 = u32::from_le_bytes([
                    wire[trailer_pos],
                    wire[trailer_pos + 1],
                    wire[trailer_pos + 2],
                    wire[trailer_pos + 3],
                ]);
                let expected_isize = u32::from_le_bytes([
                    wire[trailer_pos + 4],
                    wire[trailer_pos + 5],
                    wire[trailer_pos + 6],
                    wire[trailer_pos + 7],
                ]);
                let member_slice = &out.buf[member_out_start..];
                let member_out_len = member_slice.len() as u32;
                if member_out_len != expected_isize || crc32_ieee(member_slice) != expected_crc32 {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                member_offset = trailer_pos + 8;
            }
            Ok(out.freeze())
        }
        ContentEncodingMode::Deflate => {
            // Support both RFC 1950 zlib-wrapped deflate and raw RFC 1951 deflate:
            let is_zlib_header = wire.len() >= 2
                && (wire[0] & 0x0f) == 8
                && (wire[0] >> 4) <= 7
                && ((u16::from(wire[0]) << 8) | u16::from(wire[1])).is_multiple_of(31);
            if is_zlib_header {
                if (wire[1] & 0x20) != 0 || wire.len() < 6 {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                let start = 2usize;
                let mut reader = BitReader::new(&wire[start..]);
                let mut out = BoundedOutputBuf::new(max_body_bytes);
                inflate_raw(&mut reader, &mut out)?;
                let trailer_pos = start + reader.remaining_aligned_pos();
                if trailer_pos + 4 > wire.len() {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                let expected_adler32 = u32::from_be_bytes([
                    wire[trailer_pos],
                    wire[trailer_pos + 1],
                    wire[trailer_pos + 2],
                    wire[trailer_pos + 3],
                ]);
                if adler32_rfc1950(&out.buf) != expected_adler32 {
                    return Err(FetchError::UnsupportedContentEncoding);
                }
                return Ok(out.freeze());
            }
            let mut reader = BitReader::new(wire);
            let mut out = BoundedOutputBuf::new(max_body_bytes);
            inflate_raw(&mut reader, &mut out)?;
            Ok(out.freeze())
        }
    }
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
    initialize_tls_provider().map_err(|_| FetchError::TlsProviderInitialization)?;
    let client = pooled_pinned_client(policy, host, &addrs)?;
    let mut request = client
        .get(url.clone())
        .header(reqwest::header::ACCEPT_ENCODING, "gzip, deflate, identity")
        .header(
            reqwest::header::ACCEPT,
            "text/html,application/json;q=0.9,text/plain;q=0.5",
        );
    if let Some(value) = validator {
        request = request.header(IF_NONE_MATCH, value);
    }
    let response = request
        .send()
        .await
        .map_err(|error| FetchError::Network(error.without_url()))?;
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
    let encoding_mode = ContentEncodingMode::parse(
        response
            .headers()
            .get(CONTENT_ENCODING)
            .and_then(|v| v.to_str().ok()),
    )?;
    if let Some(len) = response.content_length()
        && len > policy.max_body_bytes as u64
    {
        return Err(FetchError::TooLarge);
    }
    // A non-2xx response is observable evidence (including 304 and 429), not
    // automatically a successful content fetch. Upper layer classifies it.
    let mut stream = response.bytes_stream();
    let mut wire = BoundedOutputBuf::new(policy.max_body_bytes);
    while let Some(next) = stream.next().await {
        let part = next.map_err(|error| FetchError::Network(error.without_url()))?;
        wire.push_slice(&part)?;
    }
    let payload = if encoding_mode == ContentEncodingMode::Identity || wire.is_empty() {
        wire.freeze()
    } else {
        decode_content_encoding(&wire.buf, encoding_mode, policy.max_body_bytes)?
    };
    Ok(Fetched {
        final_url: url.clone(),
        status,
        content_type,
        etag,
        payload,
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
    #[tokio::test]
    async fn network_error_formatting_never_exposes_query_credentials() {
        use std::time::Duration;

        initialize_tls_provider().unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            tokio::time::timeout(Duration::from_secs(5), listener.accept())
                .await
                .expect("the local client must connect within the test bound")
                .unwrap();
            // Dropping the accepted stream without an HTTP response produces a
            // deterministic request error while keeping the test loopback-only.
        });

        let secret = "athena-test-query-secret-sentinel";
        let url = Url::parse(&format!("http://{address}/?token={secret}")).unwrap();
        let client = reqwest::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap();
        let error = tokio::time::timeout(Duration::from_secs(5), client.get(url.clone()).send())
            .await
            .expect("the local request must finish within the test bound")
            .unwrap_err();
        server.await.unwrap();

        assert_eq!(
            error.url(),
            Some(&url),
            "fixture must produce a URL-bearing reqwest error"
        );
        let error = FetchError::Network(error);
        assert!(
            !format!("{error:?}").contains(secret),
            "Debug output must not expose URL query credentials"
        );
        assert!(
            !error.to_string().contains(secret),
            "Display output must not expose URL query credentials"
        );
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

    #[test]
    fn bounded_output_buf_and_gzip_deflate_decompression_abort_zip_bombs() {
        // RFC 1952 gzip of "<main><p>Hello Athena Gzip</p></main>" (37 bytes)
        let gzip_hello: &[u8] = &[
            31, 139, 8, 0, 0, 0, 0, 0, 2, 10, 179, 201, 77, 204, 204, 179, 179, 41, 176, 243, 72,
            205, 201, 201, 87, 112, 44, 201, 72, 205, 75, 84, 112, 175, 202, 44, 176, 209, 47, 176,
            179, 209, 7, 75, 3, 0, 8, 225, 221, 91, 37, 0, 0, 0,
        ];
        let decoded = decode_content_encoding(gzip_hello, ContentEncodingMode::Gzip, 1024).unwrap();
        assert_eq!(&decoded[..], b"<main><p>Hello Athena Gzip</p></main>");

        // When max_body_bytes is smaller than the decompressed output (e.g., 16 bytes < 37 bytes),
        // BoundedOutputBuf aborts immediately during inflation with FetchError::TooLarge:
        assert!(matches!(
            decode_content_encoding(gzip_hello, ContentEncodingMode::Gzip, 16),
            Err(FetchError::TooLarge)
        ));

        // Raw RFC 1951 deflate payload (slice of gzip_hello between 10-byte header and 8-byte trailer):
        let deflate_raw = &gzip_hello[10..gzip_hello.len() - 8];
        let decoded_deflate =
            decode_content_encoding(deflate_raw, ContentEncodingMode::Deflate, 1024).unwrap();
        assert_eq!(
            &decoded_deflate[..],
            b"<main><p>Hello Athena Gzip</p></main>"
        );
        assert!(matches!(
            decode_content_encoding(deflate_raw, ContentEncodingMode::Deflate, 20),
            Err(FetchError::TooLarge)
        ));

        // RFC 1950 zlib-wrapped deflate payload:
        let zlib_hello: &[u8] = &[
            120, 156, 179, 201, 77, 204, 204, 179, 179, 41, 176, 243, 72, 205, 201, 201, 87, 112,
            44, 201, 72, 205, 75, 84, 112, 175, 202, 44, 176, 209, 47, 176, 179, 209, 7, 75, 3, 0,
            241, 87, 12, 144,
        ];
        let decoded_zlib =
            decode_content_encoding(zlib_hello, ContentEncodingMode::Deflate, 1024).unwrap();
        assert_eq!(&decoded_zlib[..], b"<main><p>Hello Athena Gzip</p></main>");

        // Dynamic-Huffman compressed bomb (96 wire bytes -> 8,448 decompressed bytes):
        let gzip_bomb: &[u8] = &[
            31, 139, 8, 0, 0, 0, 0, 0, 2, 10, 237, 202, 221, 9, 128, 32, 24, 0, 192, 85, 220, 160,
            5, 68, 104, 20, 173, 15, 130, 126, 148, 112, 127, 90, 163, 135, 187, 231, 203, 163,
            172, 243, 136, 167, 166, 55, 70, 212, 25, 123, 106, 253, 110, 169, 93, 125, 59, 243,
            50, 74, 22, 4, 65, 16, 4, 65, 16, 4, 65, 16, 4, 65, 16, 4, 65, 16, 4, 65, 16, 4, 65,
            16, 4, 65, 248, 119, 248, 0, 46, 69, 153, 240, 0, 33, 0, 0,
        ];
        let decoded_bomb_ok =
            decode_content_encoding(gzip_bomb, ContentEncodingMode::Gzip, 16 * 1024).unwrap();
        assert_eq!(decoded_bomb_ok.len(), 33 * 256);
        assert!(matches!(
            decode_content_encoding(gzip_bomb, ContentEncodingMode::Gzip, 512),
            Err(FetchError::TooLarge)
        ));

        // Unknown encoding is rejected:
        assert!(matches!(
            ContentEncodingMode::parse(Some("br-custom-unknown")),
            Err(FetchError::UnsupportedContentEncoding)
        ));
    }

    #[test]
    fn net_02_03_04_empty_block_bomb_zlib_fdict_trailer_and_multi_member_gzip() {
        let gzip_hello: &[u8] = &[
            31, 139, 8, 0, 0, 0, 0, 0, 2, 10, 179, 201, 77, 204, 204, 179, 179, 41, 176, 243, 72,
            205, 201, 201, 87, 112, 44, 201, 72, 205, 75, 84, 112, 175, 202, 44, 176, 209, 47, 176,
            179, 209, 7, 75, 3, 0, 8, 225, 221, 91, 37, 0, 0, 0,
        ];
        // NET-04: Multi-member GZIP concatenating two valid GZIP members:
        let mut multi_gzip = Vec::with_capacity(gzip_hello.len() * 2);
        multi_gzip.extend_from_slice(gzip_hello);
        multi_gzip.extend_from_slice(gzip_hello);
        let decoded_multi =
            decode_content_encoding(&multi_gzip, ContentEncodingMode::Gzip, 1024).unwrap();
        assert_eq!(
            &decoded_multi[..],
            b"<main><p>Hello Athena Gzip</p></main><main><p>Hello Athena Gzip</p></main>"
        );

        // NET-04: Reject reserved GZIP header flags (flg & 0xe0 != 0):
        let mut bad_flg_gzip = gzip_hello.to_vec();
        bad_flg_gzip[3] = 0x20;
        assert!(matches!(
            decode_content_encoding(&bad_flg_gzip, ContentEncodingMode::Gzip, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));

        // NET-04: Reject corrupt GZIP CRC32 trailer even when ISIZE matches:
        let mut bad_crc_gzip = gzip_hello.to_vec();
        let crc_idx = bad_crc_gzip.len() - 8;
        bad_crc_gzip[crc_idx] ^= 0xff;
        assert!(matches!(
            decode_content_encoding(&bad_crc_gzip, ContentEncodingMode::Gzip, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));

        // NET-03: Reject Zlib FDICT (wire[1] & 0x20 != 0; 0x78 0xbb is divisible by 31 with FDICT=1):
        let zlib_fdict: &[u8] = &[0x78, 0xbb, 0, 0, 0, 0, 3, 0, 0, 0, 0, 1];
        assert!(matches!(
            decode_content_encoding(zlib_fdict, ContentEncodingMode::Deflate, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));

        // NET-03: Reject Zlib stream missing 4-byte Adler-32 trailer or with corrupt Adler-32 trailer:
        let zlib_no_trailer: &[u8] = &[
            120, 156, 179, 201, 77, 204, 204, 179, 179, 41, 176, 243, 72, 205, 201, 201, 87, 112,
            44, 201, 72, 205, 75, 84, 112, 175, 202, 44, 176, 209, 47, 176, 179, 209, 7, 75, 3, 0,
        ];
        assert!(matches!(
            decode_content_encoding(zlib_no_trailer, ContentEncodingMode::Deflate, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));
        let mut zlib_bad_adler = zlib_no_trailer.to_vec();
        zlib_bad_adler.extend_from_slice(&[0xde, 0xad, 0xbe, 0xef]);
        assert!(matches!(
            decode_content_encoding(&zlib_bad_adler, ContentEncodingMode::Deflate, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));

        // NET-02: Reject 0-byte empty deflate block bombs (> 64 consecutive 0-byte blocks):
        // Each stored empty non-final block is [0x00, 0x00, 0x00, 0xff, 0xff].
        let mut empty_block_bomb = Vec::new();
        for _ in 0..70 {
            empty_block_bomb.extend_from_slice(&[0x00, 0x00, 0x00, 0xff, 0xff]);
        }
        empty_block_bomb.extend_from_slice(&[0x01, 0x00, 0x00, 0xff, 0xff]);
        assert!(matches!(
            decode_content_encoding(&empty_block_bomb, ContentEncodingMode::Deflate, 1024),
            Err(FetchError::UnsupportedContentEncoding)
        ));
    }
}
