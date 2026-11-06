"""Immutable, fail-closed source contenders for the native research campaign."""
from __future__ import annotations
from copy import deepcopy
from hashlib import sha256
from functools import lru_cache


_DEFAULTS = {"writer": "frame", "hash": "joined", "reserve": "none", "lookup": "hash", "selection": "sort", "prepared": "oneshot", "projection": "separate", "links": "hash", "table": "dense", "html_extractor": "traversal", "lto": "thin", "codegen_units": 8, "cpu": "portable", "digest_library": "default", "html_library": "default", "json_library": "serde", "tls_provider": "aws-lc"}
_STAGES = ["parser", "parse", "projection", "export", "pipeline", "rank", "selection", "highlight", "hash", "block_ids", "serialization", "prepared", "table", "html-table", "budget"]
_SOURCE_SHA256 = {
    "src/aae.rs": "75411fe28008a2592518429a095ff02479e472412ba8a0b506644ca263ded389",
    "src/rank.rs": "10e302ba7044ee1ab4be1f65432c69ea230c4f82efa78475b18ec11b16a92bd2",
    "src/projection.rs": "084d83b1b5250d05573f584a556ee9e81b2e022abd5890e0522ede82f60f3397",
    "src/table.rs": "eb1933a8f29d21f0a8887a6c17cebfc4a2f78b98a73f7a2040139966a561533e",
}


def stage_contracts():
    return {
        "parser": "HTML5 DOM construction/drop; canonical DOM serialization outside timer.",
        "parse": "HTML5 parse, checked arena and projection construction/drop.",
        "projection": "Alias of parse with the same declared complete construction boundary.",
        "export": "Actual public AAE export over an immutable prepared projection; includes hashing/rank/serialization/drop.",
        "pipeline": "Actual HTML5 parse/projection plus AAE export/drop; adoption authority.",
        "rank": "Actual public BM25 including tokenization, lookup, scoring, full ordered result/drop.",
        "selection": "Actual public highlight selection including scoring and novelty/drop.",
        "highlight": "Alias of selection, retained for existing campaign consumers.",
        "hash": "Equivalent joined/incremental/batched content-hash primitive; not total export cost.",
        "block_ids": "Role/text/source-index block hashing; all digests consumed.",
        "serialization": "Equivalent writer primitive over immutable prebuilt typed AAE frames; excludes frame/hash/rank preparation.",
        "prepared": "Complete one-shot or prepared build, 1/2/8/32-query execution, source-key verification and drop lifetime; no export integration claim.",
        "table": "Actual public Row/Cell-to-Grid construction, dense materialization and drop; no HTML extraction claim.",
        "html-table": "Same locked HTML5 parser plus table/row enumeration, explicit attributes, existing public span-aware grid construction and drop; no inferred header semantics or production integration claim.",
        "budget": "Actual oversized public Projection export; only the specific AAE output byte-budget error is expected.",
        "controls": "Independent literal/numeric real-core controls; a correctness command, not a performance stage.",
    }


def _definition(decisions, factors, description, scope="whole_path", stages=None):
    return {"decisions": decisions, "factors": factors, "description": description, "implementation_scope": scope, "stages": stages or _STAGES, "build": _build(factors)}


def _build(factors):
    probe = {}
    for factor, variable in [("writer", "ATHENA_RESEARCH_WRITER"), ("hash", "ATHENA_RESEARCH_HASH"), ("reserve", "ATHENA_RESEARCH_RESERVE"), ("prepared", "ATHENA_RESEARCH_PREPARED"), ("html_extractor", "ATHENA_RESEARCH_TABLE_EXTRACTOR"), ("rank_model", "ATHENA_RESEARCH_RANK_MODEL")]:
        if factor in factors:
            probe[variable] = str(factors[factor])
    profile = {}
    if "lto" in factors:
        profile["CARGO_PROFILE_RELEASE_LTO"] = factors["lto"]
    if "codegen_units" in factors:
        profile["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"] = str(factors["codegen_units"])
    rustflags = ["-C", "target-cpu=native"] if factors.get("cpu") == "native" else []
    provider = factors.get("tls_provider", "aws-lc")
    if provider not in ("aws-lc", "ring"):
        raise ValueError("Unknown TLS provider profile")
    cargo_feature_args = (["--features", "research-probes"] if provider == "aws-lc" else
                          ["--no-default-features", "--features", "research-probes,tls-ring"])
    return {"env": {**profile, **probe}, "cargo_profile_env": profile, "probe_env": probe,
            "rustflags": rustflags, "cargo_feature_args": cargo_feature_args}


