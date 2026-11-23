pub mod framing;
#[cfg(feature = "engine")]
pub mod host_qos;
pub mod lcs_uax29;
pub mod teds_grits;

#[cfg(feature = "engine")]
pub mod bench_common {
    use crate::framing::DEFAULT_MAX_FRAME_BYTES;
    use athena_core_v2::projection::{BoilerplatePolicy, ParseLimits};
    use athena_lexbor_adapter_research::{LexborCharsetMode, transcode_html_whatwg_utf8};
    use std::borrow::Cow;
    use url::Url;

    pub const DEFAULT_MAX_INPUT_BYTES: usize = 33_554_432;
    pub const DEFAULT_SOFT_BUDGET_MS: u64 = 50;

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum Contender {
        Legacy,
        Breakthrough,
        LexborPure,
        LexborMinimal,
        LexborStageIr,
    }

    impl Contender {
        pub const ALL: [Self; 5] = [
            Self::Legacy,
            Self::Breakthrough,
            Self::LexborPure,
            Self::LexborMinimal,
            Self::LexborStageIr,
        ];

        pub fn parse(raw: &str) -> Result<Self, String> {
            match raw.trim().to_ascii_lowercase().as_str() {
                "legacy" | "legacy_html5ever" => Ok(Self::Legacy),
                "breakthrough" | "breakthrough_html5ever" => Ok(Self::Breakthrough),
                "lexbor_pure" | "lexbor_pure_minimal" => Ok(Self::LexborPure),
                "lexbor_minimal" | "lexbor_tree_minimal" => Ok(Self::LexborMinimal),
                "lexbor_stage_ir" | "lexbor_tree_stage_ir" => Ok(Self::LexborStageIr),
                other => Err(format!("unknown contender '{other}'")),
            }
        }

