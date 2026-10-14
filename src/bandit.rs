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
        self.tiers[tier as usize].observe(Instant::now(), success, latency_ms, self.half_life);
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
}
