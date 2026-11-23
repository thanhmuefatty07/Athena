#![cfg(feature = "engine")]

use athena_core_v2::aae::export_contrastive_training_records;
use athena_core_v2::bandit::{
    FetchOutcomeKind, ProxyEndpointConfig, ProxyPoolRouter, RoutingAlgorithm, Tier,
};
use athena_core_v2::projection::{
    BoilerplatePolicy, Chunk, ParseLimits, normalize_unicode_nfc, parse_html_breakthrough,
    parse_html_with_limits,
};
use athena_core_v2::rank::{blended_background_idf, bm25f_fused_single_pass};
use rand::SeedableRng;
use rand::rngs::StdRng;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};
use url::Url;

fn test_url() -> Url {
    Url::parse("https://www.w3.org/TR/html52/").unwrap()
}

#[test]
fn g19_01_custom_element_markdown_body_and_astro_island_wipeout() {
    let html = "<html><body><main><markdown-body><h1>README Architecture</h1><p>Critical specification paragraph inside markdown-body custom element.</p></markdown-body><astro-island><p>Hydrated component paragraph inside astro-island.</p></astro-island></main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    assert!(
        bt.projection.chunks.is_empty(),
        "expected G19#1 custom-element block wipeout in frozen src/, got {:?}",
        bt.projection.chunks
    );
}

#[test]
fn g19_02_block_in_inline_anchor_heading_wipeout() {
    let html = "<html><body><main><div><a href=\"https://www.w3.org/TR/post\"><h3>Card Headline Inside Anchor</h3><p>Detailed summary paragraph nested inside anchor wrapper.</p></a></div></main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    assert!(
        bt.projection.chunks.is_empty(),
        "expected G19#2 <a><h3> block-in-inline wipeout in frozen src/, got {:?}",
        bt.projection.chunks
    );
}

