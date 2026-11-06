//! Discounted Beta Thompson sampler. It is a heuristic under nonstationarity,
//! not an exact Bayesian posterior. It cannot guarantee globally optimal costs.
use rand::Rng;
use rand_distr::{Beta, Distribution};
use std::time::{Duration, Instant};

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tier {
    Direct = 0,
    ReusedConnection = 1,
    ConditionalGet = 2,
    AuthorizedBrowser = 3,
    ContractedProxy = 4,
}
impl Tier {
    pub const ALL: [Tier; 5] = [
        Self::Direct,
        Self::ReusedConnection,
        Self::ConditionalGet,
        Self::AuthorizedBrowser,
        Self::ContractedProxy,
    ];
}

#[derive(Debug, Clone)]
pub struct TierStats {
    pub alpha: f64,
    pub beta: f64,
    pub ewma_latency_ms: f64,
    pub updated: Instant,
}
impl TierStats {
    pub fn new(now: Instant, prior_latency_ms: f64) -> Self {
        Self {
            alpha: 1.0,
            beta: 1.0,
            ewma_latency_ms: prior_latency_ms.max(0.0),
            updated: now,
        }
    }
    pub fn observe(&mut self, now: Instant, success: bool, latency_ms: f64, half_life: Duration) {
        let elapsed = now.saturating_duration_since(self.updated).as_secs_f64();
        let h = half_life.as_secs_f64().max(f64::MIN_POSITIVE);
        let decay = (-std::f64::consts::LN_2 * elapsed / h).exp();
        self.alpha = 1.0 + decay * (self.alpha - 1.0) + if success { 1.0 } else { 0.0 };
        self.beta = 1.0 + decay * (self.beta - 1.0) + if !success { 1.0 } else { 0.0 };
        if latency_ms.is_finite() && latency_ms >= 0.0 {
            self.ewma_latency_ms = 0.2 * latency_ms + 0.8 * self.ewma_latency_ms;
        }
        self.updated = now;
    }
}

pub struct Bandit {
    pub tiers: [TierStats; 5],
    pub half_life: Duration,
}

pub struct SelectionPolicy {
    /// Cost in USD PER ATTEMPT, not per 1K requests.
    pub cost_usd_per_attempt: [f64; 5],
    /// USD assigned to each millisecond of client-observed latency.
    pub latency_value_usd_per_ms: f64,
    /// Policy/availability gates must be checked before sampling.
    pub eligible: [bool; 5],
}

impl Bandit {
    pub fn new(now: Instant, prior_latency_ms: [f64; 5]) -> Self {
        Self {
            tiers: std::array::from_fn(|i| TierStats::new(now, prior_latency_ms[i])),
            half_life: Duration::from_secs(300),
        }
    }

    pub fn observe(&mut self, tier: Tier, success: bool, latency_ms: f64) {
        self.observe_at(Instant::now(), tier, success, latency_ms);
    }

    pub fn observe_at(&mut self, now: Instant, tier: Tier, success: bool, latency_ms: f64) {
        self.tiers[tier as usize].observe(now, success, latency_ms, self.half_life);
    }