@lru_cache(maxsize=1)
def _registry():
    return {
        "baseline": _definition([], {}, "Reviewed source and lockfile unchanged."),
        "aae-direct": _definition(["H01"], {"writer": "direct"}, "Serialize each frame directly into a byte-limited output sink."),
        "aae-buffered": _definition(["H01"], {"writer": "buffered"}, "Persistent 8KiB serde writer buffer, flushed at each frame boundary into a byte-limited sink."),
        "aae-reuse": _definition(["H01"], {"writer": "reuse"}, "Reuse a byte-limited frame buffer across frames; copy complete frames into the output."),
        "aae-incremental": _definition(["H01"], {"hash": "incremental"}, "Hash chunk text and separator bytes without joining."),
        "aae-batched4096": _definition(["H01"], {"hash": "batched4096"}, "Join small hash updates in a reusable bounded 4096-byte scratch buffer."),
        "aae-batched16384": _definition(["H01"], {"hash": "batched16384"}, "Join small hash updates in a reusable bounded 16384-byte scratch buffer."),
        "aae-smallreserve": _definition(["H01"], {"reserve": "small"}, "Reserve at most 4096 output bytes initially without scanning chunks."),
        "aae-estimatedreserve": _definition(["H01"], {"reserve": "estimate"}, "Estimate output capacity from the already-built projection and cap the reservation at the output limit."),
        "rank-binary": _definition(["H02"], {"lookup": "binary"}, "Binary-search sorted distinct query terms; lowercase each document token once."),
        "rank-linear": _definition(["H02"], {"lookup": "linear"}, "Linear-search sorted distinct query terms; lowercase each document token once."),
        "rank-select": _definition(["H02"], {"selection": "heap"}, "Exact heap selection, retaining the entire score population for novelty recovery."),
        "prepared": _definition(["H03"], {"prepared": "lexical"}, "Owned bounded lexical state built, queried, invalidated and dropped within the measured lifetime.", "prepared_stage_only", ["prepared"]),
        "projection-fused": _definition(["H04", "H05"], {"projection": "fused"}, "Fuse chunk-owner assignment/text/link collection with checked arena construction after the same HTML5 parser."),
        "links-btree": _definition(["H05"], {"links": "btree"}, "Use ordered tree lookup for resolved unique links while preserving insertion-order IDs."),
        "table-sparse": _definition(["H06"], {"table": "sparse"}, "Sparse row occupancy maps followed by exact public dense-grid materialization.", "logical_grid_only", ["table"]),
        "table-html-selectors": _definition(["H06"], {"html_extractor": "selectors"}, "Precompiled CSS table/row selection over the same Scraper HTML5 DOM, with nearest-table ownership and the existing public span-aware grid.", "html_table_stage_only", ["html-table"]),
        "build-lto-off": _definition(["H10"], {"lto": "off"}, "Keep source/lock fixed and disable release LTO.", "build_factor"),
        "build-lto-full": _definition(["H10"], {"lto": "fat"}, "Keep source/lock fixed and enable full release LTO.", "build_factor"),
        "build-codegen1": _definition(["H10"], {"codegen_units": 1}, "Keep source/lock fixed and compile release with one codegen unit.", "build_factor"),
        "build-native": _definition(["H10"], {"cpu": "native"}, "Compile for the measurement host CPU; deployment portability remains unverified.", "build_factor"),
        "lib-blake3-pure": _definition(["H01"], {"digest_library": "pure"}, "Locked BLAKE3 1.8.7 pure Rust backend; same digest identity.", "dependency_factor"),
        "lib-blake3-intrinsics": _definition(["H01"], {"digest_library": "intrinsics"}, "Locked BLAKE3 1.8.7 prefer_intrinsics backend; runtime CPU detection retained.", "dependency_factor"),
        "lib-scraper-lean": _definition(["H04", "H05"], {"html_library": "lean"}, "Locked Scraper 0.27.0 without CLI dependencies; HTML5 parse warnings retained.", "dependency_factor"),
        "lib-scraper-atomic": _definition(["H04", "H05"], {"html_library": "atomic"}, "Locked Scraper 0.27.0 atomic tendril reference counting; default error reporting retained.", "dependency_factor"),
        "lib-scraper-modern": _definition(["H04", "H05", "H09"], {"html_library": "modern"}, "Pinned candidate Scraper 0.29.0 + html5ever 0.40.1 with default error reporting; candidate only, no adoption claim.", "dependency_factor"),
        "html-attrs-hash": _definition(["H04"], {"html_attr_lookup": "hash"}, "Eager HashSet duplicate-attribute membership index in the locked html5ever tokenizer; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-hybrid8": _definition(["H04"], {"html_attr_lookup": "hybrid8"}, "Promote the ordered attribute Vec to a HashSet at 8 accepted attributes; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-hybrid32": _definition(["H04"], {"html_attr_lookup": "hybrid32"}, "Promote the ordered attribute Vec to a HashSet at 32 accepted attributes; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-hybrid128": _definition(["H04"], {"html_attr_lookup": "hybrid128"}, "Promote the ordered attribute Vec to a HashSet at 128 accepted attributes; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-modern-vector": _definition(["H04"], {"html_library": "modern", "html_attr_lookup": "vector"}, "Stage-scoped H04 vector baseline on Scraper 0.29.0 + html5ever 0.40.1; same parser-stage cases as the indexed candidates.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-modern-hash": _definition(["H04"], {"html_library": "modern", "html_attr_lookup": "hash"}, "Eager HashSet duplicate-attribute membership index on Scraper 0.29.0 + html5ever 0.40.1; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-modern-hybrid8": _definition(["H04"], {"html_library": "modern", "html_attr_lookup": "hybrid8"}, "HashSet promotion at 8 accepted attributes on Scraper 0.29.0 + html5ever 0.40.1; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-modern-hybrid32": _definition(["H04"], {"html_library": "modern", "html_attr_lookup": "hybrid32"}, "HashSet promotion at 32 accepted attributes on Scraper 0.29.0 + html5ever 0.40.1; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "html-attrs-modern-hybrid128": _definition(["H04"], {"html_library": "modern", "html_attr_lookup": "hybrid128"}, "HashSet promotion at 128 accepted attributes on Scraper 0.29.0 + html5ever 0.40.1; first attribute and warning semantics retained.", "html_attr_membership_stage_only", ["parser"]),
        "lib-json-sonic": _definition(["H01"], {"json_library": "sonic"}, "Sonic 0.5.10 framed typed AAE serializer; serde_json input parsing unchanged. No bounded Sonic streaming adapter claim.", "dependency_factor"),
        "lib-rustls-ring": _definition(["H07"], {"tls_provider": "ring"}, "Locked Reqwest/Rustls ring provider profile; service-stage comparator only, with no provider or performance adoption claim.", "service_provider_stage_only", ["pipeline"]),
    }


def definitions():
    """Return a defensive copy; internal hot paths use the cached registry directly."""
    return deepcopy(_registry())


def _resolve(variant_id):
    registry = _registry()
    ids = variant_id.split("+")
    if not ids or any(name not in registry for name in ids) or len(ids) != len(set(ids)):
        raise ValueError("Unknown, empty or duplicate contender identity")
    factors = {}
    for name in ids:
        for key, value in registry[name]["factors"].items():
            if key in factors and factors[key] != value:
                raise ValueError(f"Incompatible values for factor {key}")
            factors[key] = value
    if factors.get("json_library") == "sonic" and factors.get("writer", "frame") != "frame":
        raise ValueError("Sonic framed adapter cannot implement the bounded streaming writer factors")
    if "html_table_stage_only" in {registry[name]["implementation_scope"] for name in ids} and len(ids) > 1:
        raise ValueError("HTML table extraction comparator is stage-only and cannot be composed into the production-path finalist")
    return ids, factors


def compatible(a, b):
    try:
        _resolve(a + "+" + b)
        return True
    except ValueError:
        return False


def canonical_id(variant_id):
    ids, _ = _resolve(variant_id)
    selected = set(ids) - {"baseline"}
    return "+".join(name for name in _registry() if name in selected) or "baseline"


def metadata(variant_id):
    ids, factors = _resolve(variant_id)
    registry = _registry()
    return {"id": variant_id, "components": ids, "decisions": sorted({decision for name in ids for decision in registry[name]["decisions"]}), "factors": {**_DEFAULTS, **factors}, "build": _build({**_DEFAULTS, **factors}), "implementation_scopes": sorted({registry[name]["implementation_scope"] for name in ids})}


def _replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Frozen source does not match the reviewed transformation")
    return text.replace(old, new, 1)


def _region(text, start, end, replacement):
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("Frozen source region is absent or ambiguous")
    left, right = text.index(start), text.index(end)
    if right <= left:
        raise ValueError("Frozen source region order differs")
    return text[:left] + replacement + text[right:]


_SINK = r'''struct BoundedSink { out: Vec<u8>, limit: usize }
impl io::Write for BoundedSink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.out.len()) {
            return Err(io::Error::other("AAE output byte budget exceeded"));
        }
        self.out.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> { Ok(()) }
}
'''


def _writer(text, kind):
    if kind == "direct":
        text = _replace(text, '''    let mut out = BufWriter::with_capacity(
        8192,
        Output {
            bytes: Vec::new(),
            limit: max_output_bytes,
        },
    );''', '''    let mut out = Output {
        bytes: Vec::new(),
        limit: max_output_bytes,
    };''')
        return _replace(text, '''    let out = out
        .into_inner()
        .map_err(|error| serde_json::Error::io(error.into_error()))?;
    String::from_utf8(out.bytes).map_err(|error| serde_json::Error::io(io::Error::other(error)))''',
            '''    String::from_utf8(out.bytes).map_err(|error| serde_json::Error::io(io::Error::other(error)))''')
    if kind == "buffered":
        return _replace(text, '''    out.write_all(b"\\n").map_err(serde_json::Error::io)?;
    Ok(())''', '''    out.write_all(b"\\n").map_err(serde_json::Error::io)?;
    out.flush().map_err(serde_json::Error::io)?;
    Ok(())''')
    if kind == "reuse":
        reusable = '''struct ReusableOutput {
    bytes: Vec<u8>,
    frame: Vec<u8>,
    limit: usize,
}
impl Write for ReusableOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let committed = self.bytes.len().saturating_add(self.frame.len());
        if committed > self.limit || bytes.len() > self.limit - committed {
            return Err(io::Error::other("AAE output byte budget exceeded"));
        }
        self.frame.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        self.bytes.extend_from_slice(&self.frame);
        self.frame.clear();
        Ok(())
    }
}
'''
        text = _region(text, "struct Output {", "fn json_line(frame: &Frame<'_>, out: &mut impl Write)", "")
        text = _replace(text, "fn json_line(frame: &Frame<'_>, out: &mut impl Write)", reusable + "fn json_line(frame: &Frame<'_>, out: &mut impl Write)")
        text = _replace(text, '''    out.write_all(b"\\n").map_err(serde_json::Error::io)?;
    Ok(())''', '''    out.write_all(b"\\n").map_err(serde_json::Error::io)?;
    out.flush().map_err(serde_json::Error::io)?;
    Ok(())''')
        text = _replace(text, '''    let mut out = BufWriter::with_capacity(
        8192,
        Output {
            bytes: Vec::new(),
            limit: max_output_bytes,
        },
    );''', '''    let mut out = ReusableOutput {
        bytes: Vec::new(),
        frame: Vec::new(),
        limit: max_output_bytes,
    };''')
        return _replace(text, '''    let out = out
        .into_inner()
        .map_err(|error| serde_json::Error::io(error.into_error()))?;
    String::from_utf8(out.bytes).map_err(|error| serde_json::Error::io(io::Error::other(error)))''',
            '''    String::from_utf8(out.bytes).map_err(|error| serde_json::Error::io(io::Error::other(error)))''')
    raise ValueError("Unknown writer")


def _hash(kind):
    batch = {"incremental": 0, "batched4096": 4096, "batched16384": 16384}[kind]
    if batch == 0:
        return r'''    let mut hash = blake3::Hasher::new();
    for (i, chunk) in page.chunks.iter().enumerate() {
        if i != 0 { hash.update(b"\n"); }
        hash.update(chunk.text.as_bytes());
    }
    let digest = hash.finalize().to_hex().to_string();
'''
    return r'''    let digest = match page.chunks.as_slice() {
        [] => blake3::hash(b""),
        [only] => blake3::hash(only.text.as_bytes()),
        [first, rest @ ..] => {
            let total_len = page
                .chunks
                .iter()
                .fold(page.chunks.len() - 1, |acc, c| acc + c.text.len());
            if total_len <= 4096 {
                let mut stack = [0u8; 4096];
                let first_bytes = first.text.as_bytes();
                stack[..first_bytes.len()].copy_from_slice(first_bytes);
                let mut pos = first_bytes.len();
                for chunk in rest {
                    stack[pos] = b'\n';
                    pos += 1;
                    let bytes = chunk.text.as_bytes();
                    stack[pos..pos + bytes.len()].copy_from_slice(bytes);
                    pos += bytes.len();
                }
                blake3::hash(&stack[..pos])
            } else if total_len <= 32_768 {
                let mut stack = [0u8; 32_768];
                let first_bytes = first.text.as_bytes();
                stack[..first_bytes.len()].copy_from_slice(first_bytes);
                let mut pos = first_bytes.len();
                for chunk in rest {
                    stack[pos] = b'\n';
                    pos += 1;
                    let bytes = chunk.text.as_bytes();
                    stack[pos..pos + bytes.len()].copy_from_slice(bytes);
                    pos += bytes.len();
                }
                blake3::hash(&stack[..pos])
            } else if total_len <= 262_144 {
                let mut scratch = Vec::with_capacity(total_len);
                scratch.extend_from_slice(first.text.as_bytes());
                for chunk in rest {
                    scratch.push(b'\n');
                    scratch.extend_from_slice(chunk.text.as_bytes());
                }
                blake3::hash(&scratch)
            } else {
                let mut hash = blake3::Hasher::new();
                let mut scratch = Vec::with_capacity(BATCH);
                let first_bytes = first.text.as_bytes();
                let first_full = (first_bytes.len() / BATCH) * BATCH;
                if first_full > 0 {
                    hash.update(&first_bytes[..first_full]);
                }
                scratch.extend_from_slice(&first_bytes[first_full..]);
                for chunk in rest {
                    let bytes = chunk.text.as_bytes();
                    if scratch.len() + 1 + bytes.len() <= BATCH {
                        scratch.push(b'\n');
                        scratch.extend_from_slice(bytes);
                        if scratch.len() == BATCH {
                            hash.update(&scratch);
                            scratch.clear();
                        }
                        continue;
                    }
                    scratch.push(b'\n');
                    let space = BATCH - scratch.len();
                    let (head, tail) = bytes.split_at(space);
                    scratch.extend_from_slice(head);
                    hash.update(&scratch);
                    scratch.clear();
                    let full = (tail.len() / BATCH) * BATCH;
                    if full > 0 {
                        hash.update(&tail[..full]);
                    }
                    scratch.extend_from_slice(&tail[full..]);
                }
                if !scratch.is_empty() {
                    hash.update(&scratch);
                }
                hash.finalize()
            }
        }
    }
    .to_hex()
    .to_string();
'''.replace("BATCH", f"{batch}usize")


def _hash_source(text, kind):
    start = "    let mut content = blake3::Hasher::new();\n"
    end = "    let digest = content.finalize().to_hex().to_string();"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("Frozen AAE content-hash region is absent or ambiguous")
    left = text.index(start)
    right = text.index(end, left) + len(end)
    return text[:left] + _hash(kind) + text[right:]


def _sonic_serializer(text):
    return _replace(text, '''    serde_json::to_writer(&mut *out, frame)?;
    out.write_all(b"\\n").map_err(serde_json::Error::io)?;''', '''    let bytes = sonic_rs::to_vec(frame)
        .map_err(<serde_json::Error as serde::ser::Error>::custom)?;
    out.write_all(&bytes).map_err(serde_json::Error::io)?;
    out.write_all(b"\\n").map_err(serde_json::Error::io)?;''')


def _reserve_source(text, kind):
    if kind == "small":
        reserve = "let reserve = 4096usize;"
    elif kind == "estimate":
        reserve = '''let reserve = page.chunks.iter().fold(512usize, |total, chunk| {
        total.saturating_add(chunk.text.len()).saturating_add(160)
    }).saturating_add(page.links.len().saturating_mul(64));'''
    else:
        raise ValueError("Unknown output reservation")
    text = _replace(text, "    let mut out = ", "    " + reserve + "\n    let mut out = ")
    return _replace(text, "bytes: Vec::new(),", "bytes: Vec::with_capacity(reserve.min(max_output_bytes)),")


_HEAP_SELECTION = r'''pub fn highlight_indices(chunks: &[Chunk], query: &str, limit: usize) -> Vec<usize> {
    if limit == 0 {
        return Vec::new();
    }
    let mut scores = score_chunks(chunks, query, 1.2, 0.75);
    if limit == 1 {
        return scores
            .into_iter()
            .filter(|(_, score)| *score > 0.0)
            .max_by(|(a, sa), (b, sb)| sa.total_cmp(sb).then_with(|| b.cmp(a)))
            .map(|(index, _)| vec![index])
            .unwrap_or_default();
    }
    scores.retain(|(_, score)| *score > 0.0);
    if scores.is_empty() {
        return Vec::new();
    }
    let cmp =
        |(a, sa): &(usize, f64), (b, sb): &(usize, f64)| sb.total_cmp(sa).then_with(|| a.cmp(b));
    let cap = limit.min(scores.len());
    if scores.len() > cap {
        scores.select_nth_unstable_by(cap - 1, cmp);
    }
    scores[..cap].sort_unstable_by(cmp);
    let mut selected = Vec::with_capacity(cap);
    let mut seen: Option<HashSet<Cow<'_, str>>> = None;
    let mut try_select = |index: usize, selected: &mut Vec<usize>| -> bool {
        let text = chunks[index].text.as_str();
        if seen.is_none() && selected.len() < 16 && text.is_ascii() {
            if selected
                .iter()
                .all(|&prev: &usize| !chunks[prev].text.eq_ignore_ascii_case(text))
            {
                selected.push(index);
            }
            return selected.len() == limit;
        }
        let set = seen.get_or_insert_with(|| {
            let mut s = HashSet::with_capacity(limit.saturating_mul(2));
            for &prev in selected.iter() {
                s.insert(lowercase(&chunks[prev].text));
            }
            s
        });
        if set.insert(lowercase(text)) {
            selected.push(index);
        }
        selected.len() == limit
    };
    for &(index, _) in &scores[..cap] {
        if try_select(index, &mut selected) {
            return selected;
        }
    }
    let mut start = cap;
    let mut window = (limit * 6).max(32);
    while selected.len() < limit && start < scores.len() {
        let rem = scores.len() - start;
        let take = window.min(rem);
        if rem > take {
            scores[start..].select_nth_unstable_by(take - 1, cmp);
        }
        let end = start + take;
        scores[start..end].sort_unstable_by(cmp);
        for &(index, _) in &scores[start..end] {
            if try_select(index, &mut selected) {
                return selected;
            }
        }
        start = end;
        window = window.saturating_mul(2);
    }
    selected
}

'''


_SPARSE_TABLE = r'''pub fn expand(rows: &[Row], max_rows: usize, max_cols: usize) -> Result<Grid, GridError> {
    expand_with_slot_limit(rows, max_rows, max_cols, max_rows.saturating_mul(max_cols))
}

pub fn expand_with_slot_limit(
    rows: &[Row],
    max_rows: usize,
    max_cols: usize,
    max_slots: usize,
) -> Result<Grid, GridError> {
    if rows.len() > max_rows || max_cols == 0 { return Err(GridError::TooLarge); }
    if rows.windows(2).any(|pair| pair[0].group > pair[1].group) { return Err(GridError::InvalidGroupOrder); }
    let mut occupied = vec![std::collections::BTreeMap::<usize, usize>::new(); rows.len()];
    let mut row_widths = vec![0usize; rows.len()];
    let mut cells = Vec::new();
    let mut width = 0;
    let mut group_end = 0;
    let mut initialized_slots = 0usize;
    for (r, row) in rows.iter().enumerate() {
        if r == group_end { group_end = (r + 1..rows.len()).find(|&i| rows[i].group != row.group).unwrap_or(rows.len()); }
        let mut c = 0usize;
        for cell in &row.cells {
            while occupied[r].contains_key(&c) { c += 1; }
            let dc = cell.colspan.clamp(1, 1000);
            let dr = if cell.rowspan == 0 { group_end - r } else { cell.rowspan.min(65_534).min(group_end - r) };
            let end_col = c.checked_add(dc).ok_or(GridError::TooLarge)?;
            if end_col > max_cols { return Err(GridError::TooLarge); }
            let index = cells.len();
            for (line, row_width) in occupied.iter_mut().zip(row_widths.iter_mut()).take(r + dr).skip(r) {
                if *row_width < end_col {
                    initialized_slots = initialized_slots
                        .checked_add(end_col - *row_width)
                        .ok_or(GridError::TooManySlots)?;
                    if initialized_slots > max_slots {
                        return Err(GridError::TooManySlots);
                    }
                    *row_width = end_col;
                }
                for col in c..end_col {
                    if line.insert(col, index).is_some() { return Err(GridError::Overlap); }
                }
            }
            cells.push(cell.clone());
            width = width.max(end_col);
            c = end_col;
        }
    }
    if rows.len().checked_mul(width).is_none_or(|slots| slots > max_slots) {
        return Err(GridError::TooManySlots);
    }
    let slots = occupied.into_iter().map(|line| {
        let mut dense = vec![None; width];
        for (col, cell) in line { dense[col] = Some(cell); }
        dense
    }).collect();
    Ok(Grid { cells, slots, width })
}
'''


def _transform_projection(text, *, fuse, btree):
    if btree:
        text = _replace(
            text,
            "use std::collections::HashMap;",
            "use std::collections::{BTreeMap, HashMap};",
        )
    map_type = "BTreeMap<u64, u32>" if btree else "HashMap<u64, u32>"
    map_init = "BTreeMap::new()" if btree else "HashMap::new()"
    helpers = r'''fn hash_bytes(bytes: &[u8]) -> u64 {
    let mut h = 0xcbf29ce484222325u64 ^ (bytes.len() as u64);
    for &b in bytes {
        h = (h ^ (b as u64)).wrapping_mul(0x100000001b3);
    }
    h ^ (h >> 32)
}

fn hash_pair(prefix: &[u8], suffix: &[u8]) -> u64 {
    let mut h = 0xcbf29ce484222325u64 ^ ((prefix.len() + suffix.len()) as u64);
    for &b in prefix {
        h = (h ^ (b as u64)).wrapping_mul(0x100000001b3);
    }
    for &b in suffix {
        h = (h ^ (b as u64)).wrapping_mul(0x100000001b3);
    }
    h ^ (h >> 32)
}

fn is_simple_root_path(href: &str) -> bool {
    let bytes = href.as_bytes();
    if bytes.is_empty() || bytes[0] != b'/' || (bytes.len() > 1 && bytes[1] == b'/') {
        return false;
    }
    let mut seen_hash = false;
    for &b in &bytes[1..] {
        if b == b'#' {
            if seen_hash {
                return false;
            }
            seen_hash = true;
        } else if !(b.is_ascii_alphanumeric() || matches!(b, b'/' | b'-' | b'_')) {
            return false;
        }
    }
    true
}

fn extract_base_origin(base: &Url) -> Option<&str> {
    if base.scheme() == "https"
        && base.username().is_empty()
        && base.password().is_none()
        && base.host_str().is_some()
    {
        let s = base.as_str();
        let rest = &s[8..];
        let end = 8 + rest.find(['/', '?', '#']).unwrap_or(rest.len());
        Some(&s[..end])
    } else {
        None
    }
}

struct Draft {
    parent: NodeId,
    start_or_link: u32,
    len: u32,
    tag: Tag,
}
'''
    if fuse:
        builder = f'''struct ProjectionBuilder<'a> {{
    base: &'a Url,
    base_origin: Option<Option<&'a str>>,
    limits: ParseLimits,
    node_cap: usize,
    text_cap: usize,
    chunk_count: usize,
    text: Vec<u8>,
    drafts: Vec<Draft>,
    link_ids: {map_type},
    links: Vec<String>,
    link_collisions: Vec<(String, u32)>,
    raw_hrefs: Vec<u8>,
    raw_cache: Vec<(u64, u32, u32, u32)>,
    link_bytes: usize,
}}

impl<'a> ProjectionBuilder<'a> {{
    fn with_capacity(
        base: &'a Url,
        limits: ParseLimits,
        html_len: usize,
        node_hint: usize,
    ) -> Self {{
        let max_drafts = limits.max_nodes.saturating_sub(1);
        let node_cap = if node_hint > 0 {{
            node_hint.min(max_drafts)
        }} else if html_len < 128 {{
            8.min(max_drafts)
        }} else {{
            (html_len / 24).clamp(16, 16_384).min(max_drafts)
        }};
        let text_cap = if html_len < 128 {{
            html_len.min(limits.max_text_bytes)
        }} else {{
            (html_len - node_cap.saturating_mul(3).min(html_len / 2))
                .clamp(64, 65_536)
                .min(limits.max_text_bytes)
        }};
        Self {{
            base,
            base_origin: None,
            limits,
            node_cap,
            text_cap,
            chunk_count: 0,
            text: Vec::new(),
            drafts: Vec::new(),
            link_ids: {map_init},
            links: Vec::new(),
            link_collisions: Vec::new(),
            raw_hrefs: Vec::new(),
            raw_cache: Vec::new(),
            link_bytes: 0,
        }}
    }}

    fn cache_raw_href(&mut self, raw_h: u64, href: &str, link_idx: u32) {{
        if href.len() <= 256 {{
            if self.raw_cache.is_empty() {{
                self.raw_cache.resize(64, (0, 0, u32::MAX, NONE));
                self.raw_hrefs.reserve(512);
            }} else if self.raw_hrefs.len() + href.len() > 4096 {{
                self.raw_hrefs.clear();
                self.raw_cache.fill((0, 0, u32::MAX, NONE));
            }}
            let off = self.raw_hrefs.len() as u32;
            let len = href.len() as u32;
            self.raw_hrefs.extend_from_slice(href.as_bytes());
            self.raw_cache[(raw_h as usize) & 63] = (raw_h, off, len, link_idx);
        }}
    }}

    fn resolve_link(&mut self, href: &str) -> Result<u32, &'static str> {{
        let raw_h = hash_bytes(href.as_bytes());
        if !self.raw_cache.is_empty() {{
            let (cached_h, off, len, cached_id) = self.raw_cache[(raw_h as usize) & 63];
            if cached_h == raw_h
                && len as usize == href.len()
                && self
                    .raw_hrefs
                    .get(off as usize..(off.saturating_add(len)) as usize)
                    == Some(href.as_bytes())
            {{
                return Ok(cached_id);
            }}
        }}
        let origin = *self
            .base_origin
            .get_or_insert_with(|| extract_base_origin(self.base));
        if let Some(origin) = origin
            && is_simple_root_path(href)
        {{
            let total_len = origin.len() + href.len();
            let url_h = hash_pair(origin.as_bytes(), href.as_bytes());
            if let Some(&id) = self.link_ids.get(&url_h) {{
                let existing = &self.links[id as usize];
                if existing.len() == total_len
                    && existing.starts_with(origin)
                    && &existing[origin.len()..] == href
                {{
                    self.cache_raw_href(raw_h, href, id);
                    return Ok(id);
                }}
                if let Some(&cid) = self.link_collisions.iter().find_map(|(u, idx)| {{
                    (u.len() == total_len && u.starts_with(origin) && &u[origin.len()..] == href)
                        .then_some(idx)
                }}) {{
                    self.cache_raw_href(raw_h, href, cid);
                    return Ok(cid);
                }}
            }}
            if total_len > self.limits.max_link_url_bytes
                || total_len > self.limits.max_link_bytes.saturating_sub(self.link_bytes)
            {{
                return Err("resolved link byte budget exceeded");
            }}
            self.link_bytes += total_len;
            let id = u32::try_from(self.links.len()).map_err(|_| "link ID overflow")?;
            let mut resolved = String::with_capacity(total_len);
            resolved.push_str(origin);
            resolved.push_str(href);
            if *self.link_ids.entry(url_h).or_insert(id) != id {{
                self.link_collisions.push((resolved.clone(), id));
            }}
            self.links.push(resolved);
            self.cache_raw_href(raw_h, href, id);
            return Ok(id);
        }}
        let mut link_idx = NONE;
        if let Ok(target) = self.base.join(href)
            && target.scheme() == "https"
            && target.username().is_empty()
            && target.password().is_none()
        {{
            let target_str = target.as_str();
            let url_h = hash_bytes(target_str.as_bytes());
            if let Some(&id) = self.link_ids.get(&url_h) {{
                if self.links[id as usize] == target_str {{
                    link_idx = id;
                }} else if let Some(&cid) = self
                    .link_collisions
                    .iter()
                    .find_map(|(u, idx)| (u == target_str).then_some(idx))
                {{
                    link_idx = cid;
                }}
            }}
            if link_idx == NONE {{
                if target_str.len() > self.limits.max_link_url_bytes
                    || target_str.len() > self.limits.max_link_bytes.saturating_sub(self.link_bytes)
                {{
                    return Err("resolved link byte budget exceeded");
                }}
                self.link_bytes += target_str.len();
                link_idx = u32::try_from(self.links.len()).map_err(|_| "link ID overflow")?;
                let owned: String = target.into();
                if *self.link_ids.entry(url_h).or_insert(link_idx) != link_idx {{
                    self.link_collisions.push((owned.clone(), link_idx));
                }}
                self.links.push(owned);
            }}
        }}
        self.cache_raw_href(raw_h, href, link_idx);
        Ok(link_idx)
    }}

    fn add_element(
        &mut self,
        parent: NodeId,
        name: &str,
        href: Option<&str>,
    ) -> Result<NodeId, &'static str> {{
        if self.drafts.len() >= self.limits.max_nodes - 1 {{
            return Err("max DOM nodes exceeded");
        }}
        if self.drafts.is_empty() && self.node_cap > 0 {{
            self.drafts.reserve(self.node_cap);
        }}
        let t = tag(name);
        let link_idx = if self.limits.include_links
            && t == Tag::A
            && let Some(href) = href
        {{
            self.resolve_link(href)?
        }} else {{
            NONE
        }};
        if role(t).is_some() {{
            self.chunk_count += 1;
        }}
        let id = u32::try_from(self.drafts.len() + 1).map_err(|_| "node ID overflow")?;
        self.drafts.push(Draft {{
            parent,
            start_or_link: link_idx,
            len: 0,
            tag: t,
        }});
        Ok(id)
    }}

    fn add_text(&mut self, parent: NodeId, value: &str) -> Result<(), &'static str> {{
        if value.is_empty() {{
            return Ok(());
        }}
        if self.drafts.len() >= self.limits.max_nodes - 1 {{
            return Err("max DOM nodes exceeded");
        }}
        if value.len() > self.limits.max_text_bytes.saturating_sub(self.text.len()) {{
            return Err("decoded text byte budget exceeded");
        }}
        if self.drafts.is_empty() && self.node_cap > 0 {{
            self.drafts.reserve(self.node_cap);
        }}
        if self.text.is_empty() && self.text_cap > 0 {{
            self.text.reserve(self.text_cap.max(value.len()));
        }}
        let start = self.text.len() as u32;
        self.text.extend_from_slice(value.as_bytes());
        self.drafts.push(Draft {{
            parent,
            start_or_link: start,
            len: value.len() as u32,
            tag: Tag::Text,
        }});
        Ok(())
    }}

    fn finish(self, parse_warnings: usize) -> Result<Projection, &'static str> {{
        let Self {{
            limits,
            chunk_count,
            text,
            drafts,
            link_ids,
            mut links,
            link_collisions,
            raw_hrefs,
            raw_cache,
            ..
        }} = self;
        drop(link_ids);
        drop(link_collisions);
        drop(raw_hrefs);
        drop(raw_cache);
        let mut chunk_owner = if chunk_count > 0 {{
            vec![NONE; drafts.len() + 1]
        }} else {{
            Vec::new()
        }};
        let mut chunks = Vec::<Chunk>::with_capacity(chunk_count);
        let mut has_many_links = false;
        let mut dom = DomArena::new(Bytes::from(text), drafts.len() + 1);
        for (i, draft) in drafts.into_iter().enumerate() {{
            let range = if draft.tag == Tag::Text {{
                Some((draft.start_or_link as usize, draft.len as usize))
            }} else {{
                None
            }};
            let idx = dom
                .append_child(draft.parent, draft.tag, range)
                .map_err(|_| "DOM arena capacity/depth/range error")?;
            debug_assert_eq!(idx as usize, i + 1);
            if draft.tag != Tag::Text && draft.start_or_link != NONE {{
                dom.set_attribute_id(idx, draft.start_or_link)
                    .map_err(|_| "invalid link index")?;
            }}
            if chunk_count > 0 {{
                let inherited = chunk_owner[draft.parent as usize];
                let owner = if let Some(kind) = role(draft.tag) {{
                    let index = chunks.len() as u32;
                    chunks.push(Chunk {{
                        node: idx,
                        role: kind,
                        text: String::new(),
                        links: Vec::new(),
                    }});
                    index
                }} else {{
                    inherited
                }};
                chunk_owner[idx as usize] = owner;
                if owner != NONE {{
                    if draft.tag == Tag::Text
                        && let Ok(segment) = dom.raw_text(idx)
                    {{
                        chunks[owner as usize].text.push_str(segment);
                    }}
                    if draft.tag == Tag::A && draft.start_or_link != NONE {{
                        let clinks = &mut chunks[owner as usize].links;
                        let link_id = draft.start_or_link;
                        if clinks.len() < 8 {{
                            if !clinks.contains(&link_id) {{
                                clinks.push(link_id);
                            }}
                        }} else if clinks.last() != Some(&link_id) {{
                            clinks.push(link_id);
                            has_many_links = true;
                        }}
                    }}
                }}
            }}
        }}
        drop(chunk_owner);
        let chunks = build_chunks_fused(
            chunks,
            limits.max_words_per_chunk,
            links.len(),
            has_many_links,
        );
        let mut chunks = chunks;
        apply_boilerplate_policy(&dom, &mut chunks, &mut links, limits.boilerplate);
        Ok(Projection {{
            dom,
            chunks,
            links,
            parse_warnings,
        }})
    }}
}}

'''
    else:
        builder = f'''struct ProjectionBuilder<'a> {{
    base: &'a Url,
    base_origin: Option<Option<&'a str>>,
    limits: ParseLimits,
    node_cap: usize,
    text_cap: usize,
    text: Vec<u8>,
    drafts: Vec<Draft>,
    link_ids: {map_type},
    links: Vec<String>,
    link_collisions: Vec<(String, u32)>,
    raw_hrefs: Vec<u8>,
    raw_cache: Vec<(u64, u32, u32, u32)>,
    link_bytes: usize,
}}

impl<'a> ProjectionBuilder<'a> {{
    fn with_capacity(
        base: &'a Url,
        limits: ParseLimits,
        html_len: usize,
        node_hint: usize,
    ) -> Self {{
        let max_drafts = limits.max_nodes.saturating_sub(1);
        let node_cap = if node_hint > 0 {{
            node_hint.min(max_drafts)
        }} else if html_len < 128 {{
            8.min(max_drafts)
        }} else {{
            (html_len / 24).clamp(16, 16_384).min(max_drafts)
        }};
        let text_cap = if html_len < 128 {{
            html_len.min(limits.max_text_bytes)
        }} else {{
            (html_len - node_cap.saturating_mul(3).min(html_len / 2))
                .clamp(64, 65_536)
                .min(limits.max_text_bytes)
        }};
        Self {{
            base,
            base_origin: None,
            limits,
            node_cap,
            text_cap,
            text: Vec::new(),
            drafts: Vec::new(),
            link_ids: {map_init},
            links: Vec::new(),
            link_collisions: Vec::new(),
            raw_hrefs: Vec::new(),
            raw_cache: Vec::new(),
            link_bytes: 0,
        }}
    }}

    fn cache_raw_href(&mut self, raw_h: u64, href: &str, link_idx: u32) {{
        if href.len() <= 256 {{
            if self.raw_cache.is_empty() {{
                self.raw_cache.resize(64, (0, 0, u32::MAX, NONE));
                self.raw_hrefs.reserve(512);
            }} else if self.raw_hrefs.len() + href.len() > 4096 {{
                self.raw_hrefs.clear();
                self.raw_cache.fill((0, 0, u32::MAX, NONE));
            }}
            let off = self.raw_hrefs.len() as u32;
            let len = href.len() as u32;
            self.raw_hrefs.extend_from_slice(href.as_bytes());
            self.raw_cache[(raw_h as usize) & 63] = (raw_h, off, len, link_idx);
        }}
    }}

    fn resolve_link(&mut self, href: &str) -> Result<u32, &'static str> {{
        let raw_h = hash_bytes(href.as_bytes());
        if !self.raw_cache.is_empty() {{
            let (cached_h, off, len, cached_id) = self.raw_cache[(raw_h as usize) & 63];
            if cached_h == raw_h
                && len as usize == href.len()
                && self
                    .raw_hrefs
                    .get(off as usize..(off.saturating_add(len)) as usize)
                    == Some(href.as_bytes())
            {{
                return Ok(cached_id);
            }}
        }}
        let origin = *self
            .base_origin
            .get_or_insert_with(|| extract_base_origin(self.base));
        if let Some(origin) = origin
            && is_simple_root_path(href)
        {{
            let total_len = origin.len() + href.len();
            let url_h = hash_pair(origin.as_bytes(), href.as_bytes());
            if let Some(&id) = self.link_ids.get(&url_h) {{
                let existing = &self.links[id as usize];
                if existing.len() == total_len
                    && existing.starts_with(origin)
                    && &existing[origin.len()..] == href
                {{
                    self.cache_raw_href(raw_h, href, id);
                    return Ok(id);
                }}
                if let Some(&cid) = self.link_collisions.iter().find_map(|(u, idx)| {{
                    (u.len() == total_len && u.starts_with(origin) && &u[origin.len()..] == href)
                        .then_some(idx)
                }}) {{
                    self.cache_raw_href(raw_h, href, cid);
                    return Ok(cid);
                }}
            }}
            if total_len > self.limits.max_link_url_bytes
                || total_len > self.limits.max_link_bytes.saturating_sub(self.link_bytes)
            {{
                return Err("resolved link byte budget exceeded");
            }}
            self.link_bytes += total_len;
            let id = u32::try_from(self.links.len()).map_err(|_| "link ID overflow")?;
            let mut resolved = String::with_capacity(total_len);
            resolved.push_str(origin);
            resolved.push_str(href);
            if *self.link_ids.entry(url_h).or_insert(id) != id {{
                self.link_collisions.push((resolved.clone(), id));
            }}
            self.links.push(resolved);
            self.cache_raw_href(raw_h, href, id);
            return Ok(id);
        }}
        let mut link_idx = NONE;
        if let Ok(target) = self.base.join(href)
            && target.scheme() == "https"
            && target.username().is_empty()
            && target.password().is_none()
        {{
            let target_str = target.as_str();
            let url_h = hash_bytes(target_str.as_bytes());
            if let Some(&id) = self.link_ids.get(&url_h) {{
                if self.links[id as usize] == target_str {{
                    link_idx = id;
                }} else if let Some(&cid) = self
                    .link_collisions
                    .iter()
                    .find_map(|(u, idx)| (u == target_str).then_some(idx))
                {{
                    link_idx = cid;
                }}
            }}
            if link_idx == NONE {{
                if target_str.len() > self.limits.max_link_url_bytes
                    || target_str.len() > self.limits.max_link_bytes.saturating_sub(self.link_bytes)
                {{
                    return Err("resolved link byte budget exceeded");
                }}
                self.link_bytes += target_str.len();
                link_idx = u32::try_from(self.links.len()).map_err(|_| "link ID overflow")?;
                let owned: String = target.into();
                if *self.link_ids.entry(url_h).or_insert(link_idx) != link_idx {{
                    self.link_collisions.push((owned.clone(), link_idx));
                }}
                self.links.push(owned);
            }}
        }}
        self.cache_raw_href(raw_h, href, link_idx);
        Ok(link_idx)
    }}

    fn add_element(
        &mut self,
        parent: NodeId,
        name: &str,
        href: Option<&str>,
    ) -> Result<NodeId, &'static str> {{
        if self.drafts.len() >= self.limits.max_nodes - 1 {{
            return Err("max DOM nodes exceeded");
        }}
        if self.drafts.is_empty() && self.node_cap > 0 {{
            self.drafts.reserve(self.node_cap);
        }}
        let t = tag(name);
        let link_idx = if self.limits.include_links
            && t == Tag::A
            && let Some(href) = href
        {{
            self.resolve_link(href)?
        }} else {{
            NONE
        }};
        let id = u32::try_from(self.drafts.len() + 1).map_err(|_| "node ID overflow")?;
        self.drafts.push(Draft {{
            parent,
            start_or_link: link_idx,
            len: 0,
            tag: t,
        }});
        Ok(id)
    }}

    fn add_text(&mut self, parent: NodeId, value: &str) -> Result<(), &'static str> {{
        if value.is_empty() {{
            return Ok(());
        }}
        if self.drafts.len() >= self.limits.max_nodes - 1 {{
            return Err("max DOM nodes exceeded");
        }}
        if value.len() > self.limits.max_text_bytes.saturating_sub(self.text.len()) {{
            return Err("decoded text byte budget exceeded");
        }}
        if self.drafts.is_empty() && self.node_cap > 0 {{
            self.drafts.reserve(self.node_cap);
        }}
        if self.text.is_empty() && self.text_cap > 0 {{
            self.text.reserve(self.text_cap.max(value.len()));
        }}
        let start = self.text.len() as u32;
        self.text.extend_from_slice(value.as_bytes());
        self.drafts.push(Draft {{
            parent,
            start_or_link: start,
            len: value.len() as u32,
            tag: Tag::Text,
        }});
        Ok(())
    }}

    fn finish(self, parse_warnings: usize) -> Result<Projection, &'static str> {{
        let Self {{
            limits,
            text,
            drafts,
            link_ids,
            mut links,
            link_collisions,
            raw_hrefs,
            raw_cache,
            ..
        }} = self;
        drop(link_ids);
        drop(link_collisions);
        drop(raw_hrefs);
        drop(raw_cache);
        let mut dom = DomArena::new(Bytes::from(text), drafts.len() + 1);
        for (i, draft) in drafts.into_iter().enumerate() {{
            let range = if draft.tag == Tag::Text {{
                Some((draft.start_or_link as usize, draft.len as usize))
            }} else {{
                None
            }};
            let idx = dom
                .append_child(draft.parent, draft.tag, range)
                .map_err(|_| "DOM arena capacity/depth/range error")?;
            debug_assert_eq!(idx as usize, i + 1);
            if draft.tag != Tag::Text && draft.start_or_link != NONE {{
                dom.set_attribute_id(idx, draft.start_or_link)
                    .map_err(|_| "invalid link index")?;
            }}
        }}
        let mut chunks = build_chunks(&dom, limits.max_words_per_chunk, links.len());
        apply_boilerplate_policy(&dom, &mut chunks, &mut links, limits.boilerplate);
        Ok(Projection {{
            dom,
            chunks,
            links,
            parse_warnings,
        }})
    }}
}}

'''
    text = _region(
        text,
        "struct Draft {",
        "#[cfg(feature = \"research-probes\")]\n#[derive(Debug, Clone, Copy, PartialEq, Eq)]\npub enum ResearchNodeKind {",
        helpers + "\n" + builder,
    )
    if fuse:
        finish_chunks_fn = r'''fn is_clean_single_spaced(bytes: &[u8], max_words: usize) -> bool {
    if bytes.is_empty() || max_words == 0 {
        return false;
    }
    let mut words = 0usize;
    let mut in_word = false;
    for &b in bytes {
        if b == b' ' {
            if !in_word {
                return false;
            }
            in_word = false;
        } else if b <= b' ' || b >= 0x80 {
            if b.is_ascii_whitespace() || b >= 0x80 {
                return false;
            }
            if !in_word {
                words += 1;
                if words > max_words {
                    return false;
                }
                in_word = true;
            }
        } else if !in_word {
            words += 1;
            if words > max_words {
                return false;
            }
            in_word = true;
        }
    }
    in_word
}

fn build_chunks_fused(
    mut chunks: Vec<Chunk>,
    max_words_per_chunk: usize,
    link_count: usize,
    has_many_links: bool,
) -> Vec<Chunk> {
    if chunks.is_empty() {
        return Vec::new();
    }
    if has_many_links {
        let mut last_owner = vec![0u32; link_count];
        for chunk in &mut chunks {
            if chunk.links.len() > 8 {
                chunk.links.retain(|&id| {
                    let previous = &mut last_owner[id as usize];
                    if *previous == chunk.node {
                        return false;
                    }
                    *previous = chunk.node;
                    true
                });
            }
        }
    }
    for chunk in &mut chunks {
        if chunk.role == "preformatted" {
            if let Some(excluded) = chunk.text.split_whitespace().nth(max_words_per_chunk) {
                let start = excluded.as_ptr() as usize - chunk.text.as_ptr() as usize;
                let end = chunk.text[..start].trim_end().len();
                chunk.text.truncate(end);
                if chunk.text.capacity() > chunk.text.len().saturating_add(128) {
                    chunk.text.shrink_to_fit();
                }
            }
        } else if !is_clean_single_spaced(chunk.text.as_bytes(), max_words_per_chunk) {
            let mut normalized = String::with_capacity(
                chunk
                    .text
                    .trim()
                    .len()
                    .min(max_words_per_chunk.saturating_mul(8)),
            );
            for word in chunk.text.split_whitespace().take(max_words_per_chunk) {
                if !normalized.is_empty() {
                    normalized.push(' ');
                }
                normalized.push_str(word);
            }
            chunk.text = normalized;
        }
    }
    chunks.retain(|c| !c.text.is_empty());
    chunks
}

'''
        text = _region(
            text,
            "fn build_chunks(dom: &DomArena, max_words_per_chunk: usize, link_count: usize) -> Vec<Chunk> {",
            "pub fn parse_html(",
            finish_chunks_fn,
        )
    text = _replace(
        text,
        '''    let mut builder = ProjectionBuilder::new(base, limits);
    let mut stack = vec![(parsed.tree.root(), 0u32)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                let name = el.name();
                if skip_subtree(name) {
                    visit_children = false;
                } else {
                    let href = if limits.include_links && name == "a" {
                        el.attr("href")
                    } else {
                        None
                    };
                    next_parent = builder.add_element(parent, name, href)?;
                }
            }
            HtmlNode::Text(content) => {
                builder.add_text(parent, content.text.as_ref())?;
                visit_children = false;
            }
            _ => {}
        }
        if visit_children {
            // Stack-based traversal prevents source-controlled call-stack recursion.
            for child in node.children().rev() {
                stack.push((child, next_parent));
            }
        }
    }
    builder.finish(parse_warnings)''',
        '''    let mut builder = ProjectionBuilder::with_capacity(base, limits, html.len(), parsed.tree.values().len());
    let mut stack = Vec::with_capacity(if html.len() < 128 { 4 } else { 64 });
    stack.push((parsed.tree.root(), 0u32));
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            HtmlNode::Element(el) => {
                let name = el.name();
                if skip_subtree(name) {
                    visit_children = false;
                } else {
                    let href = if limits.include_links && name == "a" { el.attr("href") } else { None };
                    next_parent = builder.add_element(parent, name, href)?;
                }
            }
            HtmlNode::Text(content) => {
                builder.add_text(parent, content.text.as_ref())?;
                visit_children = false;
            }
            _ => {}
        }
        if visit_children {
            // Stack-based traversal prevents source-controlled call-stack recursion.
            for child in node.children().rev() {
                stack.push((child, next_parent));
            }
        }
    }
    drop(stack);
    builder.finish(parse_warnings)''',
    )
    text = _replace(
        text,
        '''    let mut builder = ProjectionBuilder::new(base, limits);
    let mut projected_ids = vec![0u32; nodes.len()];
    let mut suppressed = vec![false; nodes.len()];
    for (index, node) in nodes.iter().enumerate() {
        let parent_index = match node.parent {
            Some(parent) if parent < index => Some(parent),
            Some(_) => return Err("invalid research parser tree"),
            None => None,
        };
        let parent_id = parent_index.map_or(0, |parent| projected_ids[parent]);
        if parent_index.is_some_and(|parent| suppressed[parent]) {
            suppressed[index] = true;
            continue;
        }

        match node.kind {
            ResearchNodeKind::Element => {
                if skip_subtree(&node.name) {
                    suppressed[index] = true;
                } else {
                    let href = if limits.include_links && node.name == "a" {
                        node.attr("href")
                    } else {
                        None
                    };
                    projected_ids[index] = builder.add_element(parent_id, &node.name, href)?;
                }
            }''',
        '''    let mut builder = ProjectionBuilder::with_capacity(base, limits, html_len, nodes.len());
    let mut projected_ids = vec![0u32; nodes.len()];
    let mut suppressed = vec![false; nodes.len()];
    for (index, node) in nodes.iter().enumerate() {
        let parent_index = match node.parent {
            Some(parent) if parent < index => Some(parent),
            Some(_) => return Err("invalid research parser tree"),
            None => None,
        };
        let parent_id = parent_index.map_or(0, |parent| projected_ids[parent]);
        if parent_index.is_some_and(|parent| suppressed[parent]) {
            suppressed[index] = true;
            continue;
        }

        match node.kind {
            ResearchNodeKind::Element => {
                if skip_subtree(&node.name) {
                    suppressed[index] = true;
                } else {
                    let href = if limits.include_links && node.name == "a" {
                        node.attr("href")
                    } else {
                        None
                    };
                    projected_ids[index] = builder.add_element(parent_id, &node.name, href)?;
                }
            }''',
    )
    return text


def transform(files, variant_id):
    from cloud_experiments import libraries
    _, factors = _resolve(variant_id)
    output = libraries.transform(files, factors)
    affected = set()
    for factor, source in [("writer", "src/aae.rs"), ("hash", "src/aae.rs"), ("reserve", "src/aae.rs"), ("lookup", "src/rank.rs"), ("selection", "src/rank.rs"), ("projection", "src/projection.rs"), ("links", "src/projection.rs"), ("table", "src/table.rs")]:
        if factor in factors:
            affected.add(source)
    for source in affected:
        data = output.get(source)
        if not isinstance(data, bytes) or sha256(data.replace(b"\r\n", b"\n")).hexdigest() != _SOURCE_SHA256[source]:
            raise ValueError(f"Frozen source differs from reviewed normalized bytes: {source}")
    if factors.get("json_library") == "sonic":
        affected.add("src/aae.rs")
        data = output.get("src/aae.rs")
        if not isinstance(data, bytes) or sha256(data.replace(b"\r\n", b"\n")).hexdigest() != _SOURCE_SHA256["src/aae.rs"]:
            raise ValueError("Unreviewed AAE source for Sonic adapter")

    def edit(path, function):
        if path not in output or not isinstance(output[path], bytes):
            raise ValueError(f"Frozen source missing bytes for {path}")
        text = output[path].decode("utf-8").replace("\r\n", "\n")
        output[path] = function(text).encode("utf-8")

    if "writer" in factors:
        edit("src/aae.rs", lambda text: _writer(text, factors["writer"]))
    if factors.get("json_library") == "sonic":
        edit("src/aae.rs", _sonic_serializer)
        probe = "cloud_experiments/core_probe.rs"
        edit(probe, lambda text: _replace(text, 'let bytes = serde_json::to_vec(frame).map_err(|e| e.to_string())?;', 'let bytes = sonic_rs::to_vec(frame).map_err(|e| e.to_string())?;'))
    if "hash" in factors:
        edit("src/aae.rs", lambda text: _hash_source(text, factors["hash"]))
    if "reserve" in factors:
        edit("src/aae.rs", lambda text: _reserve_source(text, factors["reserve"]))
    if "src/aae.rs" in affected:
        edit(
            "src/aae.rs",
            lambda text: (
                text
                if "BufWriter::" in text
                else _replace(
                    text,
                    "use std::io::{self, BufWriter, Write};",
                    "use std::io::{self, Write};",
                )
            ),
        )
    if "lookup" in factors or "selection" in factors:
        edit(
            "src/rank.rs",
            lambda text: _replace(
                text,
                "fn lowercase(word: &str) -> Cow<'_, str> {\n"
                "    if word\n"
                "        .bytes()\n"
                "        .all(|b| b.is_ascii() && !b.is_ascii_uppercase())\n"
                "    {\n"
                "        Cow::Borrowed(word)\n"
                "    } else {\n"
                "        Cow::Owned(word.to_lowercase())\n"
                "    }\n"
                "}",
                "fn lowercase(word: &str) -> Cow<'_, str> {\n"
                "    if word.is_ascii() {\n"
                "        if word.bytes().all(|b| !b.is_ascii_uppercase()) {\n"
                "            Cow::Borrowed(word)\n"
                "        } else {\n"
                "            Cow::Owned(word.to_ascii_lowercase())\n"
                "        }\n"
                "    } else {\n"
                "        Cow::Owned(word.to_lowercase())\n"
                "    }\n"
                "}",
            ),
        )
    if "lookup" in factors:
        def lookup(text):
            text = _replace(text, "use std::collections::{HashMap, HashSet};", "use std::collections::HashSet;")
            text = _replace(text, "    let mut terms: Vec<_> = words(query).map(str::to_lowercase).collect();", "    let mut terms: Vec<_> = words(query).map(lowercase).collect();")
            setup = (
                "    let mut min_len = usize::MAX;\n"
                "    let mut max_len = 0usize;\n"
                "    let mut first_mask = [0u64; 4];\n"
                "    let mut last_mask = [0u64; 4];\n"
                "    for term in &terms {\n"
                "        let bytes = term.as_bytes();\n"
                "        min_len = min_len.min(bytes.len());\n"
                "        max_len = max_len.max(bytes.len());\n"
                "        for b0 in [bytes[0], bytes[0].to_ascii_uppercase()] {\n"
                "            first_mask[(b0 >> 6) as usize] |= 1u64 << (b0 & 63);\n"
                "        }\n"
                "        let last = bytes[bytes.len() - 1];\n"
                "        for b1 in [last, last.to_ascii_uppercase()] {\n"
                "            last_mask[(b1 >> 6) as usize] |= 1u64 << (b1 & 63);\n"
                "        }\n"
                "    }\n"
            )
            if factors["lookup"] == "binary":
                setup += (
                    "    let mut hash_slots = Vec::new();\n"
                    "    if terms.len() > 8 && (chunks.len() > 1 || chunks[0].text.len() > 64) {\n"
                    "        let cap = (terms.len() * 4).next_power_of_two();\n"
                    "        let mask = cap - 1;\n"
                    "        hash_slots.resize(cap, usize::MAX);\n"
                    "        for (idx, term) in terms.iter().enumerate() {\n"
                    "            let mut h = term.len();\n"
                    "            for &byte in term.as_bytes() {\n"
                    "                h = h\n"
                    "                    .wrapping_mul(0x517cc1b727220a95)\n"
                    "                    .wrapping_add(byte as usize);\n"
                    "            }\n"
                    "            let mut slot = (h ^ (h >> 16)) & mask;\n"
                    "            while hash_slots[slot] != usize::MAX {\n"
                    "                slot = (slot + 1) & mask;\n"
                    "            }\n"
                    "            hash_slots[slot] = idx;\n"
                    "        }\n"
                    "    }\n"
                )
            setup += (
                "    let reserve_cap = (chunks.len() >> 2).clamp(2, 64);\n"
                "    let mut postings = vec![Vec::<(usize, usize)>::new(); terms.len()];\n"
                "    let mut lengths = Vec::with_capacity(chunks.len());\n"
            )
            text = _region(
                text,
                "    let term_ids: HashMap<_, _> = terms",
                "    for (i, chunk) in chunks.iter().enumerate() {",
                setup,
            )
            operation = (
                "            let matched = if hash_slots.is_empty() {\n"
                "                terms\n"
                "                    .binary_search_by(|term| term.as_ref().cmp(key.as_ref()))\n"
                "                    .ok()\n"
                "            } else {\n"
                "                let mask = hash_slots.len() - 1;\n"
                "                let mut h = bytes.len();\n"
                "                for &byte in bytes {\n"
                "                    h = h\n"
                "                        .wrapping_mul(0x517cc1b727220a95)\n"
                "                        .wrapping_add(byte as usize);\n"
                "                }\n"
                "                let mut slot = (h ^ (h >> 16)) & mask;\n"
                "                loop {\n"
                "                    let idx = hash_slots[slot];\n"
                "                    if idx == usize::MAX {\n"
                "                        break None;\n"
                "                    }\n"
                "                    if terms[idx].as_ref() == key.as_ref() {\n"
                "                        break Some(idx);\n"
                "                    }\n"
                "                    slot = (slot + 1) & mask;\n"
                "                }\n"
                "            };\n"
                "            if let Some(term) = matched {"
                if factors["lookup"] == "binary"
                else "            if let Some(term) = terms.iter().position(|term| term.as_ref() == key.as_ref()) {"
            )
            text = _replace(
                text,
                "            if let Some(&term) = term_ids.get(lowercase(word).as_ref()) {\n"
                "                if counts[term] == 0 {\n"
                "                    touched.push(term);\n"
                "                }\n"
                "                counts[term] += 1;\n"
                "            }\n"
                "        }\n"
                "        lengths.push(length);\n"
                "        for term in touched.drain(..) {\n"
                "            postings[term].push((i, counts[term]));\n"
                "            counts[term] = 0;\n"
                "        }",
                "            let raw = word.as_bytes();\n"
                "            if (raw[0] < 128 && (first_mask[(raw[0] >> 6) as usize] & (1u64 << (raw[0] & 63))) == 0)\n"
                "                || (raw[raw.len() - 1] < 128\n"
                "                    && (last_mask[(raw[raw.len() - 1] >> 6) as usize]\n"
                "                        & (1u64 << (raw[raw.len() - 1] & 63)))\n"
                "                        == 0)\n"
                "            {\n"
                "                continue;\n"
                "            }\n"
                "            let is_ascii = word.is_ascii();\n"
                "            if is_ascii && !(min_len..=max_len).contains(&raw.len()) {\n"
                "                continue;\n"
                "            }\n"
                "            let key = lowercase(word);\n"
                "            let bytes = key.as_bytes();\n"
                "            if !is_ascii\n"
                "                && (!(min_len..=max_len).contains(&bytes.len())\n"
                "                    || (first_mask[(bytes[0] >> 6) as usize] & (1u64 << (bytes[0] & 63))) == 0\n"
                "                    || (last_mask[(bytes[bytes.len() - 1] >> 6) as usize]\n"
                "                        & (1u64 << (bytes[bytes.len() - 1] & 63)))\n"
                "                        == 0)\n"
                "            {\n"
                "                continue;\n"
                "            }\n"
                + operation + "\n"
                "                let list = &mut postings[term];\n"
                "                if list.is_empty() {\n"
                "                    list.reserve(reserve_cap);\n"
                "                    list.push((i, 1));\n"
                "                } else if let Some((last, count)) = list.last_mut()\n"
                "                    && *last == i\n"
                "                {\n"
                "                    *count += 1;\n"
                "                } else {\n"
                "                    list.push((i, 1));\n"
                "                }\n"
                "            }\n"
                "        }\n"
                "        lengths.push(length);",
            )
            return text
        edit("src/rank.rs", lookup)
    if "selection" in factors:
        def select(text):
            text = _replace(text, "pub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {", "#[inline(always)]\nfn score_chunks(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {")
            text = _replace(text, "    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));\n    scores\n}", "    scores\n}\npub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {\n    let mut scores = score_chunks(chunks, query, k1, b);\n    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));\n    scores\n}")
            return _region(text, "pub fn highlight_indices(", "#[cfg(test)]", _HEAP_SELECTION)
        edit("src/rank.rs", select)
    if "projection" in factors or "links" in factors:
        edit(
            "src/projection.rs",
            lambda text: _transform_projection(
                text,
                fuse="projection" in factors,
                btree="links" in factors,
            ),
        )
    if "table" in factors:
        edit("src/table.rs", lambda text: _region(text, "fn resize_row(", "#[cfg(test)]", _SPARSE_TABLE))
    return output


def _self_test():
    import unittest
    from pathlib import Path
    from cloud_experiments import experimental_design, research

    class Contracts(unittest.TestCase):
        def test_real_rivals_and_composition_reject_unknown_or_conflicting_factors(self):
            registry = definitions()
            self.assertTrue({"baseline", "aae-direct", "aae-reuse", "rank-linear", "rank-select", "projection-fused", "table-sparse", "build-lto-off"} <= registry.keys())
            with self.assertRaises(ValueError):
                transform({}, "not-registered")
            with self.assertRaises(ValueError):
                transform({}, "rank-linear+rank-binary")
            composed = research.compose_finalist([
                "aae-direct", "rank-select", "prepared", "projection-fused", "links-btree", "table-sparse", "build-native"
            ])
            self.assertEqual(
                composed,
                "aae-direct+rank-select+prepared+projection-fused+links-btree+table-sparse+build-native",
            )
            self.assertEqual(
                metadata(composed)["decisions"],
                ["H01", "H02", "H03", "H04", "H05", "H06", "H10"],
            )

        def test_unreviewed_source_is_rejected_before_transformation(self):
            source = (Path(__file__).resolve().parents[1] / "src/aae.rs").read_bytes()
            with self.assertRaises(ValueError):
                transform({"src/aae.rs": source + b"\n// controlled unreviewed change\n"}, "aae-direct")

        def test_baseline_preserves_bytes_and_composition_does_not_mutate_input(self):
            import shutil
            import subprocess
            import tempfile
            root = Path(__file__).resolve().parents[1]
            names = [
                "src/aae.rs",
                "src/rank.rs",
                "src/projection.rs",
                "src/table.rs",
                "cloud_experiments/core_probe.rs",
                "Cargo.toml",
                "Cargo.lock",
            ]
            source = {name: (root / name).read_bytes() for name in names}
            for lock_path in (root / "cloud_experiments/locks").glob("*.lock"):
                source[f"cloud_experiments/locks/{lock_path.name}"] = lock_path.read_bytes()
            before = dict(source)
            self.assertEqual(transform(source, "baseline"), before)
            changed = transform(source, "aae-reuse+aae-batched4096+rank-linear+rank-select+projection-fused+links-btree+table-sparse")
            self.assertEqual(source, before)
            self.assertEqual(changed["Cargo.toml"], before["Cargo.toml"])
            self.assertEqual(changed["Cargo.lock"], before["Cargo.lock"])
            self.assertTrue(all(changed[name] != before[name] for name in ["src/aae.rs", "src/rank.rs", "src/projection.rs", "src/table.rs"]))
            self.assertIn(b"let mut row_widths = vec![0usize; rows.len()];", changed["src/table.rs"])
            for variant_id in experimental_design.native_design()["variants"]:
                out = transform(source, variant_id)
                aae_txt = out["src/aae.rs"].decode("utf-8")
                self.assertEqual("BufWriter::" in aae_txt, "use std::io::{self, BufWriter, Write};" in aae_txt, variant_id)
                rank_txt = out["src/rank.rs"].decode("utf-8")
                self.assertEqual("HashMap<" in rank_txt, "use std::collections::{HashMap, HashSet};" in rank_txt, variant_id)
                proj_txt = out["src/projection.rs"].decode("utf-8")
                self.assertEqual("BTreeMap<" in proj_txt, "use std::collections::{BTreeMap, HashMap};" in proj_txt, variant_id)
            rustc = shutil.which("rustc")
            if rustc is not None:
                extra_test = (
                    "    #[test]\n"
                    "    fn slot_limit_takes_precedence_over_later_overlap() {\n"
                    "        let rows = [\n"
                    "            Row {\n"
                    "                group: 0,\n"
                    "                cells: vec![cell(\"a\", 2, 1), cell(\"b\", 1, 1), cell(\"c\", 2, 1)],\n"
                    "            },\n"
                    "            Row {\n"
                    "                group: 0,\n"
                    "                cells: vec![cell(\"d\", 1, 3)],\n"
                    "            },\n"
                    "        ];\n"
                    "        assert_eq!(expand_with_slot_limit(&rows, 2, 4, 6), Err(GridError::TooManySlots));\n"
                    "    }\n"
                )
                table_rs = changed["src/table.rs"].decode("utf-8").replace(
                    "    #[test]\n    fn sparse_grid",
                    extra_test + "    #[test]\n    fn sparse_grid",
                    1,
                )
                with tempfile.TemporaryDirectory(prefix="athena-sparse-table-test-") as folder:
                    src_path = Path(folder) / "table_test.rs"
                    bin_path = Path(folder) / ("table_test.exe" if Path(rustc).suffix.lower() == ".exe" else "table_test")
                    src_path.write_text(table_rs, encoding="utf-8")
                    subprocess.run([rustc, "--edition=2024", "--test", str(src_path), "-o", str(bin_path)], check=True)
                    subprocess.run([str(bin_path)], check=True, stdout=subprocess.DEVNULL)
                    chunk_stub = (
                        "#[derive(Debug, Clone, PartialEq, Eq)]\n"
                        "pub struct Chunk {\n"
                        "    pub node: u32,\n"
                        "    pub role: &'static str,\n"
                        "    pub text: String,\n"
                        "    pub links: Vec<u32>,\n"
                        "}\n"
                    )
                    rank_extra_test = (
                        "    #[test]\n"
                        "    fn lookup_bloom_and_adaptive_hashmap_preserve_exact_scores_and_utf8_folding() {\n"
                        "        let single_tiny = [chunk(\"term0 term15 term127 İB evidence\")];\n"
                        "        let single_large = [chunk(&format!(\"term0 term15 term31 term64 term127 İB {}\", \"fillerword \".repeat(32)))];\n"
                        "        let multi = [\n"
                        "            chunk(\"term0 term1 term8 term31 term127 alpha beta\"),\n"
                        "            chunk(\"term2 term8 term15 term64 İB delta\"),\n"
                        "            chunk(\"term0 term31 term64 term127 epsilon\"),\n"
                        "        ];\n"
                        "        let q3 = \"term0 term8 i\\u{307}b\";\n"
                        "        let q32 = (0..32).map(|i| format!(\"term{i}\")).collect::<Vec<_>>().join(\" \");\n"
                        "        let q128 = (0..128).map(|i| format!(\"term{i}\")).collect::<Vec<_>>().join(\" \");\n"
                        "        for q in [q3.as_str(), q32.as_str(), q128.as_str()] {\n"
                        "            assert!(!bm25(&single_tiny, q, 1.2, 0.75).is_empty());\n"
                        "            assert!(!bm25(&single_large, q, 1.2, 0.75).is_empty());\n"
                        "            assert!(!bm25(&multi, q, 1.2, 0.75).is_empty());\n"
                        "            assert!(!highlight_indices(&multi, q, 2).is_empty());\n"
                        "        }\n"
                        "    }\n"
                    )
                    for rank_variant in ("rank-binary+rank-select", "rank-linear+rank-select"):
                        rank_rs = (
                            transform(source, rank_variant)["src/rank.rs"]
                            .decode("utf-8")
                            .replace("use crate::projection::Chunk;", chunk_stub, 1)
                            .replace(
                                "    #[test]\n    fn bm25_orders",
                                rank_extra_test + "    #[test]\n    fn bm25_orders",
                                1,
                            )
                        )
                        rank_src = Path(folder) / f"{rank_variant.replace('+', '_')}.rs"
                        rank_bin = Path(folder) / (
                            f"{rank_variant.replace('+', '_')}.exe"
                            if Path(rustc).suffix.lower() == ".exe"
                            else rank_variant.replace("+", "_")
                        )
                        rank_src.write_text(rank_rs, encoding="utf-8")
                        subprocess.run([rustc, "--edition=2024", "--test", str(rank_src), "-o", str(rank_bin)], check=True)
                        subprocess.run([str(rank_bin)], check=True, stdout=subprocess.DEVNULL)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    _self_test()
