"""Immutable, fail-closed source contenders for the native research campaign."""
from __future__ import annotations
from copy import deepcopy
from hashlib import sha256


_DEFAULTS = {"writer": "frame", "hash": "joined", "reserve": "none", "lookup": "hash", "selection": "sort", "prepared": "oneshot", "projection": "separate", "links": "hash", "table": "dense", "html_extractor": "traversal", "html_headers": "explicit", "lto": "thin", "codegen_units": 8, "cpu": "portable", "digest_library": "default", "html_library": "default", "json_library": "serde"}
_STAGES = ["parser", "parse", "projection", "export", "pipeline", "rank", "selection", "highlight", "hash", "block_ids", "serialization", "prepared", "table", "html-table", "budget"]
_SOURCE_SHA256 = {
    "src/aae.rs": "f5806098dfcce627b57d93ad9795fbc3599c8dce64905484feebfbfee216e983",
    "src/rank.rs": "bc5262f761c6fef1112cbca89b3984245b94e961ef43e86810d3c08e0563d22d",
    "src/projection.rs": "7f201ea9e43a357ff36d84040503df8065e61fdc378016e4704a55b9792a72d2",
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
        "html-table": "Same locked HTML5 parser plus table/row enumeration, explicit attributes, existing public span-aware grid construction and drop; the bounded scope-derived header mode is a separate stage-only comparator, not a full inferred-header or production integration claim.",
        "budget": "Actual oversized public Projection export; only the specific AAE output byte-budget error is expected.",
        "controls": "Independent literal/numeric real-core controls; a correctness command, not a performance stage.",
    }


def _definition(decisions, factors, description, scope="whole_path", stages=None):
    return {"decisions": decisions, "factors": factors, "description": description, "implementation_scope": scope, "stages": stages or _STAGES, "build": _build(factors)}


def _build(factors):
    probe = {}
    for factor, variable in [("writer", "ATHENA_RESEARCH_WRITER"), ("hash", "ATHENA_RESEARCH_HASH"), ("reserve", "ATHENA_RESEARCH_RESERVE"), ("prepared", "ATHENA_RESEARCH_PREPARED"), ("html_extractor", "ATHENA_RESEARCH_TABLE_EXTRACTOR"), ("html_headers", "ATHENA_RESEARCH_TABLE_HEADERS")]:
        if factor in factors:
            probe[variable] = str(factors[factor])
    profile = {}
    if "lto" in factors:
        profile["CARGO_PROFILE_RELEASE_LTO"] = factors["lto"]
    if "codegen_units" in factors:
        profile["CARGO_PROFILE_RELEASE_CODEGEN_UNITS"] = str(factors["codegen_units"])
    rustflags = ["-C", "target-cpu=native"] if factors.get("cpu") == "native" else []
    return {"env": {**profile, **probe}, "cargo_profile_env": profile, "probe_env": probe, "rustflags": rustflags}


def definitions():
    return deepcopy({
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
        "table-html-selectors": _definition([], {"html_extractor": "selectors"}, "Precompiled CSS table/row selection over the same Scraper HTML5 DOM, with nearest-table ownership and the existing public span-aware grid.", "html_table_stage_only", ["html-table"]),
        "table-html-scope": _definition(["H06"], {"html_headers": "scope"}, "Bounded scope-derived row/column header associations on the same HTML5 DOM and logical grid; explicit headers take precedence; no full WHATWG inferred-header claim.", "html_table_stage_only", ["html-table"]),
        "build-lto-off": _definition(["H10"], {"lto": "off"}, "Keep source/lock fixed and disable release LTO.", "build_factor"),
        "build-lto-full": _definition(["H10"], {"lto": "fat"}, "Keep source/lock fixed and enable full release LTO.", "build_factor"),
        "build-codegen1": _definition(["H10"], {"codegen_units": 1}, "Keep source/lock fixed and compile release with one codegen unit.", "build_factor"),
        "build-native": _definition(["H10"], {"cpu": "native"}, "Compile for the measurement host CPU; deployment portability remains unverified.", "build_factor"),
        "lib-blake3-pure": _definition(["H01"], {"digest_library": "pure"}, "Locked BLAKE3 1.8.7 pure Rust backend; same digest identity.", "dependency_factor"),
        "lib-blake3-intrinsics": _definition(["H01"], {"digest_library": "intrinsics"}, "Locked BLAKE3 1.8.7 prefer_intrinsics backend; runtime CPU detection retained.", "dependency_factor"),
        "lib-scraper-lean": _definition(["H04", "H05"], {"html_library": "lean"}, "Locked Scraper 0.27.0 without CLI dependencies; HTML5 parse warnings retained.", "dependency_factor"),
        "lib-scraper-atomic": _definition(["H04", "H05"], {"html_library": "atomic"}, "Locked Scraper 0.27.0 atomic tendril reference counting; default error reporting retained.", "dependency_factor"),
        "lib-json-sonic": _definition(["H01"], {"json_library": "sonic"}, "Sonic 0.5.10 framed typed AAE serializer; serde_json input parsing unchanged. No bounded Sonic streaming adapter claim.", "dependency_factor"),
    })