    /// Discount old observations at decision time too, including inactive arms.
    /// Best candidate minimizes sampled cost-per-success proxy.
    pub fn choose<R: Rng + ?Sized>(
        &self,
        rng: &mut R,
        now: Instant,
        policy: &SelectionPolicy,
    ) -> Option<Tier> {
        let mut best: Option<(Tier, f64)> = None;
        for tier in Tier::ALL {
            let i = tier as usize;
            if !policy.eligible[i] {
                continue;
            }
            let c = policy.cost_usd_per_attempt[i];
            let l = policy.latency_value_usd_per_ms;
            if !c.is_finite() || c < 0.0 || !l.is_finite() || l < 0.0 {
                continue;
            }
            let s = &self.tiers[i];
            let elapsed = now.saturating_duration_since(s.updated).as_secs_f64();
            let decay = (-std::f64::consts::LN_2 * elapsed
                / self.half_life.as_secs_f64().max(f64::MIN_POSITIVE))
            .exp();
            let a = 1.0 + decay * (s.alpha - 1.0);
            let b = 1.0 + decay * (s.beta - 1.0);
            if !(a.is_finite() && b.is_finite() && a > 0.0 && b > 0.0) {
                continue;
            }
            let sampled_p: f64 = Beta::new(a, b).ok()?.sample(rng);
            let latency = s.ewma_latency_ms;
            if !latency.is_finite() || latency < 0.0 {
                continue;
            }
            let numerator = c + l * latency;
            if !numerator.is_finite() {
                continue;
            }
            // Approximation assumes identical independent retries and no switching;
            // production should estimate fallback transition-specific costs.
            let loss = numerator / sampled_p.max(0.001);
            if best.is_none_or(|(_, best_loss)| loss < best_loss) {
                best = Some((tier, loss));
            }
        }
        best.map(|(tier, _)| tier)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rand::SeedableRng;
    #[test]
    fn half_life_reduces_excess_evidence_by_half() {
        let now = Instant::now();
        let mut s = TierStats::new(now, 100.0);
        s.alpha = 101.0;
        s.observe(
            now + Duration::from_secs(300),
            false,
            100.0,
            Duration::from_secs(300),
        );
        assert!((s.alpha - 51.0).abs() < 1e-10);
        assert!((s.beta - 2.0).abs() < 1e-10);
    }
    #[test]
    fn unavailable_tier_cannot_win() {
        let now = Instant::now();
        let b = Bandit::new(now, [20.0; 5]);
        let policy = SelectionPolicy {
            cost_usd_per_attempt: [1.0; 5],
            latency_value_usd_per_ms: 0.0,
            eligible: [false, false, true, false, false],
        };
        let mut rng = rand::rngs::StdRng::seed_from_u64(42);
        assert_eq!(b.choose(&mut rng, now, &policy), Some(Tier::ConditionalGet));
    }

    #[test]
    fn soft_block_detector_identifies_captcha_and_empty_traps_without_false_positives() {
        assert_eq!(
            SoftBlockDetector::classify_response(
                200,
                b"<html><title>Just a moment...</title><div class='cf-chl-bypass'>Verify you are human</div></html>",
                0,
                None,
            ),
            FetchOutcomeKind::SoftBlockChallenge
        );
        assert_eq!(
            SoftBlockDetector::classify_response(429, b"rate limit exceeded", 0, Some(15)),
            FetchOutcomeKind::RateLimited429 {
                retry_after_secs: Some(15)
            }
        );
        assert_eq!(
            SoftBlockDetector::classify_response(403, b"Forbidden by WAF", 0, None),
            FetchOutcomeKind::HardBlockWaf
        );
        assert_eq!(
            SoftBlockDetector::classify_response(304, b"", 0, None),
            FetchOutcomeKind::NotModified304
        );
        assert_eq!(
            SoftBlockDetector::classify_response(
                200,
                b"<main><h1>Byzantine Consensus</h1><p>Quorum intersection guarantees safety across asynchronous rounds.</p></main>",
                2,
                None,
            ),
            FetchOutcomeKind::Success
        );
    }

    #[test]
    fn athena_contextual_router_quarantines_soft_blocked_and_rate_limited_proxies_and_minimizes_bandwidth_cost()
     {
        let now = Instant::now();
        let configs = vec![
            ProxyEndpointConfig {
                id: 0,
                tier: Tier::Direct,
                cost_usd_per_attempt: 0.0001,
                egress_usd_per_mib: 0.01,
                prior_latency_ms: 35.0,
                eligible: true,
            },
            ProxyEndpointConfig {
                id: 1,
                tier: Tier::ContractedProxy,
                cost_usd_per_attempt: 0.0005,
                egress_usd_per_mib: 0.08,
                prior_latency_ms: 85.0,
                eligible: true,
            },
            ProxyEndpointConfig {
                id: 2,
                tier: Tier::ContractedProxy,
                cost_usd_per_attempt: 0.0020,
                egress_usd_per_mib: 0.45,
                prior_latency_ms: 140.0,
                eligible: true,
            },
        ];
        let mut router = ProxyPoolRouter::new(
            now,
            configs,
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            0.00001,
        );
        let mut rng = rand::rngs::StdRng::seed_from_u64(777);

        // Route 0 gets hit by a stealth HTTP 200 Cloudflare challenge trap and a 429 rate limit
        router.observe(
            0,
            now + Duration::from_secs(1),
            FetchOutcomeKind::SoftBlockChallenge,
            25.0,
            1024,
        );
        router.observe(
            0,
            now + Duration::from_secs(2),
            FetchOutcomeKind::RateLimited429 {
                retry_after_secs: Some(30),
            },
            20.0,
            512,
        );
        // Route 1 succeeds cleanly with compressed payload
        for step in 3..15 {
            router.observe(
                1,
                now + Duration::from_secs(step),
                FetchOutcomeKind::Success,
                80.0,
                4096,
            );
        }

        // During the 30s quarantine window, route 0 must not be selected
        let decision = router
            .select_route(&mut rng, now + Duration::from_secs(16))
            .unwrap();
        assert_eq!(decision.primary_id, 1);
        assert!(router.nodes()[0].is_quarantined(now + Duration::from_secs(16)));
        assert!(!router.nodes()[0].is_quarantined(now + Duration::from_secs(60)));
    }
}

/// Fine-grained outcome classification for web/API retrieval across volatile proxies and WAFs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FetchOutcomeKind {
    /// Clean HTTP 200 with verified non-challenge content.
    Success,
    /// Conditional GET `304 Not Modified` (zero body egress transfer).
    NotModified304,
    /// HTTP 200 soft-block trap (CAPTCHA, Cloudflare challenge, login wall, or empty shell).
    SoftBlockChallenge,
    /// Explicit HTTP 403/503 WAF or bot-crawl block.
    HardBlockWaf,
    /// Explicit HTTP 429 rate-limit response with optional `Retry-After` seconds.
    RateLimited429 { retry_after_secs: Option<u64> },
    /// Upstream stall, TCP reset, or deadline exceeded.
    TimeoutOrReset,
    /// Proxy tunnel dead / connection refused / TLS handshake failure.
    ProxyDead,
}

