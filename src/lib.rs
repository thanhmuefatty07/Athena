#![forbid(unsafe_code)]
//! Source-first reference implementation: no WAF evasion, no fingerprint impersonation.
#[cfg(any(
    all(feature = "tls-aws-lc", feature = "tls-ring"),
    not(any(feature = "tls-aws-lc", feature = "tls-ring"))
))]
compile_error!("select exactly one TLS provider feature: tls-aws-lc or tls-ring");

pub mod aae;
pub mod arena;
pub mod bandit;
pub mod ip_policy;
pub mod network;
pub mod orchestrator;
pub mod projection;
pub mod rank;
pub mod singleflight;
pub mod table;