def _resolve(variant_id):
    registry = definitions()
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
    return "+".join(name for name in definitions() if name in selected) or "baseline"


def metadata(variant_id):
    ids, factors = _resolve(variant_id)
    registry = definitions()
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
        text = _replace(text, "use std::io::{self, BufWriter, Write};", "use std::io::{self, Write};")
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
        text = _replace(text, "use std::io::{self, BufWriter, Write};", "use std::io::{self, Write};")
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
    return r'''    let mut hash = blake3::Hasher::new();
    let mut scratch = Vec::with_capacity(BATCH);
    for (i, chunk) in page.chunks.iter().enumerate() {
        let separator = if i == 0 { b"".as_slice() } else { b"\n".as_slice() };
        for mut bytes in [separator, chunk.text.as_bytes()] {
            if scratch.is_empty() && bytes.len() >= BATCH {
                hash.update(bytes);
                continue;
            }
            while !bytes.is_empty() {
                let take = bytes.len().min(BATCH - scratch.len());
                scratch.extend_from_slice(&bytes[..take]);
                bytes = &bytes[take..];
                if scratch.len() == BATCH { hash.update(&scratch); scratch.clear(); }
            }
        }
    }
    hash.update(&scratch);
    let digest = hash.finalize().to_hex().to_string();
'''.replace("BATCH", str(batch))


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


_HEAP_SELECTION = r'''#[derive(Debug)]
struct Ranked { index: usize, score: f64 }
impl PartialEq for Ranked { fn eq(&self, other: &Self) -> bool { self.cmp(other).is_eq() } }
impl Eq for Ranked {}
impl PartialOrd for Ranked { fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> { Some(self.cmp(other)) } }
impl Ord for Ranked {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.score.total_cmp(&other.score).then_with(|| other.index.cmp(&self.index))
    }
}
pub fn highlight_indices(chunks: &[Chunk], query: &str, limit: usize) -> Vec<usize> {
    if limit == 0 { return Vec::new(); }
    let scores = score_chunks(chunks, query, 1.2, 0.75);
    let mut heap = std::collections::BinaryHeap::from(scores.into_iter()
        .filter(|(_, score)| *score > 0.0)
        .map(|(index, score)| Ranked { index, score }).collect::<Vec<_>>());
    let mut selected = Vec::new();
    let mut seen = HashSet::<String>::new();
    while let Some(Ranked { index, .. }) = heap.pop() {
        if seen.insert(chunks[index].text.to_lowercase()) { selected.push(index); }
        if selected.len() == limit { break; }
    }
    selected
}

'''


_SPARSE_TABLE = r'''pub fn expand(rows: &[Row], max_rows: usize, max_cols: usize) -> Result<Grid, GridError> {
    if rows.len() > max_rows || max_cols == 0 { return Err(GridError::TooLarge); }
    if rows.windows(2).any(|pair| pair[0].group > pair[1].group) { return Err(GridError::InvalidGroupOrder); }
    let mut occupied = vec![std::collections::BTreeMap::<usize, usize>::new(); rows.len()];
    let mut cells = Vec::new();
    let mut width = 0;
    let mut group_end = 0;
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
            for line in occupied.iter_mut().take(r + dr).skip(r) {
                for col in c..end_col {
                    if line.insert(col, index).is_some() { return Err(GridError::Overlap); }
                }
            }
            cells.push(cell.clone());
            width = width.max(end_col);
            c = end_col;
        }
    }
    let slots = occupied.into_iter().map(|line| {
        let mut dense = vec![None; width];
        for (col, cell) in line { dense[col] = Some(cell); }
        dense
    }).collect();
    Ok(Grid { cells, slots, width })
}
'''