impl FetchOutcomeKind {
    #[inline]
    pub fn is_usable(self) -> bool {
        matches!(self, Self::Success | Self::NotModified304)
    }

    #[inline]
    pub fn is_block_or_ratelimit(self) -> bool {
        matches!(
            self,
            Self::SoftBlockChallenge | Self::HardBlockWaf | Self::RateLimited429 { .. }
        )
    }
}

/// Fast deterministic detector for HTTP 200 bot-crawl soft blocks (CAPTCHA challenges,
/// JS interstitials, login traps, or empty DOM shells).
pub struct SoftBlockDetector;

impl SoftBlockDetector {
    const CHALLENGE_SIGNATURES: [&'static [u8]; 14] = [
        b"cf-chl-",
        b"cf-turnstile",
        b"just a moment...",
        b"attention required! | cloudflare",
        b"verify you are human",
        b"enable javascript and cookies to continue",
        b"g-recaptcha",
        b"h-captcha",
        b"px-captcha",
        b"ddos-guard",
        b"bot verification",
        b"access to this page has been denied",
        b"pardon our interruption",
        b"automated access to this data is blocked",
    ];

    pub fn classify_response(
        status_code: u16,
        body: &[u8],
        projected_chunks: usize,
        retry_after_secs: Option<u64>,
    ) -> FetchOutcomeKind {
        match status_code {
            304 => FetchOutcomeKind::NotModified304,
            429 => FetchOutcomeKind::RateLimited429 { retry_after_secs },
            403 | 407 | 451 | 503 => FetchOutcomeKind::HardBlockWaf,
            502 | 504 => FetchOutcomeKind::TimeoutOrReset,
            200 | 203 | 206 => {
                if body.is_empty() {
                    return FetchOutcomeKind::SoftBlockChallenge;
                }
                let scan_len = body.len().min(8192);
                let prefix = &body[..scan_len];
                if Self::contains_challenge_signature(prefix) {
                    return FetchOutcomeKind::SoftBlockChallenge;
                }
                if projected_chunks == 0 && body.len() < 2048 {
                    return FetchOutcomeKind::SoftBlockChallenge;
                }
                FetchOutcomeKind::Success
            }
            _ if status_code >= 500 => FetchOutcomeKind::ProxyDead,
            _ => FetchOutcomeKind::HardBlockWaf,
        }
    }