        pub fn as_str(self) -> &'static str {
            match self {
                Self::Legacy => "legacy",
                Self::Breakthrough => "breakthrough",
                Self::LexborPure => "lexbor_pure",
                Self::LexborMinimal => "lexbor_minimal",
                Self::LexborStageIr => "lexbor_stage_ir",
            }
        }
    }

    #[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
    pub enum CharsetMode {
        #[default]
        Utf8Assumed,
        SniffingEnabled,
    }

    impl CharsetMode {
        pub fn parse(raw: &str) -> Result<Self, String> {
            match raw.trim().to_ascii_lowercase().as_str() {
                "utf8_assumed" | "strict_utf8" => Ok(Self::Utf8Assumed),
                "sniffing_enabled" | "transcode_whatwg" => Ok(Self::SniffingEnabled),
                other => Err(format!(
                    "unknown charset mode '{other}' (expected strict_utf8, transcode_whatwg, utf8_assumed, or sniffing_enabled)"
                )),
            }
        }

        pub fn as_str(self) -> &'static str {
            match self {
                Self::Utf8Assumed => "utf8_assumed",
                Self::SniffingEnabled => "sniffing_enabled",
            }
        }

        pub fn to_lexbor_charset_mode(self) -> LexborCharsetMode {
            match self {
                Self::Utf8Assumed => LexborCharsetMode::StrictUtf8,
                Self::SniffingEnabled => LexborCharsetMode::TranscodeWhatwg,
            }
        }
    }

    pub fn is_disallowed_placeholder_host(host: &str) -> bool {
        let lower = host.trim_end_matches('.').to_ascii_lowercase();
        lower == "invalid"
            || lower.ends_with(".invalid")
            || lower == "example"
            || lower.ends_with(".example")
            || lower.starts_with("example.")
            || lower.contains(".example.")
    }

    pub fn validate_real_url(raw: &str) -> Result<Url, String> {
        let trimmed = raw.trim();
        if trimmed.is_empty() {
            return Err("missing required --url <real_url>".to_owned());
        }
        let parsed = Url::parse(trimmed).map_err(|e| format!("invalid --url '{trimmed}': {e}"))?;
        match parsed.scheme() {
            "http" | "https" => {}
            other => {
                return Err(format!(
                    "invalid --url '{trimmed}': unsupported scheme '{other}' (expected http or https)"
                ));
            }
        }
        let host = parsed
            .host_str()
            .filter(|h| !h.is_empty())
            .ok_or_else(|| format!("invalid --url '{trimmed}': missing host"))?;
        if is_disallowed_placeholder_host(host) {
            return Err(format!(
                "invalid --url '{trimmed}': placeholder host '{host}' (.invalid / example.) is forbidden"
            ));
        }
        Ok(parsed)
    }

    pub fn parse_boilerplate_policy(raw: &str) -> Result<BoilerplatePolicy, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "main_content_v1" | "maincontentv1" => Ok(BoilerplatePolicy::MainContentV1),
            "full_dom" | "fulldom" | "none" => Ok(BoilerplatePolicy::FullDom),
            "cetd_entropy_v1" | "cetdentropyv1" => Ok(BoilerplatePolicy::CetdEntropyV1),
            other => Err(format!(
                "unknown --boilerplate-policy '{other}' (expected main_content_v1, full_dom, or cetd_entropy_v1)"
            )),
        }
    }

    pub fn boilerplate_policy_str(policy: BoilerplatePolicy) -> &'static str {
        match policy {
            BoilerplatePolicy::MainContentV1 => "MainContentV1",
            BoilerplatePolicy::FullDom => "FullDom",
            BoilerplatePolicy::CetdEntropyV1 => "CetdEntropyV1",
        }
    }

    pub fn boilerplate_policy_cli_str(policy: BoilerplatePolicy) -> &'static str {
        match policy {
            BoilerplatePolicy::MainContentV1 => "main_content_v1",
            BoilerplatePolicy::FullDom => "full_dom",
            BoilerplatePolicy::CetdEntropyV1 => "cetd_entropy_v1",
        }
    }

    pub fn build_parse_limits_with_policy(
        max_input_bytes: usize,
        boilerplate: BoilerplatePolicy,
    ) -> Result<ParseLimits, String> {
        if max_input_bytes == 0 || max_input_bytes > DEFAULT_MAX_FRAME_BYTES {
            return Err(format!(
                "invalid --max-input-bytes {max_input_bytes}: expected 1..={DEFAULT_MAX_FRAME_BYTES}"
            ));
        }
        Ok(ParseLimits {
            max_html_bytes: max_input_bytes,
            max_text_bytes: max_input_bytes,
            max_link_bytes: max_input_bytes,
            boilerplate,
            ..ParseLimits::default()
        })
    }

    pub fn build_parse_limits(max_input_bytes: usize) -> Result<ParseLimits, String> {
        build_parse_limits_with_policy(max_input_bytes, BoilerplatePolicy::MainContentV1)
    }

    fn has_utf16_bom(bytes: &[u8]) -> bool {
        bytes.len() >= 2
            && ((bytes[0] == 0xFE && bytes[1] == 0xFF) || (bytes[0] == 0xFF && bytes[1] == 0xFE))
    }

    /// Strict-UTF8 or WHATWG-sniffed HTML decoding without `String::from_utf8_lossy`.
    pub fn decode_html_bytes<'a>(
        raw_body: &'a [u8],
        raw_headers: &[u8],
        max_input_bytes: usize,
        charset_mode: CharsetMode,
    ) -> Result<Cow<'a, str>, String> {
        if raw_body.len() > max_input_bytes {
            return Err(format!(
                "input payload ({} bytes) exceeds max_input_bytes ({max_input_bytes})",
                raw_body.len()
            ));
        }
        match charset_mode {
            CharsetMode::Utf8Assumed => {
                let s = std::str::from_utf8(raw_body)
                    .map_err(|_| "non-UTF-8 HTML bytes rejected in utf8_assumed mode".to_owned())?;
                Ok(Cow::Borrowed(s))
            }
            CharsetMode::SniffingEnabled => {
                if raw_headers.is_empty()
                    && !has_utf16_bom(raw_body)
                    && let Ok(valid_utf8) = std::str::from_utf8(raw_body)
                {
                    let prefix_len = valid_utf8.len().min(2048);
                    let ascii_only = raw_body.iter().all(|&b| b < 0x80);
                    let has_meta_charset = raw_body[..prefix_len]
                        .windows(7)
                        .any(|w| w.eq_ignore_ascii_case(b"charset"));
                    if ascii_only || !has_meta_charset {
                        let trimmed = valid_utf8.strip_prefix('\u{FEFF}').unwrap_or(valid_utf8);
                        return Ok(Cow::Borrowed(trimmed));
                    }
                }
                let max_out = max_input_bytes.saturating_mul(3).min(u32::MAX as usize);
                let decoded = transcode_html_whatwg_utf8(raw_body, raw_headers, max_out)
                    .map_err(|e| format!("WHATWG charset transcode failed: {e:?}"))?;
                Ok(Cow::Owned(decoded))
            }
        }
    }

    pub fn effective_limits(base: ParseLimits, decoded_len: usize) -> ParseLimits {
        ParseLimits {
            max_html_bytes: base.max_html_bytes.max(decoded_len),
            max_text_bytes: base.max_text_bytes.max(decoded_len),
            max_link_bytes: base.max_link_bytes.max(decoded_len),
            ..base
        }
    }
}