def _fuse_projection(text):
    start = "fn build_chunks(dom: &DomArena, max_words_per_chunk: usize, link_count: usize) -> Vec<Chunk> {"
    finish = "    // Finish each owner's list after traversal; nested owners can revisit a link."
    text = _region(text, start, finish, "fn finish_chunks(mut chunks: Vec<Chunk>, max_words_per_chunk: usize, link_count: usize) -> Vec<Chunk> {\n")
    text = _replace(text, "    let mut dom = DomArena::new(Bytes::from(text), drafts.len() + 1);", "    let mut chunk_owner = vec![None; drafts.len() + 1];\n    let mut chunks = Vec::<Chunk>::new();\n    let mut dom = DomArena::new(Bytes::from(text), drafts.len() + 1);")
    text = _replace(text, '''        if draft.link_idx != NONE {
            dom.set_attribute_id(idx, draft.link_idx)
                .map_err(|_| "invalid link index")?;
        }
    }
    let chunks = build_chunks(&dom, max_words_per_chunk, links.len());''', '''        if draft.link_idx != NONE {
            dom.set_attribute_id(idx, draft.link_idx)
                .map_err(|_| "invalid link index")?;
        }
        let inherited = chunk_owner[draft.parent as usize];
        let owner = if let Some(kind) = role(draft.tag) {
            let index = chunks.len();
            chunks.push(Chunk { node: idx, role: kind, text: String::new(), links: Vec::new() });
            Some(index)
        } else { inherited };
        chunk_owner[idx as usize] = owner;
        if let Some(index) = owner {
            if draft.tag == Tag::Text && let Ok(segment) = dom.raw_text(idx) { chunks[index].text.push_str(segment); }
            if draft.tag == Tag::A && draft.link_idx != NONE { chunks[index].links.push(draft.link_idx); }
        }
    }
    let chunks = finish_chunks(chunks, max_words_per_chunk, links.len());''')
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
    if "lookup" in factors:
        def lookup(text):
            text = _region(
                text,
                "    let term_ids: HashMap<_, _> = terms",
                "    let mut postings = vec![Vec::<(usize, usize)>::new(); terms.len()];",
                "",
            )
            text = _replace(text, "use std::collections::{HashMap, HashSet};", "use std::collections::HashSet;")
            operation = "if let Ok(term) = terms.binary_search_by(|term| term.as_str().cmp(key.as_ref())) {" if factors["lookup"] == "binary" else "if let Some(term) = terms.iter().position(|term| term.as_str() == key.as_ref()) {"
            return _replace(text, "            if let Some(&term) = term_ids.get(lowercase(word).as_ref()) {", "            let key = lowercase(word);\n            " + operation)
        edit("src/rank.rs", lookup)
    if "selection" in factors:
        def select(text):
            text = _replace(text, "pub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {", "fn score_chunks(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {")
            text = _replace(text, "    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));\n    scores\n}", "    scores\n}\npub fn bm25(chunks: &[Chunk], query: &str, k1: f64, b: f64) -> Vec<(usize, f64)> {\n    let mut scores = score_chunks(chunks, query, k1, b);\n    scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));\n    scores\n}")
            return _region(text, "pub fn highlight_indices(", "#[cfg(test)]", _HEAP_SELECTION)
        edit("src/rank.rs", select)
    if "projection" in factors:
        edit("src/projection.rs", _fuse_projection)
    if "links" in factors:
        edit("src/projection.rs", lambda text: _replace(_replace(text, "use std::collections::HashMap;", "use std::collections::BTreeMap;"), "let mut link_ids = HashMap::<String, u32>::new();", "let mut link_ids = BTreeMap::<String, u32>::new();"))
    if "table" in factors:
        edit("src/table.rs", lambda text: _region(text, "fn resize_row(", "#[cfg(test)]", _SPARSE_TABLE))
    return output


def _self_test():
    import unittest
    from pathlib import Path

    class Contracts(unittest.TestCase):
        def test_real_rivals_and_composition_reject_unknown_or_conflicting_factors(self):
            registry = definitions()
            self.assertTrue({"baseline", "aae-direct", "aae-reuse", "rank-linear", "rank-select", "projection-fused", "table-sparse", "build-lto-off"} <= registry.keys())
            with self.assertRaises(ValueError):
                transform({}, "not-registered")
            with self.assertRaises(ValueError):
                transform({}, "rank-linear+rank-binary")

        def test_unreviewed_source_is_rejected_before_transformation(self):
            source = (Path(__file__).resolve().parents[1] / "src/aae.rs").read_bytes()
            with self.assertRaises(ValueError):
                transform({"src/aae.rs": source + b"\n// controlled unreviewed change\n"}, "aae-direct")

        def test_baseline_preserves_bytes_and_composition_does_not_mutate_input(self):
            root = Path(__file__).resolve().parents[1]
            source = {name: (root / name).read_bytes() for name in ["src/aae.rs", "src/rank.rs", "src/projection.rs", "src/table.rs", "Cargo.toml", "Cargo.lock"]}
            before = dict(source)
            self.assertEqual(transform(source, "baseline"), before)
            changed = transform(source, "aae-reuse+aae-batched4096+rank-linear+rank-select+projection-fused+links-btree+table-sparse")
            self.assertEqual(source, before)
            self.assertEqual(changed["Cargo.toml"], before["Cargo.toml"])
            self.assertEqual(changed["Cargo.lock"], before["Cargo.lock"])
            self.assertTrue(all(changed[name] != before[name] for name in ["src/aae.rs", "src/rank.rs", "src/projection.rs", "src/table.rs"]))

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Contracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    _self_test()