#[test]
fn g19_03_dead_script_math_tex_skip_subtree() {
    let html = "<html><body><main><p>Mass-energy equivalence:</p><script type=\"math/tex\">E = mc^2</script></main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    let joined: String = bt
        .projection
        .chunks
        .iter()
        .map(|c| c.text.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(
        !joined.contains("mc^2"),
        "expected G19#3 dead <script type=\"math/tex\"> branch in frozen src/, got {joined}"
    );
}

#[test]
fn g19_04_multi_tbody_next_grp_1_collapse_rowspan_zero_bleed() {
    let html = "<html><body><main><table>\
        <tbody><tr><td rowspan=\"0\">GroupOneSpan</td><td>G1R1C2</td></tr><tr><td>G1R2C2</td></tr></tbody>\
        <tbody><tr><td>G2R1C1</td><td>G2R1C2</td></tr></tbody>\
        </table></main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    assert_eq!(bt.tables.len(), 1);
    let grid = &bt.tables[0];
    assert_eq!(grid.slots.len(), 3);
    // Row 2 belongs to the SECOND <tbody>; under WHATWG §4.9.12 rowspan="0" ends at tbody group 1,
    // so row 2 must have 2 columns, NOT 3 columns with GroupOneSpan bleeding into col 0!
    let span_cell_idx = grid.slots[0][0].unwrap();
    assert_eq!(
        grid.slots[2][0],
        Some(span_cell_idx),
        "expected G19#4 multi-tbody grp=1 collapse causing rowspan=0 bleed into second tbody"
    );
}

#[test]
fn g19_05_nested_block_text_order_inversion() {
    let html = "<html><body><main><ul><li><p>First nested paragraph</p> Second trailing text</li></ul></main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    assert!(bt.projection.chunks.len() >= 2);
    assert!(
        bt.projection.chunks[0].text.contains("Second")
            && bt.projection.chunks[1].text.contains("First"),
        "expected G19#5 LIFO stack text-order inversion, got {:?}",
        bt.projection.chunks
    );
}

#[test]
fn g19_06_form_h5_h6_dl_drops_and_cetd_entropy_overwrite() {
    // (a) <h5> and <h6> omitted in Tag::role() in parse_html_with_limits
    let h56 = "<html><body><main><h5>Subheading Level Five</h5><h6>Subheading Level Six</h6></main></body></html>";
    let leg_h56 = parse_html_with_limits(h56, &test_url(), ParseLimits::default()).unwrap();
    assert!(
        leg_h56.chunks.is_empty(),
        "expected G19#6a h5/h6 drop in legacy role()"
    );

    // (b) <form> wrapping <main> wipes out entire page under MainContentV1 in breakthrough
    let form_main = "<html><body><form action=\"/postback\"><main><h1>ASP.NET Article</h1><p>Full body text inside form wrapper.</p></main></form></body></html>";
    let bt_form = parse_html_breakthrough(
        form_main,
        &test_url(),
        ParseLimits {
            boilerplate: BoilerplatePolicy::MainContentV1,
            ..ParseLimits::default()
        },
    )
    .unwrap();
    assert!(
        bt_form.projection.chunks.is_empty(),
        "expected G19#6b <form> wrapper wipeout"
    );

    // (c) Direct text inside <dl> dropped by container whitelist
    let dl_html =
        "<html><body><main><dl>Orphan definition list overview text</dl></main></body></html>";
    let bt_dl = parse_html_breakthrough(dl_html, &test_url(), ParseLimits::default()).unwrap();
    assert!(
        bt_dl.projection.chunks.is_empty(),
        "expected G19#6c <dl> direct text drop"
    );

    // (d) CetdEntropyV1 overwritten at projection.rs:3389 in parse_html_breakthrough
    let cetd_html = "<html><body><h1>Heading</h1><p>Detailed primary article paragraph with realistic vocabulary.</p><p>All rights reserved Privacy Policy</p></body></html>";
    let leg_cetd = parse_html_with_limits(
        cetd_html,
        &test_url(),
        ParseLimits {
            boilerplate: BoilerplatePolicy::CetdEntropyV1,
            ..ParseLimits::default()
        },
    )
    .unwrap();
    let bt_cetd = parse_html_breakthrough(
        cetd_html,
        &test_url(),
        ParseLimits {
            boilerplate: BoilerplatePolicy::CetdEntropyV1,
            ..ParseLimits::default()
        },
    )
    .unwrap();
    assert_eq!(
        leg_cetd.chunks.len(),
        2,
        "legacy CetdEntropyV1 prunes footer"
    );
    assert_eq!(
        bt_cetd.projection.chunks.len(),
        3,
        "expected G19#6d CetdEntropyV1 overwrite leaving footer unpruned in breakthrough"
    );
}

#[test]
fn g19_07_cetd_entropy_utf8_byte_entropy_and_1_link_cjk_deletion() {
    let cjk_html = "<html><body><h1>研究概要</h1><p>本研究では分散トランザクション処理における厳密な直列化可能性と低遅延合意プロトコルの設計を検証し<a href=\"https://www.jstage.jst.go.jp/paper\">先行研究</a>と比較する。</p></body></html>";
    let main_v1 = parse_html_with_limits(
        cjk_html,
        &test_url(),
        ParseLimits {
            boilerplate: BoilerplatePolicy::MainContentV1,
            ..ParseLimits::default()
        },
    )
    .unwrap();
    let cetd_v1 = parse_html_with_limits(
        cjk_html,
        &test_url(),
        ParseLimits {
            boilerplate: BoilerplatePolicy::CetdEntropyV1,
            ..ParseLimits::default()
        },
    )
    .unwrap();
    assert_eq!(main_v1.chunks.len(), 2);
    assert_eq!(
        cetd_v1.chunks.len(),
        1,
        "expected G19#7 1-link unspaced CJK paragraph deletion under split_whitespace() link_density=1.0"
    );
}

#[test]
fn g19_08_vietnamese_u_dot_below_corruption_and_uax15_equal_ccc_blocking() {
    // 'u' + U+0323 (COMBINING DOT BELOW) must be 'ụ' (U+1EE5), NOT horned 'ự' (U+1EF1)
    let actual_u_dot = normalize_unicode_nfc("u\u{0323}");
    assert_eq!(
        actual_u_dot.as_ref(),
        "\u{1EF1}",
        "expected G19#8a 'u'+U+0323 -> 'ự' corruption in frozen projection.rs:2026"
    );
    assert_ne!(actual_u_dot.as_ref(), "\u{1EE5}");

    // UAX #15 D115/D117: 'e' + U+030B (CCC=230) + U+0301 (CCC=230): U+0301 is blocked by equal-CCC U+030B
    let actual_blocked = normalize_unicode_nfc("e\u{030B}\u{0301}");
    assert_eq!(
        actual_blocked.as_ref(),
        "\u{00E9}\u{030B}",
        "expected G19#8b illegal composition across equal-CCC blocker in frozen projection.rs:2179"
    );
}

#[test]
fn g19_09_utf8_byte_length_idf_and_camelcase_triple_tf() {
    // 3 CJK chars (9 UTF-8 bytes) vs 3 ASCII chars (3 UTF-8 bytes) at identical (N, df)
    let idf_cjk_3chars = blended_background_idf(20.0, 1.0, "東京駅");
    let idf_ascii_3chars = blended_background_idf(20.0, 1.0, "nyc");
    assert!(
        idf_cjk_3chars > idf_ascii_3chars + 0.05,
        "expected G19#9a UTF-8 byte-length IDF bias: cjk={idf_cjk_3chars} vs ascii={idf_ascii_3chars}"
    );

    // camelCase query "parseHtml" expands into ["parsehtml", "parse", "html"] hitting 1 token 3x + proximity bonus
    let chunks = vec![
        Chunk {
            node: 1,
            role: "paragraph",
            text: "invoke parseHtml routine".into(),
            links: Vec::new(),
        },
        Chunk {
            node: 2,
            role: "paragraph",
            text: "background filler paragraph without query terms".into(),
            links: Vec::new(),
        },
        Chunk {
            node: 3,
            role: "paragraph",
            text: "another unrelated control paragraph".into(),
            links: Vec::new(),
        },
    ];
    let camel_scores = bm25f_fused_single_pass(&chunks, "parseHtml", 1.2, 0.75, 0.5);
    let plain_scores = bm25f_fused_single_pass(&chunks, "routine", 1.2, 0.75, 0.5);
    let s_camel = camel_scores
        .iter()
        .find(|&&(idx, _)| idx == 0)
        .map(|x| x.1)
        .unwrap_or(0.0);
    let s_plain = plain_scores
        .iter()
        .find(|&&(idx, _)| idx == 0)
        .map(|x| x.1)
        .unwrap_or(0.0);
    assert!(
        s_camel > s_plain * 1.70,
        "expected G19#9b camelCase triple-TF + proximity inflation: s_camel={s_camel} vs s_plain={s_plain}"
    );
}

#[test]
fn g19_10_zero_score_contrastive_negative_admission() {
    let html = "<html><body><main>\
        <p>Quantum eigenvalue theorem proof for Hermitian operators.</p>\
        <p>Unrelated culinary recipe for sourdough bread baking.</p>\
        <p>Completely disjoint botanical gardening notes on orchids.</p>\
        </main></body></html>";
    let bt = parse_html_breakthrough(html, &test_url(), ParseLimits::default()).unwrap();
    let records =
        export_contrastive_training_records(&bt, &test_url(), "quantum_eigenvalue_hermitian", 1, 2);
    assert_eq!(records.len(), 1);
    assert!(
        records[0].hard_negatives.iter().any(|neg| neg.score == 0.0),
        "expected G19#10 admission of 0.0-score contrastive negatives in aae.rs:486, got {:?}",
        records[0].hard_negatives
    );
}

fn find_rlib(deps_dir: &Path, prefix: &str) -> PathBuf {
    let mut matches: Vec<PathBuf> = fs::read_dir(deps_dir)
        .unwrap()
        .filter_map(|e| e.ok().map(|x| x.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with(prefix) && n.ends_with(".rlib"))
        })
        .collect();
    matches.sort();
    matches
        .pop()
        .unwrap_or_else(|| panic!("missing {prefix}*.rlib in {}", deps_dir.display()))
}

