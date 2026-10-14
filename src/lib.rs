#![forbid(unsafe_code)]
//! Source-first reference implementation: no WAF evasion, no fingerprint impersonation.
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