#[cfg(all(test, feature = "engine"))]
mod bench_common_tests {
    use super::bench_common::*;
    use athena_core_v2::projection::BoilerplatePolicy;

    #[test]
    fn validate_real_url_rejects_placeholders_and_accepts_real_http_urls() {
        assert!(validate_real_url("").is_err());
        assert!(validate_real_url("https://bench.invalid/doc").is_err());
        assert!(validate_real_url("https://example.org/page").is_err());
        assert!(validate_real_url("https://legacy.example.jp/sjis").is_err());
        assert!(validate_real_url("https://example.com/test").is_err());
        assert!(validate_real_url("ftp://kernel.org/pub").is_err());
        let ok = validate_real_url("https://en.wikipedia.org/wiki/Html").expect("valid real url");
        assert_eq!(ok.host_str(), Some("en.wikipedia.org"));
    }

    #[test]
    fn build_parse_limits_enforces_main_content_v1_and_32_mib_default() {
        let lim = build_parse_limits(DEFAULT_MAX_INPUT_BYTES).expect("32 MiB limits");
        assert_eq!(lim.max_html_bytes, 33_554_432);
        assert_eq!(lim.max_text_bytes, 33_554_432);
        assert_eq!(lim.boilerplate, BoilerplatePolicy::MainContentV1);
        assert!(build_parse_limits(0).is_err());
    }

    #[test]
    fn decode_html_bytes_enforces_strict_utf8_and_whatwg_sniffing() {
        let sjis = b"<html><body><p>\x93\xfa\x96\x7b\x8c\xea</p></body></html>";
        let hdrs = b"Content-Type: text/html; charset=Shift_JIS\r\n";
        assert!(
            decode_html_bytes(
                sjis,
                hdrs,
                DEFAULT_MAX_INPUT_BYTES,
                CharsetMode::Utf8Assumed
            )
            .is_err()
        );
        let dec = decode_html_bytes(
            sjis,
            hdrs,
            DEFAULT_MAX_INPUT_BYTES,
            CharsetMode::SniffingEnabled,
        )
        .expect("Shift_JIS decodes in sniffing_enabled mode");
        assert!(dec.contains("日本語"), "decoded={dec}");
    }

    #[test]
    fn charset_and_boilerplate_cli_aliases_parse_cleanly() {
        assert_eq!(
            CharsetMode::parse("strict_utf8").unwrap(),
            CharsetMode::Utf8Assumed
        );
        assert_eq!(
            CharsetMode::parse("transcode_whatwg").unwrap(),
            CharsetMode::SniffingEnabled
        );
        assert_eq!(
            parse_boilerplate_policy("main_content_v1").unwrap(),
            BoilerplatePolicy::MainContentV1
        );
        assert_eq!(
            parse_boilerplate_policy("full_dom").unwrap(),
            BoilerplatePolicy::FullDom
        );
        assert_eq!(
            parse_boilerplate_policy("cetd_entropy_v1").unwrap(),
            BoilerplatePolicy::CetdEntropyV1
        );
        assert!(parse_boilerplate_policy("bogus").is_err());
        let full =
            build_parse_limits_with_policy(DEFAULT_MAX_INPUT_BYTES, BoilerplatePolicy::FullDom)
                .expect("full_dom limits");
        assert_eq!(full.boilerplate, BoilerplatePolicy::FullDom);
    }
}