#[test]
fn g19_11_exp3_floor_collapse_and_cli_invalid_url_panic_exit_101() {
    // Part A: EXP3 weight decay 0.985 vs exp(0.07 * r / K) collapses all K >= 5 arms to 1e-4 floor
    let epoch = Instant::now();
    let configs: Vec<ProxyEndpointConfig> = (0..6)
        .map(|id| ProxyEndpointConfig {
            id,
            tier: Tier::Direct,
            cost_usd_per_attempt: 0.0001,
            egress_usd_per_mib: 0.01,
            prior_latency_ms: 25.0,
            eligible: true,
        })
        .collect();
    let mut router = ProxyPoolRouter::new(epoch, configs, RoutingAlgorithm::Exp3Adversarial, 0.0);
    let mut rng = StdRng::seed_from_u64(20261111);
    for step in 0..4500u64 {
        let now = epoch + Duration::from_millis(step * 100);
        let dec = router.select_route(&mut rng, now).unwrap();
        let outcome = if dec.primary_id == 0 || step % 2 == 0 {
            FetchOutcomeKind::Success
        } else {
            FetchOutcomeKind::SoftBlockChallenge
        };
        router.observe(dec.primary_id, now, outcome, 25.0, 4096);
    }
    let at_exact_floor = router
        .nodes()
        .iter()
        .filter(|n| (n.exp3_weight - 1e-4).abs() < 1e-12)
        .count();
    assert!(
        at_exact_floor >= 3
            && router
                .nodes()
                .iter()
                .all(|n| (1e-4..=1.15e-4).contains(&n.exp3_weight)),
        "expected G19#11a EXP3 collapse to 1e-4 floor for K=6 (even optimal arm 0!), got weights {:?}",
        router
            .nodes()
            .iter()
            .map(|n| n.exp3_weight)
            .collect::<Vec<_>>()
    );

    // Part B: src/main.rs:40 `.expect("Invalid base URL")` exits with code 101 on malformed URL
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir.parent().unwrap().parent().unwrap();
    let main_rs = repo_root.join("src").join("main.rs");
    let deps_dir = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let core_rlib = find_rlib(&deps_dir, "libathena_core_v2-");
    let url_rlib = find_rlib(&deps_dir, "liburl-");

    let tmp_dir = std::env::temp_dir().join(format!("athena-g19-cli-{}", std::process::id()));
    let _ = fs::create_dir_all(&tmp_dir);
    let bin_path = tmp_dir.join("athena_main_probe");
    let html_path = tmp_dir.join("sample.html");
    fs::write(&html_path, "<html><body><p>Hello</p></body></html>").unwrap();

    let rustc = std::env::var("RUSTC").unwrap_or_else(|_| "rustc".into());
    let compile_status = Command::new(rustc)
        .arg("--edition=2024")
        .arg(&main_rs)
        .arg("-o")
        .arg(&bin_path)
        .arg("-L")
        .arg(format!("dependency={}", deps_dir.display()))
        .arg("--extern")
        .arg(format!("athena_core_v2={}", core_rlib.display()))
        .arg("--extern")
        .arg(format!("url={}", url_rlib.display()))
        .status()
        .expect("failed to invoke rustc on src/main.rs");
    assert!(compile_status.success(), "rustc link of src/main.rs failed");

    let out = Command::new(&bin_path)
        .arg(&html_path)
        .arg("not-a-valid-url")
        .output()
        .expect("failed to execute compiled src/main.rs binary");
    let _ = fs::remove_dir_all(&tmp_dir);

    assert_eq!(
        out.status.code(),
        Some(101),
        "expected G19#11b exit-101 panic on invalid CLI URL, got status {:?}, stderr={}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    assert!(
        String::from_utf8_lossy(&out.stderr).contains("Invalid base URL"),
        "expected Invalid base URL panic message"
    );
}

#[test]
fn three_mode_binaries_pass_self_tests() {
    for (name, bin) in [
        ("mode_a_cpu", env!("CARGO_BIN_EXE_mode_a_cpu")),
        ("mode_a_mem", env!("CARGO_BIN_EXE_mode_a_mem")),
        ("mode_b_pool", env!("CARGO_BIN_EXE_mode_b_pool")),
    ] {
        let out = Command::new(bin)
            .arg("--self-test")
            .output()
            .unwrap_or_else(|e| panic!("failed to run {name}: {e}"));
        assert!(
            out.status.success(),
            "{name} --self-test failed (status {:?}):\nstdout: {}\nstderr: {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        );
    }
}
