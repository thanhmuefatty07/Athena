//! Preliminary public-address classifier ONLY, not a complete SSRF guard.
//! Production must also enforce URL scheme, DNS-to-connect pinning,
//! redirects, proxy policy, and network egress firewall rules.
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

#[inline]
fn prefix4(ip: Ipv4Addr, base: [u8; 4], bits: u8) -> bool {
    let mask = if bits == 0 {
        0
    } else {
        u32::MAX << (32 - bits)
    };
    (u32::from(ip) & mask) == (u32::from_be_bytes(base) & mask)
}

pub fn is_public_candidate(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => public_ipv4(v4),
        IpAddr::V6(v6) => public_ipv6(v6),
    }
}

pub fn public_ipv4(ip: Ipv4Addr) -> bool {
    // Conservative list. IANA registry is authoritative and evolves.
    const DENY: &[([u8; 4], u8)] = &[
        ([0, 0, 0, 0], 8),
        ([10, 0, 0, 0], 8),
        ([100, 64, 0, 0], 10),
        ([127, 0, 0, 0], 8),
        ([169, 254, 0, 0], 16),
        ([172, 16, 0, 0], 12),
        ([192, 0, 0, 0], 24),
        ([192, 0, 2, 0], 24),
        ([192, 31, 196, 0], 24),
        ([192, 52, 193, 0], 24),
        ([192, 88, 99, 0], 24),
        ([192, 168, 0, 0], 16),
        ([192, 175, 48, 0], 24),
        ([198, 18, 0, 0], 15),
        ([198, 51, 100, 0], 24),
        ([203, 0, 113, 0], 24),
        ([224, 0, 0, 0], 4),
        ([240, 0, 0, 0], 4),
    ];
    !DENY.iter().any(|&(base, bits)| prefix4(ip, base, bits))
}

pub fn public_ipv6(ip: Ipv6Addr) -> bool {
    if let Some(v4) = ip.to_ipv4_mapped() {
        return public_ipv4(v4);
    }
    let value = u128::from(ip);
    let in_prefix =
        |base: u128, bits: u32| -> bool { (value >> (128 - bits)) == (base >> (128 - bits)) };
    // 2000::/3 is the allocated IPv6 global-unicast region, not proof
    // of global reachability. Exclude some well-known special-use ranges.
    if !in_prefix(0x20000000000000000000000000000000, 3) {
        return false;
    }
    if in_prefix(0x3ffe0000000000000000000000000000, 16) {
        return false;
    }
    if in_prefix(0x20010db8000000000000000000000000, 32) {
        return false;
    }
    if in_prefix(0x20020000000000000000000000000000, 16) {
        return false;
    }
    if in_prefix(0x3fff0000000000000000000000000000, 20) {
        return false;
    }
    if in_prefix(0x20010000000000000000000000000000, 23)
        || in_prefix(0x2620004f800000000000000000000000, 48)
    {
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn test_ipv4_exclusions() {
        for ip in [
            "0.1.2.3",
            "10.0.0.1",
            "100.100.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "172.31.255.1",
            "192.0.2.1",
            "198.19.2.3",
            "240.0.0.1",
        ] {
            assert!(!is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
        assert!(is_public_candidate("8.8.8.8".parse().unwrap()));
    }

    #[test]
    fn iana_special_purpose_ipv4_destinations_remain_denied_by_ssrf_policy() {
        for ip in [
            "192.0.0.8",
            "192.0.0.9",
            "192.0.0.10",
            "192.0.0.11",
            "192.0.0.170",
            "192.0.0.171",
            "192.31.196.1",
            "192.52.193.1",
            "192.175.48.1",
        ] {
            assert!(!is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
        for ip in [
            "::ffff:192.0.0.8",
            "::ffff:192.0.0.9",
            "::ffff:192.0.0.10",
            "::ffff:192.0.0.11",
        ] {
            assert!(!is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
        for ip in [
            "192.31.195.255",
            "192.31.197.0",
            "192.52.192.255",
            "192.52.194.0",
            "192.175.47.255",
            "192.175.49.0",
        ] {
            assert!(is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
    }

    #[test]
    fn test_ipv6_exclusions() {
        for ip in [
            "::1",
            "fc00::1",
            "fe80::1",
            "2001:db8::1",
            "::ffff:127.0.0.1",
            "2002::1",
        ] {
            assert!(!is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
        assert!(is_public_candidate("2606:4700:4700::1111".parse().unwrap()));
    }
    #[test]
    fn iana_special_purpose_ipv6_destinations_are_denied_by_ssrf_policy() {
        for ip in [
            "2001:1::1",
            "2001:1::2",
            "2001:1::3",
            "2001:2::1",
            "2001:3::1",
            "2001:4:112::1",
            "2001:10::1",
            "2001:20::1",
            "2001:30::1",
            "2001:1ff::1",
            "2620:4f:8000::1",
            "3ffe::",
            "3ffe:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
            "3fff::1",
            "3fff:fff::1",
            "5f00::1",
        ] {
            assert!(!is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
        for ip in ["2001:200::1", "2001:4860:4860::8888", "2620:4f:8001::1"] {
            assert!(is_public_candidate(ip.parse().unwrap()), "{ip}");
        }
    }
}
