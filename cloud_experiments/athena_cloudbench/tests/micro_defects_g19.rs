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

fn rlib_matches_prefix(file_name: &str, prefix: &str) -> bool {
    let Some(without_ext) = file_name.strip_suffix(".rlib") else {
        return false;
    };
    let stem = prefix.trim_end_matches('-');
    without_ext == stem
        || without_ext
            .strip_prefix(stem)
            .is_some_and(|rest| rest.starts_with('-') && rest.len() > 1)
}

fn is_rustc_search_artifact(file_name: &str) -> bool {
    [".rlib", ".rmeta", ".so", ".dylib", ".dll", ".a"]
        .iter()
        .any(|ext| file_name.ends_with(ext))
}

const CARGO_TREE_MAX_DEPTH: usize = 12;

fn should_skip_cargo_subdir(name: &str) -> bool {
    matches!(
        name,
        ".fingerprint" | "fingerprint" | "incremental" | ".git" | "doc" | "package" | "tmp"
    ) || name.ends_with(".dSYM")
}

fn canonical_or_clean(path: &Path) -> PathBuf {
    fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

fn same_path_canonical(a: &Path, b: &Path) -> bool {
    a == b || canonical_or_clean(a) == canonical_or_clean(b)
}

fn path_is_within(child: &Path, parent: &Path) -> bool {
    child.starts_with(parent) || canonical_or_clean(child).starts_with(canonical_or_clean(parent))
}

fn active_profile_name(seed: &Path) -> Option<String> {
    for anc in seed.ancestors() {
        let Some(name) = anc.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if matches!(name, "debug" | "release") {
            return Some(name.to_owned());
        }
        if matches!(name, "deps" | "build" | "tests" | "examples")
            && let Some(parent_name) = anc
                .parent()
                .and_then(|p| p.file_name())
                .and_then(|s| s.to_str())
            && !parent_name.is_empty()
        {
            return Some(parent_name.to_owned());
        }
    }
    None
}

fn path_has_component(path: &Path, target: &str) -> bool {
    path.components()
        .any(|c| c.as_os_str().to_str() == Some(target))
}

fn push_unique_path(list: &mut Vec<PathBuf>, candidate: PathBuf) {
    if !list
        .iter()
        .any(|existing| same_path_canonical(existing, &candidate))
    {
        list.push(candidate);
    }
}

fn expand_cargo_search_roots(seed: &Path, stop_dirs: &[PathBuf]) -> Vec<PathBuf> {
    let mut roots = Vec::new();
    let is_stopped =
        |p: &Path| p.parent().is_none() || stop_dirs.iter().any(|s| same_path_canonical(s, p));
    if seed.exists() && !is_stopped(seed) {
        push_unique_path(&mut roots, seed.to_path_buf());
    }
    for (depth, anc) in seed.ancestors().enumerate() {
        if depth > 8 || is_stopped(anc) {
            break;
        }
        if anc.join("CACHEDIR.TAG").is_file() || anc.join(".rustc_info.json").is_file() {
            push_unique_path(&mut roots, anc.to_path_buf());
            break;
        }
        let Some(name) = anc.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if matches!(name, "debug" | "release") {
            push_unique_path(&mut roots, anc.to_path_buf());
            if let Some(target_dir) = anc.parent()
                && !is_stopped(target_dir)
                && (target_dir.join("CACHEDIR.TAG").is_file()
                    || target_dir.join(".rustc_info.json").is_file())
            {
                push_unique_path(&mut roots, target_dir.to_path_buf());
            }
        } else if matches!(name, "deps" | "build" | "tests" | "examples")
            && let Some(profile_dir) = anc.parent()
            && !is_stopped(profile_dir)
        {
            push_unique_path(&mut roots, profile_dir.to_path_buf());
            if let Some(target_dir) = profile_dir.parent()
                && !is_stopped(target_dir)
                && (target_dir.join("CACHEDIR.TAG").is_file()
                    || target_dir.join(".rustc_info.json").is_file()
                    || matches!(
                        profile_dir.file_name().and_then(|s| s.to_str()),
                        Some("debug" | "release")
                    ))
            {
                push_unique_path(&mut roots, target_dir.to_path_buf());
            }
        }
    }
    roots
}

fn resolve_env_target_dirs(
    raw_path: &Path,
    active_seeds: &[&Path],
    cwd: Option<&Path>,
    manifest_dir: &Path,
    repo_root: &Path,
) -> Vec<PathBuf> {
    if raw_path.as_os_str().is_empty() {
        return Vec::new();
    }
    if raw_path.is_absolute() {
        return vec![raw_path.to_path_buf()];
    }
    let mut candidates = Vec::new();
    if let Some(dir) = cwd {
        let joined = dir.join(raw_path);
        if joined.exists() {
            push_unique_path(&mut candidates, joined);
        }
    }
    for base in [manifest_dir, repo_root] {
        let joined = base.join(raw_path);
        if joined.exists() {
            push_unique_path(&mut candidates, joined);
        }
    }
    if candidates.is_empty() {
        if let Some(dir) = cwd {
            candidates.push(dir.join(raw_path));
        }
        return candidates;
    }

    let containing_active: Vec<PathBuf> = candidates
        .iter()
        .filter(|cand| {
            active_seeds
                .iter()
                .any(|seed| seed.exists() && path_is_within(seed, cand))
        })
        .cloned()
        .collect();
    if !containing_active.is_empty() {
        containing_active
    } else {
        candidates
    }
}

fn walk_cargo_tree(root: &Path, depth: usize, max_depth: usize, visit_file: &mut dyn FnMut(&Path)) {
    if depth > max_depth || !root.is_dir() {
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_dir() {
            let skip = path
                .file_name()
                .and_then(|s| s.to_str())
                .is_some_and(should_skip_cargo_subdir);
            if !skip {
                walk_cargo_tree(&path, depth + 1, max_depth, visit_file);
            }
        } else if ft.is_file() || (ft.is_symlink() && path.is_file()) {
            visit_file(&path);
        }
    }
}

fn extract_depinfo_rlibs(dep_file: &Path, prefix: &str, out: &mut Vec<PathBuf>) {
    let Ok(text) = fs::read_to_string(dep_file) else {
        return;
    };
    for token in text.split_whitespace() {
        let candidate = Path::new(token.trim_end_matches(':'));
        if candidate
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|n| rlib_matches_prefix(n, prefix))
            && candidate.is_file()
        {
            push_unique_path(out, candidate.to_path_buf());
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn find_rlibs_for_test_with_env(
    deps_dir: &Path,
    prefix: &str,
    cargo_target_dir: Option<&Path>,
    cargo_build_dir: Option<&Path>,
    current_exe: Option<&Path>,
    compile_time_bin: Option<&Path>,
    cwd: Option<&Path>,
    manifest_dir: &Path,
    repo_root: &Path,
) -> Vec<PathBuf> {
    let stop_dirs = vec![
        std::env::temp_dir(),
        manifest_dir.to_path_buf(),
        repo_root.to_path_buf(),
    ];
    let preferred_profile =
        active_profile_name(deps_dir).or_else(|| current_exe.and_then(active_profile_name));

    let mut active_seeds: Vec<&Path> = vec![deps_dir];
    if let Some(exe) = current_exe {
        active_seeds.push(exe);
    }

    let scan_roots = |roots: &[PathBuf], exe_for_depinfo: Option<&Path>| -> Vec<PathBuf> {
        let mut out = Vec::new();
        if let Some(exe) = exe_for_depinfo {
            extract_depinfo_rlibs(&exe.with_extension("d"), prefix, &mut out);
        }
        let mut discovered = Vec::new();
        for root in roots {
            walk_cargo_tree(root, 0, CARGO_TREE_MAX_DEPTH, &mut |file_path| {
                if file_path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|n| rlib_matches_prefix(n, prefix))
                {
                    push_unique_path(&mut discovered, file_path.to_path_buf());
                }
            });
        }
        discovered.sort_by(|a, b| {
            let prof_a = preferred_profile
                .as_deref()
                .is_some_and(|prof| path_has_component(a, prof));
            let prof_b = preferred_profile
                .as_deref()
                .is_some_and(|prof| path_has_component(b, prof));
            let ma = a.metadata().and_then(|m| m.modified()).ok();
            let mb = b.metadata().and_then(|m| m.modified()).ok();
            prof_b
                .cmp(&prof_a)
                .then_with(|| mb.cmp(&ma))
                .then_with(|| b.cmp(a))
        });
        for item in discovered {
            push_unique_path(&mut out, item);
        }
        out
    };

    let mut active_roots = expand_cargo_search_roots(deps_dir, &stop_dirs);
    if let Some(build_raw) = cargo_build_dir {
        for resolved in
            resolve_env_target_dirs(build_raw, &active_seeds, cwd, manifest_dir, repo_root)
        {
            for r in expand_cargo_search_roots(&resolved, &stop_dirs) {
                push_unique_path(&mut active_roots, r);
            }
            if resolved.is_dir() {
                push_unique_path(&mut active_roots, resolved);
            }
        }
    }

    let exe_in_active = current_exe.filter(|exe| {
        active_roots
            .iter()
            .any(|root| path_is_within(exe, root) || path_is_within(deps_dir, root))
    });
    let mut out = scan_roots(&active_roots, exe_in_active);

    let mut env_roots = Vec::new();
    if let Some(target_raw) = cargo_target_dir {
        for resolved in
            resolve_env_target_dirs(target_raw, &active_seeds, cwd, manifest_dir, repo_root)
        {
            for r in expand_cargo_search_roots(&resolved, &stop_dirs) {
                push_unique_path(&mut env_roots, r);
            }
            if resolved.is_dir() {
                push_unique_path(&mut env_roots, resolved);
            }
        }
    }
    if out.is_empty() && !env_roots.is_empty() {
        let exe_in_env = current_exe.filter(|exe| {
            env_roots
                .iter()
                .any(|root| path_is_within(exe, root) || path_is_within(deps_dir, root))
        });
        out = scan_roots(&env_roots, exe_in_env);
    }

    if out.is_empty() {
        let mut fallback_roots = Vec::new();
        for bin_path in [current_exe, compile_time_bin].into_iter().flatten() {
            if let Some(parent) = bin_path.parent() {
                for r in expand_cargo_search_roots(parent, &stop_dirs) {
                    push_unique_path(&mut fallback_roots, r);
                }
            }
        }
        out = scan_roots(&fallback_roots, current_exe);
    }

    assert!(
        !out.is_empty(),
        "missing {prefix}*.rlib in {} (active_roots={:?}, env_roots={:?})",
        deps_dir.display(),
        active_roots,
        env_roots
    );
    out
}

fn find_rlibs_for_current_test(deps_dir: &Path, prefix: &str) -> Vec<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .unwrap_or(manifest_dir.as_path())
        .to_path_buf();
    let target_env = std::env::var_os("CARGO_TARGET_DIR").map(PathBuf::from);
    let build_env = std::env::var_os("CARGO_BUILD_BUILD_DIR").map(PathBuf::from);
    let exe = std::env::current_exe().ok();
    let cwd = std::env::current_dir().ok();
    let compile_bin = Path::new(env!("CARGO_BIN_EXE_mode_a_cpu"));
    find_rlibs_for_test_with_env(
        deps_dir,
        prefix,
        target_env.as_deref(),
        build_env.as_deref(),
        exe.as_deref(),
        Some(compile_bin),
        cwd.as_deref(),
        &manifest_dir,
        &repo_root,
    )
}

#[allow(clippy::too_many_arguments)]
fn collect_rustc_link_search_dirs_with_env(
    deps_dir: &Path,
    extra_rlibs: &[&Path],
    cargo_target_dir: Option<&Path>,
    cargo_build_dir: Option<&Path>,
    current_exe: Option<&Path>,
    cwd: Option<&Path>,
    manifest_dir: &Path,
    repo_root: &Path,
) -> Vec<PathBuf> {
    let stop_dirs = vec![
        std::env::temp_dir(),
        manifest_dir.to_path_buf(),
        repo_root.to_path_buf(),
    ];
    let preferred_profile = active_profile_name(deps_dir)
        .or_else(|| extra_rlibs.first().and_then(|p| active_profile_name(p)))
        .or_else(|| current_exe.and_then(active_profile_name));

    let mut active_seeds: Vec<&Path> = vec![deps_dir];
    active_seeds.extend(extra_rlibs.iter().copied());
    if let Some(exe) = current_exe {
        active_seeds.push(exe);
    }

    let mut dirs = Vec::new();
    for rlib in extra_rlibs {
        if let Some(parent) = rlib.parent() {
            push_unique_path(&mut dirs, parent.to_path_buf());
        }
    }
    if deps_dir.is_dir() {
        push_unique_path(&mut dirs, deps_dir.to_path_buf());
    }

    let mut roots = expand_cargo_search_roots(deps_dir, &stop_dirs);
    for rlib in extra_rlibs {
        if let Some(parent) = rlib.parent() {
            for r in expand_cargo_search_roots(parent, &stop_dirs) {
                push_unique_path(&mut roots, r);
            }
        }
    }
    for env_opt in [cargo_target_dir, cargo_build_dir].into_iter().flatten() {
        for resolved in
            resolve_env_target_dirs(env_opt, &active_seeds, cwd, manifest_dir, repo_root)
        {
            for r in expand_cargo_search_roots(&resolved, &stop_dirs) {
                push_unique_path(&mut roots, r);
            }
            if resolved.is_dir() {
                push_unique_path(&mut roots, resolved);
            }
        }
    }

    let mut discovered_dirs = Vec::new();
    for root in &roots {
        walk_cargo_tree(root, 0, CARGO_TREE_MAX_DEPTH, &mut |file_path| {
            if file_path
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(is_rustc_search_artifact)
                && let Some(parent) = file_path.parent()
            {
                push_unique_path(&mut discovered_dirs, parent.to_path_buf());
            }
        });
    }
    discovered_dirs.sort_by(|a, b| {
        let prof_a = preferred_profile
            .as_deref()
            .is_some_and(|prof| path_has_component(a, prof));
        let prof_b = preferred_profile
            .as_deref()
            .is_some_and(|prof| path_has_component(b, prof));
        prof_b.cmp(&prof_a).then_with(|| a.cmp(b))
    });
    for d in discovered_dirs {
        push_unique_path(&mut dirs, d);
    }
    dirs
}

fn collect_rustc_link_search_dirs(deps_dir: &Path, extra_rlibs: &[&Path]) -> Vec<PathBuf> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let repo_root = manifest_dir
        .parent()
        .and_then(Path::parent)
        .unwrap_or(manifest_dir.as_path())
        .to_path_buf();
    let target_env = std::env::var_os("CARGO_TARGET_DIR").map(PathBuf::from);
    let build_env = std::env::var_os("CARGO_BUILD_BUILD_DIR").map(PathBuf::from);
    let exe = std::env::current_exe().ok();
    let cwd = std::env::current_dir().ok();
    collect_rustc_link_search_dirs_with_env(
        deps_dir,
        extra_rlibs,
        target_env.as_deref(),
        build_env.as_deref(),
        exe.as_deref(),
        cwd.as_deref(),
        &manifest_dir,
        &repo_root,
    )
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
    let core_rlibs = find_rlibs_for_current_test(&deps_dir, "libathena_core_v2-");
    let url_rlibs = find_rlibs_for_current_test(&deps_dir, "liburl-");

    let tmp_dir = std::env::temp_dir().join(format!("athena-g19-cli-{}", std::process::id()));
    let _ = fs::create_dir_all(&tmp_dir);
    let bin_path = tmp_dir.join("athena_main_probe");
    let html_path = tmp_dir.join("sample.html");
    fs::write(&html_path, "<html><body><p>Hello</p></body></html>").unwrap();

    let rustc = std::env::var("RUSTC").unwrap_or_else(|_| "rustc".into());
    let mut linked = false;
    let mut last_err = String::new();
    'pairs: for core_rlib in &core_rlibs {
        for url_rlib in &url_rlibs {
            let search_dirs = collect_rustc_link_search_dirs(
                &deps_dir,
                &[core_rlib.as_path(), url_rlib.as_path()],
            );
            let mut cmd = Command::new(&rustc);
            cmd.arg("--edition=2024")
                .arg(&main_rs)
                .arg("-o")
                .arg(&bin_path);
            for search_dir in &search_dirs {
                cmd.arg("-L").arg(search_dir);
            }
            let out = cmd
                .arg("--extern")
                .arg(format!("athena_core_v2={}", core_rlib.display()))
                .arg("--extern")
                .arg(format!("url={}", url_rlib.display()))
                .output()
                .expect("failed to invoke rustc on src/main.rs");
            if out.status.success() {
                linked = true;
                break 'pairs;
            }
            last_err = String::from_utf8_lossy(&out.stderr).into_owned();
        }
    }
    assert!(linked, "rustc link of src/main.rs failed: {last_err}");

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

    let bad_cpu = Command::new(env!("CARGO_BIN_EXE_mode_a_cpu"))
        .args([
            "--url",
            "https://en.wikipedia.org/wiki/Rust",
            "--contender",
            "lexbor_minimal",
            "--arm-b",
            "legacy",
            "--self-test",
        ])
        .output()
        .expect("run mode_a_cpu negative stage check");
    assert!(
        !bad_cpu.status.success(),
        "mode_a_cpu --contender lexbor_minimal --arm-b legacy must fail closed"
    );

    use std::io::Write as _;
    use std::process::Stdio;
    for (contender, exp_stage, exp_dom_only) in [
        ("breakthrough", "full_projection", false),
        ("lexbor_stage_ir", "full_projection", false),
        ("lexbor_minimal", "dom_parse_only", true),
    ] {
        let mut child = Command::new(env!("CARGO_BIN_EXE_mode_b_pool"))
            .args([
                "--url",
                "https://en.wikipedia.org/wiki/Rust",
                "--contender",
                contender,
                "--workers",
                "1",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn mode_b_pool");
        child
            .stdin
            .take()
            .unwrap()
            .write_all(b"<html><body><main><article><h1>Title</h1><p>Substantive body paragraph for live pool IPC test.</p></article></main></body></html>")
            .unwrap();
        let out = child.wait_with_output().unwrap();
        assert!(out.status.success(), "mode_b_pool {contender} failed");
        let row: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
        assert_eq!(row["stage"], exp_stage);
        assert_eq!(row["track_a_status"], "SKIPPED");
        assert!(row["track_a_elapsed_us"].is_null());
        assert_eq!(row["lexbor_pure_status"], "SKIPPED");
        assert!(row["lexbor_pure_elapsed_us"].is_null());
        assert!(row["wall_us"].as_u64().is_some());
        assert!(row["thread_cpu_us"].as_u64().is_some());
        assert_eq!(row["budget_clock"], "thread_cpu");
        if exp_dom_only {
            assert!(row["boilerplate_policy"].is_null());
            assert!(row["contender_chunks"].is_null());
            assert!(row["contender_dom_nodes"].as_u64().unwrap_or(0) > 0);
        } else {
            assert_eq!(row["boilerplate_policy"], "MainContentV1");
            assert!(row["contender_chunks"].as_u64().unwrap_or(0) > 0);
            assert!(row["contender_dom_nodes"].is_null());
        }
    }
}

#[test]
fn w1_3_custom_cargo_target_dir_resolves_rlibs_across_v1_and_v2_layouts() {
    let base_tmp = std::env::temp_dir().join(format!("athena-w1-3-target-{}", std::process::id()));
    let _ = fs::remove_dir_all(&base_tmp);
    let sim_repo_root = base_tmp.join("sim_repo");
    let sim_manifest_dir = sim_repo_root
        .join("cloud_experiments")
        .join("athena_cloudbench");
    fs::create_dir_all(&sim_manifest_dir).unwrap();

    let v1_root = base_tmp.join("v1_custom_target");
    let v1_deps = v1_root.join("debug").join("deps");
    let v1_tests = v1_root.join("debug").join("tests");
    fs::create_dir_all(&v1_deps).unwrap();
    fs::create_dir_all(&v1_tests).unwrap();
    fs::write(
        v1_root.join("CACHEDIR.TAG"),
        "Signature: 8a477f597d28d172789f06886806bc55\n",
    )
    .unwrap();
    let v1_core = v1_deps.join("libathena_core_v2-1122334455667788.rlib");
    let v1_url = v1_deps.join("liburl-8877665544332211.rlib");
    fs::write(&v1_core, b"rlib").unwrap();
    fs::write(&v1_url, b"rlib").unwrap();

    let found_v1_core = find_rlibs_for_test_with_env(
        &v1_tests,
        "libathena_core_v2-",
        Some(&v1_root),
        None,
        None,
        None,
        None,
        &sim_manifest_dir,
        &sim_repo_root,
    );
    let found_v1_url = find_rlibs_for_test_with_env(
        &v1_tests,
        "liburl-",
        Some(&v1_root),
        None,
        None,
        None,
        None,
        &sim_manifest_dir,
        &sim_repo_root,
    );
    assert_eq!(found_v1_core, vec![v1_core]);
    assert_eq!(found_v1_url, vec![v1_url]);

    let cwd_sim = base_tmp.join("workspace_cwd");
    let v2_rel = Path::new("test_target_new");
    let v2_root = cwd_sim.join(v2_rel);
    let stale_repo_target_deps = sim_repo_root.join(v2_rel).join("debug").join("deps");
    fs::create_dir_all(&stale_repo_target_deps).unwrap();
    fs::write(
        stale_repo_target_deps.join("libathena_core_v2-dead0000stale199.rlib"),
        b"stale_repo_root_decoy",
    )
    .unwrap();

    let v2_test_exe_dir = v2_root
        .join("debug")
        .join("build")
        .join("athena-cloudbench")
        .join("04f7db82c63dddb0")
        .join("out");
    let v2_core_dir = v2_root
        .join("debug")
        .join("build")
        .join("athena-core-v2")
        .join("aabbccddeeff0011")
        .join("out");
    let v2_url_dir = v2_root
        .join("debug")
        .join("build")
        .join("url")
        .join("9988776655443322")
        .join("out");
    let v2_bytes_dir = v2_root
        .join("debug")
        .join("build")
        .join("bytes")
        .join("1234567890abcdef")
        .join("out");
    let v2_decoy_url_dir = v2_root
        .join("debug")
        .join("build")
        .join("url-escape")
        .join("deadbeef00001111")
        .join("out");
    let v2_fp_dir = v2_root
        .join("debug")
        .join(".fingerprint")
        .join("athena-core-v2-deadbeef");
    let v2_unit_fp_dir = v2_root
        .join("debug")
        .join("build")
        .join("athena-core-v2")
        .join("aabbccddeeff0011")
        .join("fingerprint");

    for d in [
        &v2_test_exe_dir,
        &v2_core_dir,
        &v2_url_dir,
        &v2_bytes_dir,
        &v2_decoy_url_dir,
        &v2_fp_dir,
        &v2_unit_fp_dir,
    ] {
        fs::create_dir_all(d).unwrap();
    }
    fs::write(
        v2_root.join("CACHEDIR.TAG"),
        "Signature: 8a477f597d28d172789f06886806bc55\n",
    )
    .unwrap();

    let v2_core_unhashed = v2_core_dir.join("libathena_core_v2.rlib");
    let v2_url_hashed = v2_url_dir.join("liburl-9988776655443322.rlib");
    let v2_bytes_rmeta = v2_bytes_dir.join("libbytes.rmeta");
    fs::write(&v2_core_unhashed, b"rlib_v2").unwrap();
    fs::write(&v2_url_hashed, b"rlib_v2").unwrap();
    fs::write(&v2_bytes_rmeta, b"rmeta_v2").unwrap();
    fs::write(
        v2_decoy_url_dir.join("liburl_escape-deadbeef00001111.rlib"),
        b"decoy",
    )
    .unwrap();
    fs::write(
        v2_fp_dir.join("libathena_core_v2-deadbeef.rlib"),
        b"fingerprint_decoy",
    )
    .unwrap();
    fs::write(
        v2_unit_fp_dir.join("libathena_core_v2-unitfp.rlib"),
        b"unit_fingerprint_decoy",
    )
    .unwrap();

    let found_v2_core = find_rlibs_for_test_with_env(
        &v2_test_exe_dir,
        "libathena_core_v2-",
        Some(v2_rel),
        None,
        None,
        None,
        Some(&cwd_sim),
        &sim_manifest_dir,
        &sim_repo_root,
    );
    let found_v2_url = find_rlibs_for_test_with_env(
        &v2_test_exe_dir,
        "liburl-",
        Some(v2_rel),
        None,
        None,
        None,
        Some(&cwd_sim),
        &sim_manifest_dir,
        &sim_repo_root,
    );
    assert_eq!(found_v2_core, vec![v2_core_unhashed.clone()]);
    assert_eq!(found_v2_url, vec![v2_url_hashed.clone()]);

    let link_dirs = collect_rustc_link_search_dirs_with_env(
        &v2_test_exe_dir,
        &[v2_core_unhashed.as_path(), v2_url_hashed.as_path()],
        Some(v2_rel),
        None,
        None,
        Some(&cwd_sim),
        &sim_manifest_dir,
        &sim_repo_root,
    );
    assert!(link_dirs.contains(&v2_core_dir));
    assert!(link_dirs.contains(&v2_url_dir));
    assert!(link_dirs.contains(&v2_bytes_dir));
    assert!(!link_dirs.contains(&v2_fp_dir));
    assert!(!link_dirs.contains(&v2_unit_fp_dir));
    assert!(!link_dirs.contains(&stale_repo_target_deps));

    let split_target_deps = base_tmp.join("split_target").join("debug").join("deps");
    let split_build_root = base_tmp.join("external_build_cache");
    let split_build_core_dir = split_build_root
        .join("debug")
        .join("build")
        .join("athena-core-v2")
        .join("778899aabbccddee")
        .join("out");
    fs::create_dir_all(&split_target_deps).unwrap();
    fs::create_dir_all(&split_build_core_dir).unwrap();
    let split_core_rlib = split_build_core_dir.join("libathena_core_v2.rlib");
    fs::write(&split_core_rlib, b"split_build_dir_rlib").unwrap();
    let found_split_core = find_rlibs_for_test_with_env(
        &split_target_deps,
        "libathena_core_v2-",
        Some(&base_tmp.join("split_target")),
        Some(&split_build_root),
        None,
        None,
        None,
        &sim_manifest_dir,
        &sim_repo_root,
    );
    assert_eq!(found_split_core, vec![split_core_rlib]);

    let triple_root = base_tmp.join("triple_target");
    let triple_deps = triple_root
        .join("x86_64-unknown-linux-gnu")
        .join("release")
        .join("deps");
    fs::create_dir_all(&triple_deps).unwrap();
    let triple_core = triple_deps.join("libathena_core_v2-feedfacecafebeef.rlib");
    fs::write(&triple_core, b"rlib_triple").unwrap();
    let empty_probe = base_tmp.join("nonexistent_probe");
    let found_triple = find_rlibs_for_test_with_env(
        &empty_probe,
        "libathena_core_v2-",
        Some(&triple_root),
        None,
        None,
        None,
        None,
        &sim_manifest_dir,
        &sim_repo_root,
    );
    assert_eq!(found_triple, vec![triple_core]);

    let _ = fs::remove_dir_all(&base_tmp);
}

#[test]
fn w1_5_mode_a_mem_arm_selection_and_process_isolation_prevent_cross_arm_contamination() {
    let tmp_dir = std::env::temp_dir().join(format!("athena-w1-5-mem-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp_dir);
    fs::create_dir_all(&tmp_dir).unwrap();

    let html_path = tmp_dir.join("large_fixture.html");
    let mut html = String::with_capacity(450_000);
    html.push_str("<!doctype html><html><head><title>W1-5 Isolation</title></head><body><main>");
    for i in 0..2_000 {
        html.push_str(&format!(
            "<section class=\"sec-{i}\" data-meta=\"payload-{i}-0123456789abcdef\"><h2>Section {i}</h2><p>Detailed paragraph content for memory isolation test block {i} with <a href=\"https://en.wikipedia.org/wiki/Entry_{i}\">reference link {i}</a>.</p></section>"
        ));
    }
    html.push_str("</main></body></html>");
    fs::write(&html_path, html.as_bytes()).unwrap();

    let single_out = Command::new(env!("CARGO_BIN_EXE_mode_a_mem"))
        .args([
            "--arm",
            "lexbor_tree_minimal",
            "--url",
            "https://en.wikipedia.org/wiki/Memory_protection",
            "--html-file",
        ])
        .arg(&html_path)
        .output()
        .expect("run single-arm mode_a_mem");
    assert!(
        single_out.status.success(),
        "single-arm mode_a_mem failed: {}",
        String::from_utf8_lossy(&single_out.stderr)
    );
    let single_json: serde_json::Value = serde_json::from_slice(&single_out.stdout).unwrap();
    assert_eq!(single_json["arm"], "lexbor_tree_minimal");
    assert_eq!(single_json["arms"].as_object().unwrap().len(), 1);
    let standalone_before =
        single_json["arms"]["lexbor_tree_minimal"]["os_before"]["phys_footprint_bytes"]
            .as_u64()
            .unwrap();

    let iso_out = Command::new(env!("CARGO_BIN_EXE_mode_a_mem"))
        .args([
            "--arm",
            "all",
            "--isolate-arms",
            "--url",
            "https://en.wikipedia.org/wiki/Memory_protection",
            "--html-file",
        ])
        .arg(&html_path)
        .output()
        .expect("run isolated all-arms mode_a_mem");

    #[cfg(unix)]
    {
        use std::process::Stdio;
        let stdin_file = fs::File::open(&html_path).expect("open html_path for stdin redirect");
        let stdin_out = Command::new(env!("CARGO_BIN_EXE_mode_a_mem"))
            .args([
                "--arm",
                "all",
                "--isolate-arms",
                "--url",
                "https://en.wikipedia.org/wiki/Memory_protection",
                "--html-file",
                "/dev/stdin",
            ])
            .stdin(Stdio::from(stdin_file))
            .output()
            .expect("run isolated mode_a_mem with /dev/stdin file redirect");
        assert!(
            stdin_out.status.success(),
            "isolated /dev/stdin mode_a_mem failed: {}",
            String::from_utf8_lossy(&stdin_out.stderr)
        );
        let stdin_json: serde_json::Value = serde_json::from_slice(&stdin_out.stdout).unwrap();
        assert_eq!(stdin_json["html_bytes"].as_u64(), Some(html.len() as u64));
        assert_eq!(stdin_json["arms"].as_object().unwrap().len(), 5);
    }

    let _ = fs::remove_dir_all(&tmp_dir);
    assert!(
        iso_out.status.success(),
        "isolated mode_a_mem failed: {}",
        String::from_utf8_lossy(&iso_out.stderr)
    );
    let iso_json: serde_json::Value = serde_json::from_slice(&iso_out.stdout).unwrap();
    assert_eq!(iso_json["arm"], "all");
    assert_eq!(iso_json["isolate_arms"], true);
    assert_eq!(iso_json["arm_isolation"], "process_per_arm");

    let arms = iso_json["arms"].as_object().unwrap();
    assert_eq!(arms.len(), 5);
    for key in [
        "legacy_html5ever",
        "breakthrough_html5ever",
        "lexbor_pure_minimal",
        "lexbor_pure_stage_ir",
        "lexbor_tree_minimal",
    ] {
        let entry = &arms[key];
        assert!(entry["os_before"]["phys_footprint_bytes"].as_u64().unwrap() > 0);
        assert!(entry["os_during"]["phys_footprint_bytes"].as_u64().unwrap() > 0);
        assert!(entry["os_after"]["phys_footprint_bytes"].as_u64().unwrap() > 0);
        assert!(entry["rust_retained_while_alive_bytes"].as_u64().unwrap() > 0);
    }

    let iso_before = arms["lexbor_tree_minimal"]["os_before"]["phys_footprint_bytes"]
        .as_u64()
        .unwrap();
    assert!(
        iso_before <= standalone_before.saturating_add(8 * 1024 * 1024),
        "isolated lexbor_tree_minimal os_before ({iso_before}) must not inherit cross-arm dirty heap above standalone baseline ({standalone_before})"
    );
}

#[test]
fn w1_8_mode_b_pool_emits_thread_cpu_us_and_prevents_cfs_false_quarantines() {
    use athena_cloudbench::framing::encode_raw_warc_frame;

    let tmp_dir = std::env::temp_dir().join(format!("athena-w1-8-pool-{}", std::process::id()));
    let _ = fs::remove_dir_all(&tmp_dir);
    fs::create_dir_all(&tmp_dir).unwrap();

    let frames_path = tmp_dir.join("cfs_and_pill.frames");
    let summary_cpu_path = tmp_dir.join("summary_cpu.json");
    let summary_wall_path = tmp_dir.join("summary_wall.json");

    let mut wire = Vec::new();
    wire.extend_from_slice(
        &encode_raw_warc_frame(
            b"https://en.wikipedia.org/wiki/Cfs_benign_page",
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-Cfs-Sleep-Ms: 75\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><article><h1>Benign Page</h1><p>Fast parse throttled by 75ms cgroups v2 CFS sleep pause.</p></article></main></body></html>",
        )
        .unwrap(),
    );
    wire.extend_from_slice(
        &encode_raw_warc_frame(
            b"https://en.wikipedia.org/wiki/Cpu_poison_pill",
            b"HTTP/1.1 200 OK\r\nX-Athena-Test-Stall-Ms: 75\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
            b"<html><body><main><article><h1>CPU Poison Pill</h1><p>Consumes >75ms of real thread CPU time.</p></article></main></body></html>",
        )
        .unwrap(),
    );
    fs::write(&frames_path, &wire).unwrap();

    let cpu_out = Command::new(env!("CARGO_BIN_EXE_mode_b_pool"))
        .args([
            "--contender",
            "breakthrough",
            "--workers",
            "1",
            "--soft-budget-ms",
            "50",
            "--frames-in",
        ])
        .arg(&frames_path)
        .arg("--summary-out")
        .arg(&summary_cpu_path)
        .output()
        .expect("run mode_b_pool default thread_cpu without explicit --watchdog-ms");
    assert!(
        cpu_out.status.success(),
        "mode_b_pool thread_cpu failed: {}",
        String::from_utf8_lossy(&cpu_out.stderr)
    );
    let cpu_rows: Vec<serde_json::Value> = String::from_utf8_lossy(&cpu_out.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(cpu_rows.len(), 2);
    assert_eq!(cpu_rows[0]["status"], "OK");
    assert_eq!(cpu_rows[0]["soft_budget_exceeded"], false);
    assert_eq!(cpu_rows[0]["budget_clock"], "thread_cpu");
    assert!(cpu_rows[0]["wall_us"].as_u64().unwrap() >= 70_000);
    assert!(cpu_rows[0]["thread_cpu_us"].as_u64().unwrap() < 40_000);

    assert_eq!(cpu_rows[1]["status"], "PAGE_QUARANTINED");
    assert_eq!(
        cpu_rows[1]["quarantine_reason"],
        "SOFT_BUDGET_50MS_EXCEEDED"
    );
    assert_eq!(cpu_rows[1]["soft_budget_exceeded"], true);
    assert!(cpu_rows[1]["thread_cpu_us"].as_u64().unwrap() > 50_000);

    let summary_cpu: serde_json::Value =
        serde_json::from_slice(&fs::read(&summary_cpu_path).unwrap()).unwrap();
    assert_eq!(summary_cpu["budget_clock"], "thread_cpu");
    assert_eq!(summary_cpu["ok_records"], 1);
    assert_eq!(summary_cpu["quarantined_records"], 1);
    assert_eq!(summary_cpu["soft_budget_exceeded_records"], 1);
    assert_eq!(summary_cpu["hard_watchdog_timeouts"], 0);
    assert_eq!(summary_cpu["worker_respawns"], 0);
    assert_eq!(summary_cpu["watchdog_ms"], 500);

    let wall_out = Command::new(env!("CARGO_BIN_EXE_mode_b_pool"))
        .args([
            "--contender",
            "breakthrough",
            "--workers",
            "1",
            "--soft-budget-ms",
            "50",
            "--watchdog-ms",
            "500",
            "--budget-clock",
            "wall",
            "--frames-in",
        ])
        .arg(&frames_path)
        .arg("--summary-out")
        .arg(&summary_wall_path)
        .output()
        .expect("run mode_b_pool wall");
    assert!(
        wall_out.status.success(),
        "mode_b_pool wall failed: {}",
        String::from_utf8_lossy(&wall_out.stderr)
    );
    let wall_rows: Vec<serde_json::Value> = String::from_utf8_lossy(&wall_out.stdout)
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(wall_rows.len(), 2);
    assert_eq!(wall_rows[0]["status"], "PAGE_QUARANTINED");
    assert_eq!(wall_rows[0]["budget_clock"], "wall");
    assert_eq!(wall_rows[1]["status"], "PAGE_QUARANTINED");

    let real_heavy_path = tmp_dir.join("real_heavy.frames");
    let mut heavy_html = String::with_capacity(550_000);
    heavy_html.push_str("<!doctype html><html><body><main>");
    for i in 0..2_500 {
        heavy_html.push_str(&format!(
            "<div class=\"row-{i}\"><h3>Heading {i}</h3><p>Substantive real HTML parser workload item {i} without synthetic headers to verify real thread CPU measurement.</p></div>"
        ));
    }
    heavy_html.push_str("</main></body></html>");
    let heavy_wire = encode_raw_warc_frame(
        b"https://en.wikipedia.org/wiki/Heavy_real_parse",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n",
        heavy_html.as_bytes(),
    )
    .unwrap();
    fs::write(&real_heavy_path, &heavy_wire).unwrap();

    let heavy_out = Command::new(env!("CARGO_BIN_EXE_mode_b_pool"))
        .args([
            "--contender",
            "legacy",
            "--workers",
            "1",
            "--soft-budget-ms",
            "1",
            "--watchdog-ms",
            "10000",
            "--budget-clock",
            "thread_cpu",
            "--frames-in",
        ])
        .arg(&real_heavy_path)
        .output()
        .expect("run mode_b_pool real heavy parse");
    let _ = fs::remove_dir_all(&tmp_dir);
    assert!(heavy_out.status.success());
    let heavy_row: serde_json::Value = serde_json::from_slice(&heavy_out.stdout).unwrap();
    assert_eq!(heavy_row["status"], "PAGE_QUARANTINED");
    assert_eq!(heavy_row["quarantine_reason"], "SOFT_BUDGET_1MS_EXCEEDED");
    assert!(heavy_row["thread_cpu_us"].as_u64().unwrap() > 1_000);
}