    fn contains_challenge_signature(haystack: &[u8]) -> bool {
        let mut lower_buf = [0u8; 8192];
        let n = haystack.len().min(lower_buf.len());
        for (dst, &src) in lower_buf[..n].iter_mut().zip(&haystack[..n]) {
            *dst = src.to_ascii_lowercase();
        }
        let slice = &lower_buf[..n];
        for sig in Self::CHALLENGE_SIGNATURES {
            if slice.windows(sig.len()).any(|w| w == sig) {
                return true;
            }
        }
        false
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoutingAlgorithm {
    /// Naive uniform round-robin across eligible routes.
    StaticRoundRobin,
    /// Baseline Discounted Beta Thompson Sampler (ignores soft-block quarantine & jitter).
    DiscountedThompson,
    /// Variance-aware Sliding-Window / Exponentially-Decayed UCB1-Tuned.
    SlidingWindowUcb1Tuned,
    /// Adversarial EXP3 bandit resilient to non-stationary regime switches.
    Exp3Adversarial,
    /// Athena's zero-tradeoff contextual router: soft-block anomaly detection, exponential
    /// quarantine circuit breaker, egress bandwidth cost budgeting, jitter penalty, and
    /// adaptive hedged failover.
    AthenaAdaptiveContextualV1,
}

impl RoutingAlgorithm {
    pub const ALL: [RoutingAlgorithm; 5] = [
        Self::StaticRoundRobin,
        Self::DiscountedThompson,
        Self::SlidingWindowUcb1Tuned,
        Self::Exp3Adversarial,
        Self::AthenaAdaptiveContextualV1,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Self::StaticRoundRobin => "static_round_robin",
            Self::DiscountedThompson => "discounted_thompson",
            Self::SlidingWindowUcb1Tuned => "sliding_window_ucb1_tuned",
            Self::Exp3Adversarial => "exp3_adversarial",
            Self::AthenaAdaptiveContextualV1 => "athena_adaptive_contextual_v1",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ProxyEndpointConfig {
    pub id: usize,
    pub tier: Tier,
    pub cost_usd_per_attempt: f64,
    pub egress_usd_per_mib: f64,
    pub prior_latency_ms: f64,
    pub eligible: bool,
}

#[derive(Debug, Clone)]
pub struct ProxyNodeStats {
    pub config: ProxyEndpointConfig,
    pub alpha: f64,
    pub beta: f64,
    pub alpha_net: f64,
    pub beta_net: f64,
    pub alpha_waf: f64,
    pub beta_waf: f64,
    pub min_interval_ms: f64,
    pub last_dispatch: Option<Instant>,
    pub decayed_pulls: f64,
    pub decayed_reward_sum: f64,
    pub decayed_reward_sq_sum: f64,
    pub exp3_weight: f64,
    pub ewma_latency_ms: f64,
    pub ewma_jitter_ms: f64,
    pub ewma_bytes: f64,
    pub ewma_304_rate: f64,
    pub ewma_block_rate: f64,
    pub consecutive_failures: u32,
    pub quarantined_until: Option<Instant>,
    pub updated: Instant,
    pub total_attempts: u64,
    pub usable_successes: u64,
    pub soft_blocks: u64,
    pub hard_blocks: u64,
    pub rate_limits: u64,
    pub timeouts_or_dead: u64,
    pub total_bytes: u64,
}

impl ProxyNodeStats {
    pub fn new(now: Instant, config: ProxyEndpointConfig) -> Self {
        let lat = config.prior_latency_ms.max(1.0);
        Self {
            config,
            alpha: 1.0,
            beta: 1.0,
            alpha_net: 1.0,
            beta_net: 1.0,
            alpha_waf: 1.0,
            beta_waf: 1.0,
            min_interval_ms: 0.0,
            last_dispatch: None,
            decayed_pulls: 1.0,
            decayed_reward_sum: 0.8,
            decayed_reward_sq_sum: 0.64,
            exp3_weight: 1.0,
            ewma_latency_ms: lat,
            ewma_jitter_ms: lat * 0.15,
            ewma_bytes: 12_288.0,
            ewma_304_rate: 0.0,
            ewma_block_rate: 0.0,
            consecutive_failures: 0,
            quarantined_until: None,
            updated: now,
            total_attempts: 0,
            usable_successes: 0,
            soft_blocks: 0,
            hard_blocks: 0,
            rate_limits: 0,
            timeouts_or_dead: 0,
            total_bytes: 0,
        }
    }

    #[inline]
    pub fn is_quarantined(&self, now: Instant) -> bool {
        self.quarantined_until.is_some_and(|until| now < until)
    }

    #[inline]
    pub fn pacing_delay_ms(&self, now: Instant) -> f64 {
        if self.min_interval_ms <= 0.0 {
            return 0.0;
        }
        match self.last_dispatch {
            Some(last) => {
                let elapsed_ms = now.saturating_duration_since(last).as_secs_f64() * 1000.0;
                (self.min_interval_ms - elapsed_ms).max(0.0)
            }
            None => 0.0,
        }
    }

    pub fn observe(
        &mut self,
        now: Instant,
        outcome: FetchOutcomeKind,
        latency_ms: f64,
        wire_bytes: usize,
        half_life: Duration,
        quarantine_enabled: bool,
    ) {
        let elapsed = now.saturating_duration_since(self.updated).as_secs_f64();
        let h = half_life.as_secs_f64().max(f64::MIN_POSITIVE);
        let decay = (-std::f64::consts::LN_2 * elapsed / h).exp();

        let usable = outcome.is_usable();
        let blocked = outcome.is_block_or_ratelimit();
        let is_304 = outcome == FetchOutcomeKind::NotModified304;
        let is_transport_fail = matches!(
            outcome,
            FetchOutcomeKind::TimeoutOrReset | FetchOutcomeKind::ProxyDead
        );

        self.total_attempts = self.total_attempts.saturating_add(1);
        self.total_bytes = self.total_bytes.saturating_add(wire_bytes as u64);
        match outcome {
            FetchOutcomeKind::Success | FetchOutcomeKind::NotModified304 => {
                self.usable_successes = self.usable_successes.saturating_add(1);
                self.consecutive_failures = 0;
                self.quarantined_until = None;
                self.min_interval_ms = (self.min_interval_ms - 5.0).max(0.0);
            }
            FetchOutcomeKind::SoftBlockChallenge => {
                self.soft_blocks = self.soft_blocks.saturating_add(1);
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                self.min_interval_ms = (self.min_interval_ms * 1.8 + 40.0).min(2_500.0);
            }
            FetchOutcomeKind::HardBlockWaf => {
                self.hard_blocks = self.hard_blocks.saturating_add(1);
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                self.min_interval_ms = (self.min_interval_ms * 1.8 + 50.0).min(2_500.0);
            }
            FetchOutcomeKind::RateLimited429 { .. } => {
                self.rate_limits = self.rate_limits.saturating_add(1);
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                self.min_interval_ms = (self.min_interval_ms * 2.0 + 75.0).min(3_000.0);
            }
            FetchOutcomeKind::TimeoutOrReset | FetchOutcomeKind::ProxyDead => {
                self.timeouts_or_dead = self.timeouts_or_dead.saturating_add(1);
                self.consecutive_failures = self.consecutive_failures.saturating_add(1);
            }
        }

        // Factored transport x WAF posteriors
        self.alpha_net =
            1.0 + decay * (self.alpha_net - 1.0) + if !is_transport_fail { 1.0 } else { 0.0 };
        self.beta_net =
            1.0 + decay * (self.beta_net - 1.0) + if is_transport_fail { 1.5 } else { 0.0 };
        if !is_transport_fail {
            let waf_decay = (-std::f64::consts::LN_2 * elapsed / (h * 0.25).max(1.0)).exp();
            self.alpha_waf =
                1.0 + waf_decay * (self.alpha_waf - 1.0) + if usable { 1.0 } else { 0.0 };
            self.beta_waf =
                1.0 + waf_decay * (self.beta_waf - 1.0) + if blocked { 2.2 } else { 0.0 };
        }

        // Strongly penalize beta when a block or dead tunnel occurs under quarantine_enabled
        let beta_increment = if usable {
            0.0
        } else if quarantine_enabled && blocked {
            2.2
        } else {
            1.0
        };
        self.alpha = 1.0 + decay * (self.alpha - 1.0) + if usable { 1.0 } else { 0.0 };
        self.beta = 1.0 + decay * (self.beta - 1.0) + beta_increment;

        if latency_ms.is_finite() && latency_ms >= 0.0 {
            let abs_err = (latency_ms - self.ewma_latency_ms).abs();
            self.ewma_jitter_ms = 0.25 * abs_err + 0.75 * self.ewma_jitter_ms;
            self.ewma_latency_ms = 0.20 * latency_ms + 0.80 * self.ewma_latency_ms;
        }
        self.ewma_bytes = 0.20 * (wire_bytes as f64) + 0.80 * self.ewma_bytes;
        self.ewma_304_rate = 0.15 * (if is_304 { 1.0 } else { 0.0 }) + 0.85 * self.ewma_304_rate;
        self.ewma_block_rate =
            0.25 * (if blocked { 1.0 } else { 0.0 }) + 0.75 * self.ewma_block_rate;

        // Sliding-window / decayed UCB1-Tuned statistics (normalized utility in [0, 1])
        let norm_lat_penalty = (latency_ms.max(0.0) / 1500.0).min(0.6);
        let reward = if usable {
            (1.0 - norm_lat_penalty).clamp(0.05, 1.0)
        } else {
            0.0
        };
        self.decayed_pulls = 0.96 * self.decayed_pulls + 1.0;
        self.decayed_reward_sum = 0.96 * self.decayed_reward_sum + reward;
        self.decayed_reward_sq_sum = 0.96 * self.decayed_reward_sq_sum + reward * reward;

        if quarantine_enabled && !usable {
            let cooldown_ms = match outcome {
                FetchOutcomeKind::RateLimited429 {
                    retry_after_secs: Some(secs),
                } => secs.saturating_mul(1000).clamp(1_000, 120_000),
                FetchOutcomeKind::RateLimited429 {
                    retry_after_secs: None,
                } => {
                    let shift = self.consecutive_failures.saturating_sub(1).min(6);
                    (2_500u64 << shift).min(90_000)
                }
                FetchOutcomeKind::SoftBlockChallenge | FetchOutcomeKind::HardBlockWaf => {
                    let shift = self.consecutive_failures.saturating_sub(1).min(6);
                    (3_000u64 << shift).min(120_000)
                }
                FetchOutcomeKind::ProxyDead => {
                    let shift = self.consecutive_failures.saturating_sub(1).min(6);
                    (4_000u64 << shift).min(180_000)
                }
                FetchOutcomeKind::TimeoutOrReset if self.consecutive_failures >= 2 => {
                    let shift = self.consecutive_failures.saturating_sub(2).min(5);
                    (1_500u64 << shift).min(45_000)
                }
                _ => 0,
            };
            if cooldown_ms > 0 {
                self.quarantined_until = Some(now + Duration::from_millis(cooldown_ms));
            }
        }
        self.updated = now;
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RouteDecision {
    pub primary_id: usize,
    pub hedge_backup_id: Option<usize>,
    pub hedge_delay_ms: Option<f64>,
    pub pacing_delay_ms: f64,
    pub expected_loss_usd: f64,
}

pub struct ProxyPoolRouter {
    nodes: Vec<ProxyNodeStats>,
    algorithm: RoutingAlgorithm,
    half_life: Duration,
    latency_value_usd_per_ms: f64,
    rr_cursor: usize,
    total_decisions: u64,
}

impl ProxyPoolRouter {
    pub fn new(
        now: Instant,
        configs: Vec<ProxyEndpointConfig>,
        algorithm: RoutingAlgorithm,
        latency_value_usd_per_ms: f64,
    ) -> Self {
        let nodes = configs
            .into_iter()
            .map(|c| ProxyNodeStats::new(now, c))
            .collect();
        Self {
            nodes,
            algorithm,
            half_life: Duration::from_secs(180),
            latency_value_usd_per_ms: latency_value_usd_per_ms.max(0.0),
            rr_cursor: 0,
            total_decisions: 0,
        }
    }

    pub fn nodes(&self) -> &[ProxyNodeStats] {
        &self.nodes
    }

    pub fn algorithm(&self) -> RoutingAlgorithm {
        self.algorithm
    }

    pub fn observe(
        &mut self,
        node_id: usize,
        now: Instant,
        outcome: FetchOutcomeKind,
        latency_ms: f64,
        wire_bytes: usize,
    ) {
        let k = self.nodes.len().max(1) as f64;
        let quarantine_enabled = self.algorithm == RoutingAlgorithm::AthenaAdaptiveContextualV1;
        let weight_sum: f64 = self
            .nodes
            .iter()
            .filter(|n| n.config.eligible)
            .map(|n| n.exp3_weight)
            .sum::<f64>()
            .max(f64::MIN_POSITIVE);
        if let Some(node) = self.nodes.get_mut(node_id) {
            node.observe(
                now,
                outcome,
                latency_ms,
                wire_bytes,
                self.half_life,
                quarantine_enabled,
            );
            // Update EXP3 adversarial weight
            let usable = outcome.is_usable();
            let cost_norm = ((node.config.cost_usd_per_attempt
                + (wire_bytes as f64 / (1024.0 * 1024.0)) * node.config.egress_usd_per_mib
                + self.latency_value_usd_per_ms * latency_ms.max(0.0))
                / 0.01)
                .clamp(0.0, 1.0);
            let reward = if usable { 1.0 - 0.5 * cost_norm } else { 0.0 };
            let gamma = 0.07f64;
            let p_arm = ((1.0 - gamma) * (node.exp3_weight / weight_sum) + gamma / k).max(0.01);
            let est_reward = reward / p_arm;
            node.exp3_weight =
                (node.exp3_weight * 0.985 * ((gamma * est_reward / k).exp())).clamp(1e-4, 1e6);
        }
    }

    pub fn select_route<R: Rng + ?Sized>(
        &mut self,
        rng: &mut R,
        now: Instant,
    ) -> Option<RouteDecision> {
        let eligible_count = self.nodes.iter().filter(|n| n.config.eligible).count();
        if eligible_count == 0 {
            return None;
        }
        self.total_decisions = self.total_decisions.saturating_add(1);

        let decision = match self.algorithm {
            RoutingAlgorithm::StaticRoundRobin => {
                let mut found = None;
                for offset in 0..self.nodes.len() {
                    let idx = (self.rr_cursor + offset) % self.nodes.len();
                    if self.nodes[idx].config.eligible {
                        self.rr_cursor = (idx + 1) % self.nodes.len();
                        let s = &self.nodes[idx];
                        let loss = s.config.cost_usd_per_attempt
                            + self.latency_value_usd_per_ms * s.ewma_latency_ms;
                        found = Some(RouteDecision {
                            primary_id: idx,
                            hedge_backup_id: None,
                            hedge_delay_ms: None,
                            pacing_delay_ms: 0.0,
                            expected_loss_usd: loss,
                        });
                        break;
                    }
                }
                found
            }
            RoutingAlgorithm::DiscountedThompson => {
                let mut best: Option<(usize, f64)> = None;
                for (idx, s) in self.nodes.iter().enumerate() {
                    if !s.config.eligible {
                        continue;
                    }
                    let elapsed = now.saturating_duration_since(s.updated).as_secs_f64();
                    let decay = (-std::f64::consts::LN_2 * elapsed
                        / self.half_life.as_secs_f64().max(f64::MIN_POSITIVE))
                    .exp();
                    let a = (1.0 + decay * (s.alpha - 1.0)).max(1e-3);
                    let b = (1.0 + decay * (s.beta - 1.0)).max(1e-3);
                    let sampled_p = Beta::new(a, b).ok()?.sample(rng);
                    let num = s.config.cost_usd_per_attempt
                        + self.latency_value_usd_per_ms * s.ewma_latency_ms;
                    let loss = num / sampled_p.max(0.001);
                    if best.is_none_or(|(_, b_loss)| loss < b_loss) {
                        best = Some((idx, loss));
                    }
                }
                best.map(|(primary_id, expected_loss_usd)| RouteDecision {
                    primary_id,
                    hedge_backup_id: None,
                    hedge_delay_ms: None,
                    pacing_delay_ms: 0.0,
                    expected_loss_usd,
                })
            }
            RoutingAlgorithm::SlidingWindowUcb1Tuned => {
                let total_pulls: f64 = self
                    .nodes
                    .iter()
                    .filter(|n| n.config.eligible)
                    .map(|n| n.decayed_pulls)
                    .sum::<f64>()
                    .max(2.0);
                let ln_n = total_pulls.ln().max(0.1);
                let mut best: Option<(usize, f64, f64)> = None;
                for (idx, s) in self.nodes.iter().enumerate() {
                    if !s.config.eligible {
                        continue;
                    }
                    let n_i = s.decayed_pulls.max(1.0);
                    let mean_r = (s.decayed_reward_sum / n_i).clamp(0.0, 1.0);
                    let var_r = ((s.decayed_reward_sq_sum / n_i) - mean_r * mean_r).max(0.0);
                    let v_i = (var_r + (2.0 * ln_n / n_i).sqrt()).min(0.25);
                    let bonus = ((ln_n / n_i) * v_i).sqrt();
                    let cost_penalty = (s.config.cost_usd_per_attempt
                        + (s.ewma_bytes / (1024.0 * 1024.0)) * s.config.egress_usd_per_mib)
                        * 120.0;
                    let ucb_score = mean_r + bonus - cost_penalty;
                    let est_loss = (s.config.cost_usd_per_attempt
                        + self.latency_value_usd_per_ms * s.ewma_latency_ms)
                        / mean_r.max(0.01);
                    if best.is_none_or(|(_, b_score, _)| ucb_score > b_score) {
                        best = Some((idx, ucb_score, est_loss));
                    }
                }
                best.map(|(primary_id, _, expected_loss_usd)| RouteDecision {
                    primary_id,
                    hedge_backup_id: None,
                    hedge_delay_ms: None,
                    pacing_delay_ms: 0.0,
                    expected_loss_usd,
                })
            }
            RoutingAlgorithm::Exp3Adversarial => {
                let gamma = 0.07f64;
                let k = eligible_count as f64;
                let weight_sum: f64 = self
                    .nodes
                    .iter()
                    .filter(|n| n.config.eligible)
                    .map(|n| n.exp3_weight)
                    .sum::<f64>()
                    .max(f64::MIN_POSITIVE);
                let u: f64 = rng.random_range(0.0..1.0);
                let mut cumulative = 0.0;
                let mut chosen = None;
                for (idx, s) in self.nodes.iter().enumerate() {
                    if !s.config.eligible {
                        continue;
                    }
                    let p = (1.0 - gamma) * (s.exp3_weight / weight_sum) + gamma / k;
                    cumulative += p;
                    if u <= cumulative || chosen.is_none() {
                        chosen = Some(idx);
                        if u <= cumulative {
                            break;
                        }
                    }
                }
                let primary_id = chosen?;
                let s = &self.nodes[primary_id];
                let loss = s.config.cost_usd_per_attempt
                    + self.latency_value_usd_per_ms * s.ewma_latency_ms;
                Some(RouteDecision {
                    primary_id,
                    hedge_backup_id: None,
                    hedge_delay_ms: None,
                    pacing_delay_ms: 0.0,
                    expected_loss_usd: loss,
                })
            }
            RoutingAlgorithm::AthenaAdaptiveContextualV1 => {
                let any_unquarantined = self
                    .nodes
                    .iter()
                    .any(|n| n.config.eligible && !n.is_quarantined(now));
                let mut candidates: Vec<(usize, f64)> = Vec::with_capacity(self.nodes.len());
                for (idx, s) in self.nodes.iter().enumerate() {
                    if !s.config.eligible {
                        continue;
                    }
                    if any_unquarantined && s.is_quarantined(now) {
                        continue;
                    }
                    let elapsed = now.saturating_duration_since(s.updated).as_secs_f64();
                    let h = self.half_life.as_secs_f64().max(f64::MIN_POSITIVE);
                    let decay = (-std::f64::consts::LN_2 * elapsed / h).exp();
                    let waf_decay = (-std::f64::consts::LN_2 * elapsed / (h * 0.25).max(1.0)).exp();
                    let a = (1.0 + decay * (s.alpha - 1.0)).max(1e-3);
                    let b = (1.0 + decay * (s.beta - 1.0)).max(1e-3);
                    let a_net = (1.0 + decay * (s.alpha_net - 1.0)).max(1e-3);
                    let b_net = (1.0 + decay * (s.beta_net - 1.0)).max(1e-3);
                    let a_waf = (1.0 + waf_decay * (s.alpha_waf - 1.0)).max(1e-3);
                    let b_waf = (1.0 + waf_decay * (s.beta_waf - 1.0)).max(1e-3);

                    let posterior_mean = 0.5 * (a / (a + b))
                        + 0.5 * ((a_net / (a_net + b_net)) * (a_waf / (a_waf + b_waf)));
                    let sampled_joint = Beta::new(a, b).ok()?.sample(rng);
                    let sampled_factored = Beta::new(a_net, b_net).ok()?.sample(rng)
                        * Beta::new(a_waf, b_waf).ok()?.sample(rng);
                    let sampled_p = 0.5 * sampled_joint + 0.5 * sampled_factored;
                    // Variance-damped Thompson blending: as sample confidence (a+b) grows,
                    // blend toward the posterior mean to avoid gratuitous regret spikes.
                    let confidence = ((a + b - 2.0) / 24.0).clamp(0.0, 0.75);
                    let effective_p =
                        (confidence * posterior_mean + (1.0 - confidence) * sampled_p).max(0.005);

                    let egress_mib =
                        (s.ewma_bytes * (1.0 - s.ewma_304_rate * 0.92)) / (1024.0 * 1024.0);
                    let bandwidth_cost_usd = egress_mib * s.config.egress_usd_per_mib;
                    let pacing_ms = s.pacing_delay_ms(now);
                    let effective_latency_ms =
                        s.ewma_latency_ms + 1.35 * s.ewma_jitter_ms + pacing_ms;
                    let latency_cost_usd = self.latency_value_usd_per_ms * effective_latency_ms;
                    let block_risk_usd =
                        0.0025 * s.ewma_block_rate + 0.0010 * (s.consecutive_failures as f64);

                    let total_expected_loss = (s.config.cost_usd_per_attempt
                        + bandwidth_cost_usd
                        + latency_cost_usd
                        + block_risk_usd)
                        / effective_p;
                    candidates.push((idx, total_expected_loss));
                }
                candidates.sort_by(|a, b| a.1.total_cmp(&b.1).then_with(|| a.0.cmp(&b.0)));
                let &(primary_id, expected_loss_usd) = candidates.first()?;
                let primary = &self.nodes[primary_id];
                let pacing_delay_ms = primary.pacing_delay_ms(now);

                // Trigger adaptive hedged backup when primary proxy exhibits high tail jitter
                // or elevated block/failure risk.
                let (hedge_backup_id, hedge_delay_ms) = if candidates.len() >= 2
                    && (primary.ewma_jitter_ms > 0.35 * primary.ewma_latency_ms
                        || primary.ewma_block_rate > 0.12
                        || primary.consecutive_failures > 0)
                {
                    let backup_id = candidates[1].0;
                    let delay = (primary.ewma_latency_ms + 1.25 * primary.ewma_jitter_ms)
                        .clamp(15.0, 350.0);
                    (Some(backup_id), Some(delay))
                } else {
                    (None, None)
                };

                Some(RouteDecision {
                    primary_id,
                    hedge_backup_id,
                    hedge_delay_ms,
                    pacing_delay_ms,
                    expected_loss_usd,
                })
            }
        };
        if let Some(d) = decision
            && let Some(node) = self.nodes.get_mut(d.primary_id)
        {
            node.last_dispatch = Some(now);
        }
        decision
    }
}
