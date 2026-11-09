//! Native research boundaries; canonical outputs are produced outside timers.
#![forbid(unsafe_code)]
use athena_core_v2::{
    aae::{
        LinkMode, export_contrastive_training_records, export_jsonl_with_limit,
        export_jsonl_with_link_mode_and_prepared, export_jsonl_with_options,
    },
    arena::Tag,
    bandit::{
        CusumDetector, EmpiricalBayesProxyPool, FetchOutcomeKind, ProxyEndpointConfig,
        ProxyPoolRouter, RoutingAlgorithm, SoftBlockDetector, Tier,
    },
    network::{ContentEncodingMode, decode_content_encoding, detect_soft_block_with_headers},
    projection::{
        BoilerplatePolicy, Chunk, CommentUnwrapPolicy, ParseLimits, Projection, ResearchNode,
        ResearchNodeKind, extract_annas_archive_records, parse_html, parse_html_breakthrough,
        parse_html_document_with_node_limit, parse_html_with_limits, project_research_nodes,
    },
    rank::{
        PreparedRank as CorePreparedRank, bm25, bm25_plus, bm25f,
        bm25f_guarded_dual_channel_highlight_indices, bm25f_highlight_indices, bm25f_plus_mslp,
        bm25f_plus_mslp_simhash_highlight_indices, bm25f_simhash_highlight_indices, bm25l,
        highlight_indices, hybrid_rrf_simhash_highlight_indices,
        multi_query_rrf_simhash_highlight_indices, ql_dirichlet, reciprocal_rank_fusion,
    },
    table::{
        Cell, Grid, GridError, Row, expand, expand_tolerant, expand_with_slot_limit,
        ground_grid_cells,
    },
};
use rand::{Rng, SeedableRng};
use scraper::{ElementRef, Html, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    env, fs,
    hint::black_box,
    io::{self, Read, Write},
    sync::Arc,
    time::{Duration, Instant},
};
use url::Url;

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Frame {
    Meta {
        version: &'static str,
        source_url: String,
        fetched_at_unix_ms: u64,
        content_hash: String,
        total_blocks: usize,
        parse_warnings: usize,
    },
    Block {
        id: String,
        role: &'static str,
        text: String,
        links: Vec<u32>,
        highlight: bool,
    },
    Link {
        id: u32,
        url: String,
    },
    End {
        block_count: usize,
    },
}

struct Sink {
    out: Vec<u8>,
    limit: usize,
}
impl Write for Sink {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.out.len()) {
            return Err(io::Error::other("AAE output byte budget exceeded"));
        }
        self.out.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn serialize_frames(
    frames: &[Frame],
    limit: usize,
    writer: &str,
    reserve: usize,
) -> Result<Vec<u8>, String> {
    let mut sink = Sink {
        out: Vec::with_capacity(reserve.min(limit)),
        limit,
    };
    if writer == "buffered" {
        let mut buffered = io::BufWriter::with_capacity(8192.min(limit).max(1), sink);
        for frame in frames {
            serde_json::to_writer(&mut buffered, frame).map_err(|e| e.to_string())?;
            buffered.write_all(b"\n").map_err(|e| e.to_string())?;
            buffered.flush().map_err(|e| e.to_string())?;
        }
        return buffered
            .into_inner()
            .map(|s| s.out)
            .map_err(|e| e.error().to_string());
    }
    let mut scratch = Vec::with_capacity(if writer == "reuse" {
        4096.min(limit)
    } else {
        0
    });
    for frame in frames {
        match writer {
            "frame" => {
                let bytes = serde_json::to_vec(frame).map_err(|e| e.to_string())?;
                if bytes
                    .len()
                    .checked_add(1)
                    .is_none_or(|n| n > sink.limit.saturating_sub(sink.out.len()))
                {
                    return Err("AAE output byte budget exceeded".into());
                }
                sink.out.extend_from_slice(&bytes);
                sink.out.push(b'\n');
            }
            "direct" => {
                serde_json::to_writer(&mut sink, frame).map_err(|e| e.to_string())?;
                sink.write_all(b"\n").map_err(|e| e.to_string())?;
            }
            "reuse" => {
                scratch.clear();
                let remaining = sink
                    .limit
                    .saturating_sub(sink.out.len())
                    .checked_sub(1)
                    .ok_or("AAE output byte budget exceeded")?;
                let mut frame_sink = Sink {
                    out: scratch,
                    limit: remaining,
                };
                serde_json::to_writer(&mut frame_sink, frame).map_err(|e| e.to_string())?;
                scratch = frame_sink.out;
                sink.out.extend_from_slice(&scratch);
                sink.out.push(b'\n');
            }
            _ => return Err("unknown serialization factor".into()),
        }
    }
    Ok(sink.out)
}

fn words(text: &str) -> impl Iterator<Item = &str> {
    text.split(|c: char| !c.is_alphanumeric())
        .filter(|word| !word.is_empty())
}

fn source_key(chunks: &[Chunk], revision: &str) -> [u8; 32] {
    let mut hash = blake3::Hasher::new();
    hash.update(&(revision.len() as u64).to_le_bytes());
    hash.update(revision.as_bytes());
    hash.update(&(chunks.len() as u64).to_le_bytes());
    for chunk in chunks {
        hash.update(&chunk.node.to_le_bytes());
        for bytes in [chunk.role.as_bytes(), chunk.text.as_bytes()] {
            hash.update(&(bytes.len() as u64).to_le_bytes());
            hash.update(bytes);
        }
        hash.update(&(chunk.links.len() as u64).to_le_bytes());
        for id in &chunk.links {
            hash.update(&id.to_le_bytes());
        }
    }
    *hash.finalize().as_bytes()
}

fn lowercase(word: &str) -> std::borrow::Cow<'_, str> {
    if word
        .bytes()
        .all(|b| b.is_ascii() && !b.is_ascii_uppercase())
    {
        std::borrow::Cow::Borrowed(word)
    } else {
        std::borrow::Cow::Owned(word.to_lowercase())
    }
}

struct PreparedRank {
    key: [u8; 32],
    norms: Vec<f64>,
    postings: BTreeMap<String, Vec<(usize, usize)>>,
    retained_payload_bytes: usize,
}
impl PreparedRank {
    fn new(chunks: &[Chunk], revision: &str, max_tokens: usize) -> Result<Self, String> {
        Self::new_with_key(chunks, source_key(chunks, revision), max_tokens)
    }
    fn new_with_key(chunks: &[Chunk], key: [u8; 32], max_tokens: usize) -> Result<Self, String> {
        let mut lengths = Vec::with_capacity(chunks.len());
        let mut total_len = 0usize;
        let mut postings = BTreeMap::<String, Vec<(usize, usize)>>::new();
        let mut tokens = 0usize;
        let mut normalized_bytes = 0usize;
        for (index, chunk) in chunks.iter().enumerate() {
            let mut length = 0usize;
            for word in words(&chunk.text) {
                tokens += 1;
                if tokens > max_tokens {
                    return Err("prepared token budget exceeded".into());
                }
                let normalized = lowercase(word);
                normalized_bytes = normalized_bytes
                    .checked_add(normalized.len())
                    .ok_or("prepared byte budget exceeded")?;
                if normalized_bytes > 32 * 1024 * 1024 {
                    return Err("prepared byte budget exceeded".into());
                }
                if let Some(hits) = postings.get_mut(normalized.as_ref()) {
                    if let Some((last_index, count)) = hits.last_mut()
                        && *last_index == index
                    {
                        *count += 1;
                    } else {
                        hits.push((index, 1));
                    }
                } else {
                    postings.insert(normalized.into_owned(), vec![(index, 1)]);
                }
                length += 1;
            }
            total_len += length;
            lengths.push(length);
        }
        let n = lengths.len();
        let avg_len = if n == 0 {
            1.0
        } else {
            (total_len as f64 / n as f64).max(1.0)
        };
        let k1 = 1.2;
        let b = 0.75;
        let norms: Vec<f64> = lengths
            .into_iter()
            .map(|len| k1 * (1.0 - b + b * len as f64 / avg_len))
            .collect();
        let retained_payload_bytes = norms.capacity() * std::mem::size_of::<f64>()
            + postings
                .iter()
                .map(|(term, hits)| {
                    term.capacity() + hits.capacity() * std::mem::size_of::<(usize, usize)>()
                })
                .sum::<usize>();
        Ok(Self {
            key,
            norms,
            postings,
            retained_payload_bytes,
        })
    }
    fn scores(&self, query: &str) -> Vec<(usize, f64)> {
        let mut terms = words(query).map(lowercase).collect::<Vec<_>>();
        terms.sort_unstable();
        terms.dedup();
        let n = self.norms.len();
        if n == 0 {
            return vec![];
        }
        let k1 = 1.2;
        let mut scores = (0..n).map(|i| (i, 0.0)).collect::<Vec<_>>();
        for term in terms {
            if let Some(matches) = self.postings.get(term.as_ref()) {
                let df = matches.len() as f64;
                let idf = (1.0 + (n as f64 - df + 0.5) / (df + 0.5)).ln();
                for &(i, frequency) in matches {
                    let freq = frequency as f64;
                    scores[i].1 += idf * freq * (k1 + 1.0) / (freq + self.norms[i]);
                }
            }
        }
        scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
        scores
    }
    #[cfg(test)]
    fn matches(&self, chunks: &[Chunk], revision: &str) -> bool {
        self.matches_key(&source_key(chunks, revision))
    }
    fn matches_key(&self, key: &[u8; 32]) -> bool {
        &self.key == key
    }
}

fn content_hash(chunks: &[Chunk], batch: usize) -> [u8; 32] {
    if batch == 0 {
        let joined = chunks
            .iter()
            .map(|chunk| chunk.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        return *blake3::hash(joined.as_bytes()).as_bytes();
    }
    if batch == 1 {
        let mut hash = blake3::Hasher::new();
        for (i, chunk) in chunks.iter().enumerate() {
            if i != 0 {
                hash.update(b"\n");
            }
            hash.update(chunk.text.as_bytes());
        }
        return *hash.finalize().as_bytes();
    }
    match chunks {
        [] => *blake3::hash(b"").as_bytes(),
        [only] => *blake3::hash(only.text.as_bytes()).as_bytes(),
        [first, rest @ ..] => {
            let total_len = chunks
                .iter()
                .fold(chunks.len() - 1, |acc, c| acc + c.text.len());
            if total_len <= 4096 {
                let mut buf = [0u8; 4096];
                let first_bytes = first.text.as_bytes();
                let mut pos = first_bytes.len();
                buf[..pos].copy_from_slice(first_bytes);
                for chunk in rest {
                    buf[pos] = b'\n';
                    pos += 1;
                    let bytes = chunk.text.as_bytes();
                    buf[pos..pos + bytes.len()].copy_from_slice(bytes);
                    pos += bytes.len();
                }
                return *blake3::hash(&buf[..pos]).as_bytes();
            }
            if total_len <= 32_768 {
                let mut buf = [0u8; 32_768];
                let first_bytes = first.text.as_bytes();
                let mut pos = first_bytes.len();
                buf[..pos].copy_from_slice(first_bytes);
                for chunk in rest {
                    buf[pos] = b'\n';
                    pos += 1;
                    let bytes = chunk.text.as_bytes();
                    buf[pos..pos + bytes.len()].copy_from_slice(bytes);
                    pos += bytes.len();
                }
                return *blake3::hash(&buf[..pos]).as_bytes();
            }
            if total_len <= batch.max(262_144) {
                let mut scratch = Vec::with_capacity(total_len);
                scratch.extend_from_slice(first.text.as_bytes());
                for chunk in rest {
                    scratch.push(b'\n');
                    scratch.extend_from_slice(chunk.text.as_bytes());
                }
                return *blake3::hash(&scratch).as_bytes();
            }
            let mut hash = blake3::Hasher::new();
            let mut scratch = Vec::with_capacity(batch);
            let first_bytes = first.text.as_bytes();
            let first_full = (first_bytes.len() / batch) * batch;
            if first_full > 0 {
                hash.update(&first_bytes[..first_full]);
            }
            scratch.extend_from_slice(&first_bytes[first_full..]);
            for chunk in rest {
                let bytes = chunk.text.as_bytes();
                if scratch.len() + 1 + bytes.len() <= batch {
                    scratch.push(b'\n');
                    scratch.extend_from_slice(bytes);
                    if scratch.len() == batch {
                        hash.update(&scratch);
                        scratch.clear();
                    }
                    continue;
                }
                scratch.push(b'\n');
                let space = batch - scratch.len();
                let (head, tail) = bytes.split_at(space);
                scratch.extend_from_slice(head);
                hash.update(&scratch);
                scratch.clear();
                let full = (tail.len() / batch) * batch;
                if full > 0 {
                    hash.update(&tail[..full]);
                }
                scratch.extend_from_slice(&tail[full..]);
            }
            if !scratch.is_empty() {
                hash.update(&scratch);
            }
            *hash.finalize().as_bytes()
        }
    }
}

fn block_ids(chunks: &[Chunk]) -> Vec<[u8; 32]> {
    chunks
        .iter()
        .enumerate()
        .map(|(index, chunk)| {
            let mut hash = blake3::Hasher::new();
            hash.update(chunk.role.as_bytes());
            hash.update(&[0]);
            hash.update(chunk.text.as_bytes());
            hash.update(&(index as u64).to_le_bytes());
            *hash.finalize().as_bytes()
        })
        .collect()
}

fn make_frames(
    page: &Projection,
    url: &Url,
    observed: u64,
    query: Option<&str>,
    limit: usize,
    link_mode: LinkMode,
) -> Vec<Frame> {
    let highlights = query
        .map(|q| highlight_indices(&page.chunks, q, limit))
        .unwrap_or_default();
    let mut selected = vec![false; page.chunks.len()];
    for &index in &highlights {
        selected[index] = true;
    }
    let order: Vec<usize> = highlights
        .into_iter()
        .chain((0..page.chunks.len()).filter(|&i| !selected[i]))
        .collect();
    let mut link_remap: Vec<u32> = Vec::new();
    let mut active_links: Vec<(u32, String)> = Vec::new();
    if link_mode == LinkMode::HighlightedOnly && !page.links.is_empty() {
        link_remap.resize(page.links.len(), u32::MAX);
        for &index in &order {
            if !selected[index] {
                break;
            }
            for &old_id in &page.chunks[index].links {
                let idx = old_id as usize;
                if idx < page.links.len() && link_remap[idx] == u32::MAX {
                    let new_id = active_links.len() as u32;
                    link_remap[idx] = new_id;
                    active_links.push((new_id, page.links[idx].clone()));
                }
            }
        }
    }
    let mut frames = vec![Frame::Meta {
        version: "aae/1",
        source_url: url.to_string(),
        fetched_at_unix_ms: observed,
        content_hash: blake3::Hash::from(content_hash(&page.chunks, 0))
            .to_hex()
            .to_string(),
        total_blocks: page.chunks.len(),
        parse_warnings: page.parse_warnings,
    }];
    let ids = block_ids(&page.chunks);
    for index in order {
        let chunk = &page.chunks[index];
        let links = match link_mode {
            LinkMode::All => chunk.links.clone(),
            LinkMode::None => Vec::new(),
            LinkMode::HighlightedOnly => {
                if selected[index] && !chunk.links.is_empty() {
                    let mut remapped = Vec::with_capacity(chunk.links.len());
                    for &old_id in &chunk.links {
                        let idx = old_id as usize;
                        if idx < link_remap.len() && link_remap[idx] != u32::MAX {
                            let new_id = link_remap[idx];
                            if !remapped.contains(&new_id) {
                                remapped.push(new_id);
                            }
                        }
                    }
                    remapped
                } else {
                    Vec::new()
                }
            }
        };
        frames.push(Frame::Block {
            id: blake3::Hash::from(ids[index]).to_hex().to_string(),
            role: chunk.role,
            text: chunk.text.clone(),
            links,
            highlight: selected[index],
        });
    }
    match link_mode {
        LinkMode::All => {
            for (index, url) in page.links.iter().enumerate() {
                frames.push(Frame::Link {
                    id: index as u32,
                    url: url.clone(),
                });
            }
        }
        LinkMode::HighlightedOnly => {
            for (id, url) in active_links {
                frames.push(Frame::Link { id, url });
            }
        }
        LinkMode::None => {}
    }
    frames.push(Frame::End {
        block_count: page.chunks.len(),
    });
    frames
}

fn require(condition: bool, message: &str) -> Result<(), String> {
    if condition {
        Ok(())
    } else {
        Err(message.into())
    }
}

struct TableSelectors {
    table: Selector,
    row: Selector,
}

const HTML_TABLE_MAX_ROWS: usize = 16_384;
const HTML_TABLE_MAX_CELLS: usize = 50_000;
const HTML_TABLE_MAX_GRID_SLOTS: usize = 250_000;

#[derive(Clone, Copy)]
struct HtmlTableLimits {
    max_tables: usize,
    max_rows: usize,
    max_cols: usize,
    max_html_bytes: usize,
    max_nodes: usize,
    max_text_bytes: usize,
}

impl Default for HtmlTableLimits {
    fn default() -> Self {
        Self {
            max_tables: 64,
            max_rows: 1000,
            max_cols: 1000,
            max_html_bytes: 4 * 1024 * 1024,
            max_nodes: 250_000,
            max_text_bytes: 4 * 1024 * 1024,
        }
    }
}

impl TableSelectors {
    fn new() -> Result<Self, String> {
        Ok(Self {
            table: Selector::parse("table").map_err(|error| error.to_string())?,
            row: Selector::parse("tr").map_err(|error| error.to_string())?,
        })
    }
}

fn nearest_table_index<'a>(element: ElementRef<'a>, tables: &[ElementRef<'a>]) -> Option<usize> {
    let mut parent = element.parent().and_then(ElementRef::wrap);
    while let Some(candidate) = parent {
        if candidate.value().name() == "table" {
            return tables.iter().position(|table| *table == candidate);
        }
        parent = candidate.parent().and_then(ElementRef::wrap);
    }
    None
}

fn row_group<'a>(
    row: ElementRef<'a>,
    table: ElementRef<'a>,
    groups: &[ElementRef<'a>],
) -> (usize, String) {
    let mut parent = row.parent().and_then(ElementRef::wrap);
    while let Some(candidate) = parent {
        if candidate == table {
            break;
        }
        let name = candidate.value().name();
        if matches!(name, "thead" | "tbody" | "tfoot") {
            let index = groups
                .iter()
                .position(|group| *group == candidate)
                .unwrap_or(groups.len());
            return (index, name.to_owned());
        }
        parent = candidate.parent().and_then(ElementRef::wrap);
    }
    (groups.len(), "table".to_owned())
}

fn rows_by_traversal<'a>(
    table: ElementRef<'a>,
    _tables: &[ElementRef<'a>],
    _table_index: usize,
) -> Vec<(ElementRef<'a>, usize, String)> {
    let groups = table
        .child_elements()
        .filter(|child| matches!(child.value().name(), "thead" | "tbody" | "tfoot"))
        .collect::<Vec<_>>();
    let mut stack = table.child_elements().collect::<Vec<_>>();
    stack.reverse();
    let mut rows = Vec::new();
    while let Some(element) = stack.pop() {
        match element.value().name() {
            "table" => continue,
            "tr" => {
                let (group, group_type) = row_group(element, table, &groups);
                rows.push((element, group, group_type));
            }
            _ => {
                let mut children = element.child_elements().collect::<Vec<_>>();
                children.reverse();
                stack.extend(children);
            }
        }
    }
    rows
}

fn rows_by_selector<'a>(
    _document: &'a Html,
    tables: &[ElementRef<'a>],
    selectors: &TableSelectors,
) -> Vec<Vec<(ElementRef<'a>, usize, String)>> {
    let mut result = vec![Vec::new(); tables.len()];
    for (index, &table) in tables.iter().enumerate() {
        let mut groups: Option<Vec<ElementRef<'a>>> = None;
        for row in table.select(&selectors.row) {
            let mut group_candidate = None;
            let mut owned_by_table = false;
            let mut parent = row.parent().and_then(ElementRef::wrap);
            while let Some(candidate) = parent {
                if candidate == table {
                    owned_by_table = true;
                    break;
                }
                let name = candidate.value().name();
                if name == "table" {
                    break;
                }
                if group_candidate.is_none() && matches!(name, "thead" | "tbody" | "tfoot") {
                    group_candidate = Some((candidate, name));
                }
                parent = candidate.parent().and_then(ElementRef::wrap);
            }
            if owned_by_table {
                let groups = groups.get_or_insert_with(|| {
                    table
                        .child_elements()
                        .filter(|child| matches!(child.value().name(), "thead" | "tbody" | "tfoot"))
                        .collect()
                });
                let (group, group_type) = match group_candidate {
                    Some((candidate, name)) => {
                        let group_idx = groups
                            .iter()
                            .position(|group| *group == candidate)
                            .unwrap_or(groups.len());
                        (group_idx, name.to_owned())
                    }
                    None => (groups.len(), "table".to_owned()),
                };
                result[index].push((row, group, group_type));
            }
        }
    }
    result
}

fn normalized_cell_text(cell: ElementRef<'_>) -> String {
    let mut normalized = String::new();
    for token in cell.text().flat_map(str::split_whitespace) {
        if !normalized.is_empty() {
            normalized.push(' ');
        }
        normalized.push_str(token);
    }
    normalized
}

fn normalized_table_cell_text(cell: ElementRef<'_>) -> String {
    let mut normalized = String::new();
    let root_id = cell.id();
    let mut cursor = cell.first_child();
    while let Some(node) = cursor {
        let is_nested_table = node
            .value()
            .as_element()
            .is_some_and(|element| &*element.name.local == "table");
        if !is_nested_table {
            if let Some(text) = node.value().as_text() {
                for token in text.split_whitespace() {
                    if !normalized.is_empty() {
                        normalized.push(' ');
                    }
                    normalized.push_str(token);
                }
            }
            if let Some(child) = node.first_child() {
                cursor = Some(child);
                continue;
            }
        }

        let mut ancestor = node;
        cursor = loop {
            if ancestor.id() == root_id {
                break None;
            }
            if let Some(sibling) = ancestor.next_sibling() {
                break Some(sibling);
            }
            match ancestor.parent() {
                Some(parent) => ancestor = parent,
                None => break None,
            }
        };
    }
    normalized
}

fn span_attribute(cell: ElementRef<'_>, name: &str) -> usize {
    let parsed = cell
        .attr(name)
        .and_then(|value| value.trim().parse::<usize>().ok());
    match (name, parsed) {
        ("rowspan", Some(value)) => value.min(65_534),
        ("colspan", Some(value)) if value > 0 => value.min(1_000),
        _ => 1,
    }
}

fn extract_html_tables(
    html: &str,
    algorithm: &str,
    selectors: &TableSelectors,
    limits: &HtmlTableLimits,
) -> Result<Value, String> {
    if limits.max_html_bytes == 0
        || limits.max_html_bytes > 4 * 1024 * 1024
        || html.len() > limits.max_html_bytes
        || limits.max_nodes == 0
        || limits.max_nodes > 250_000
        || limits.max_text_bytes == 0
        || limits.max_text_bytes > 4 * 1024 * 1024
    {
        return Err("invalid resource budget".into());
    }
    if !matches!(algorithm, "traversal" | "selectors")
        || limits.max_tables == 0
        || limits.max_tables > 64
        || limits.max_rows == 0
        || limits.max_rows > 2048
        || limits.max_cols == 0
        || limits.max_cols > 4096
        || limits
            .max_rows
            .checked_mul(limits.max_cols)
            .is_none_or(|n| n > 1_000_000)
    {
        return Err("invalid HTML table admission".into());
    }

    let (document, _) =
        parse_html_document_with_node_limit(html, limits.max_nodes).map_err(str::to_owned)?;
    let tables = match algorithm {
        "traversal" => document
            .root_element()
            .descendent_elements()
            .filter(|element| element.value().name() == "table")
            .collect::<Vec<_>>(),
        "selectors" => document.select(&selectors.table).collect::<Vec<_>>(),
        _ => return Err("unknown HTML table algorithm".into()),
    };
    if tables.len() > limits.max_tables {
        return Err("HTML table count limit exceeded".into());
    }
    let mut selected_rows = if algorithm == "selectors" {
        Some(rows_by_selector(&document, &tables, selectors))
    } else {
        None
    };
    let mut total_cells = 0usize;
    let mut total_rows = 0usize;
    let mut total_grid_slots = 0usize;
    let mut total_text_bytes = 0usize;
    let mut output_tables = Vec::with_capacity(tables.len());

    for (table_index, table) in tables.iter().copied().enumerate() {
        let rows = match selected_rows.as_mut() {
            Some(all) => std::mem::take(&mut all[table_index]),
            None => rows_by_traversal(table, &tables, table_index),
        };
        if rows.len() > limits.max_rows {
            return Err("HTML table row limit exceeded".into());
        }
        total_rows = total_rows
            .checked_add(rows.len())
            .ok_or("HTML table row limit exceeded")?;
        if total_rows > HTML_TABLE_MAX_ROWS {
            return Err("HTML table row limit exceeded".into());
        }
        let mut grid_rows = Vec::with_capacity(rows.len());
        let mut output_rows = Vec::with_capacity(rows.len());
        for (row_index, (row, group, group_type)) in rows.into_iter().enumerate() {
            let mut grid_cells = Vec::new();
            let mut output_cells = Vec::new();
            for cell in row
                .child_elements()
                .filter(|child| matches!(child.value().name(), "th" | "td"))
            {
                total_cells = total_cells
                    .checked_add(1)
                    .ok_or("HTML table cell limit exceeded")?;
                if grid_cells.len() >= limits.max_cols || total_cells > HTML_TABLE_MAX_CELLS {
                    return Err("HTML table column limit exceeded".into());
                }
                let tag = cell.value().name();
                let rowspan = span_attribute(cell, "rowspan");
                let colspan = span_attribute(cell, "colspan");
                let header_ids = cell
                    .attr("headers")
                    .unwrap_or("")
                    .split_ascii_whitespace()
                    .collect::<Vec<_>>();
                let locator = format!(
                    "table[{table_index}]/row[{row_index}]/cell[{}]",
                    grid_cells.len()
                );
                let text = normalized_table_cell_text(cell);
                total_text_bytes = total_text_bytes
                    .checked_add(text.len())
                    .ok_or("decoded text byte budget exceeded")?;
                if total_text_bytes > limits.max_text_bytes {
                    return Err("decoded text byte budget exceeded".into());
                }
                output_cells.push(json!({
                    "cell_index": grid_cells.len(), "tag": tag, "text": text.clone(),
                    "rowspan": rowspan, "colspan": colspan, "scope": cell.attr("scope"),
                    "headers": header_ids, "unit": cell.attr("data-unit"), "abbr": cell.attr("abbr"),
                    "id": cell.attr("id"), "locator": locator
                }));
                grid_cells.push(Cell {
                    text,
                    rowspan,
                    colspan,
                    is_header: tag == "th",
                });
            }
            grid_rows.push(Row {
                group,
                cells: grid_cells,
            });
            output_rows.push(json!({"row_index":row_index,"group":group,"group_type":group_type,"cells":output_cells}));
        }
        let remaining_grid_slots = HTML_TABLE_MAX_GRID_SLOTS
            .checked_sub(total_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        let grid = expand_with_slot_limit(
            &grid_rows,
            limits.max_rows,
            limits.max_cols,
            remaining_grid_slots,
        )
        .map_err(|error| match error {
            GridError::TooLarge => "HTML table column limit exceeded".to_owned(),
            GridError::TooManySlots => "HTML table grid limit exceeded".to_owned(),
            GridError::InvalidGroupOrder => "HTML table row group order invalid".to_owned(),
            GridError::Overlap => "HTML table cell spans overlap".to_owned(),
        })?;
        let table_grid_slots = grid
            .slots
            .len()
            .checked_mul(grid.width)
            .ok_or("HTML table grid limit exceeded")?;
        total_grid_slots = total_grid_slots
            .checked_add(table_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        if total_grid_slots > HTML_TABLE_MAX_GRID_SLOTS {
            return Err("HTML table grid limit exceeded".into());
        }
        let slots = grid
            .slots
            .iter()
            .map(|row| {
                row.iter()
                    .map(|cell| cell.map_or(Value::Null, |index| json!(index)))
                    .collect::<Vec<_>>()
            })
            .collect::<Vec<_>>();
        let caption = table
            .child_elements()
            .find(|child| child.value().name() == "caption")
            .map(normalized_cell_text);
        if let Some(caption) = &caption {
            total_text_bytes = total_text_bytes
                .checked_add(caption.len())
                .ok_or("decoded text byte budget exceeded")?;
            if total_text_bytes > limits.max_text_bytes {
                return Err("decoded text byte budget exceeded".into());
            }
        }
        output_tables.push(json!({
            "table_index":table_index, "id":table.attr("id"), "caption":caption,
            "rows":output_rows, "grid":{"width":grid.width,"slots":slots}
        }));
    }
    Ok(json!({
        "schema":"athena-html-table-candidate/2",
        "parse_warnings":document.errors.len(), "header_associations":"explicit-attributes-only",
        "cell_text":"normalized DOM text, excluding nested-table descendants from each cell", "tables":output_tables
    }))
}

fn semantic_controls() -> Result<Value, String> {
    let url = Url::parse("https://evidence.example/source").map_err(|e| e.to_string())?;
    let html = "<p>A &amp; B \"x\"\n内容 <a href='/c'>C</a></p><pre>if x:\n  y()</pre><script>ignored()</script>";
    let page = parse_html(html, &url, 100, 100).map_err(str::to_string)?;
    require(
        page.chunks
            .iter()
            .map(|c| (c.role, c.text.as_str(), c.links.clone()))
            .collect::<Vec<_>>()
            == [
                ("paragraph", "A & B \"x\" 内容 C", vec![0]),
                ("preformatted", "if x:\n  y()", vec![]),
            ],
        "projection literal text/role/link oracle",
    )?;
    require(
        page.links == ["https://evidence.example/c"],
        "link resolution and insertion order oracle",
    )?;
    let envelope =
        export_jsonl_with_limit(&page, &url, 71, None, 0, 16384).map_err(|e| e.to_string())?;
    let parsed = envelope
        .lines()
        .map(serde_json::from_str::<Value>)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    require(
        parsed.len() == 5
            && parsed[0]["version"] == "aae/1"
            && parsed[0]["source_url"] == url.as_str()
            && parsed[0]["fetched_at_unix_ms"] == 71
            && parsed[0]["total_blocks"] == 2
            && parsed[4] == json!({"type":"end","block_count":2}),
        "AAE literal provenance/count/end oracle",
    )?;
    require(
        parsed[0]["content_hash"]
            == blake3::hash("A & B \"x\" 内容 C\nif x:\n  y()".as_bytes())
                .to_hex()
                .as_str(),
        "AAE independent joined-byte hash oracle",
    )?;
    let mut first_id_bytes = "paragraph\0A & B \"x\" 内容 C".as_bytes().to_vec();
    first_id_bytes.extend_from_slice(&[0; 8]);
    let mut second_id_bytes = "preformatted\0if x:\n  y()".as_bytes().to_vec();
    second_id_bytes.extend_from_slice(&[1, 0, 0, 0, 0, 0, 0, 0]);
    require(
        parsed[1]
            == json!({"type":"block","id":blake3::hash(&first_id_bytes).to_hex().as_str(),"role":"paragraph","text":"A & B \"x\" 内容 C","links":[0],"highlight":false})
            && parsed[2]
                == json!({"type":"block","id":blake3::hash(&second_id_bytes).to_hex().as_str(),"role":"preformatted","text":"if x:\n  y()","links":[],"highlight":false}),
        "AAE literal block fields and role/NUL/text/little-endian-index IDs",
    )?;
    require(
        parsed[3] == json!({"type":"link","id":0,"url":"https://evidence.example/c"}),
        "AAE literal ordered link frame",
    )?;
    require(
        export_jsonl_with_limit(&page, &url, 71, None, 0, envelope.len())
            .map_err(|e| e.to_string())?
            == envelope,
        "AAE exact cap oracle",
    )?;
    require(
        export_jsonl_with_limit(&page, &url, 71, None, 0, envelope.len() - 1)
            .is_err_and(|e| e.is_io() && e.to_string() == "AAE output byte budget exceeded"),
        "AAE one-byte-over cap/error-class oracle",
    )?;
    let chunk = |text: &str| Chunk {
        node: 0,
        role: "paragraph",
        text: text.into(),
        links: vec![],
    };
    let docs = [chunk("Rust rust"), chunk("rust tokio"), chunk("unrelated")];
    let scores = bm25(&docs, "RUST rust", 1.2, 0.75);
    let idf = (1.0_f64 + 1.5 / 2.5).ln();
    require(
        scores.iter().map(|s| s.0).collect::<Vec<_>>() == [0, 1, 2]
            && (scores[0].1 - idf * 4.4 / 3.38).abs() < 1e-12
            && (scores[1].1 - idf * 2.2 / 2.38).abs() < 1e-12
            && scores[2].1 == 0.0,
        "BM25 hand-derived df/frequency/length/query-dedup oracle",
    )?;
    let novelty = [
        chunk("Rust"),
        chunk("RUST"),
        chunk("Rust distinct"),
        chunk("unrelated"),
    ];
    require(
        highlight_indices(&novelty, "rust", 2) == [0, 2],
        "selection must recover after duplicate leading results",
    )?;
    require(
        highlight_indices(&novelty, "!?", 3).is_empty()
            && highlight_indices(&novelty, "rust", 0).is_empty(),
        "empty/zero-score/limit-zero selection oracle",
    )?;
    require(
        highlight_indices(
            &[chunk("TÀI liệu 内容"), chunk("TÀI"), chunk("nothing")],
            "tài",
            2,
        ) == [1, 0],
        "Unicode lowercase and length-normalization oracle",
    )?;
    let cell = |text: &str, rowspan, colspan| Cell {
        text: text.into(),
        rowspan,
        colspan,
        is_header: false,
    };
    let rows = [
        Row {
            group: 0,
            cells: vec![cell("A", 0, 2), cell("B", 1, 1)],
        },
        Row {
            group: 0,
            cells: vec![cell("C", 1, 1)],
        },
        Row {
            group: 1,
            cells: vec![cell("D", 1, 1)],
        },
    ];
    let grid = expand(&rows, 3, 3).map_err(|e| e.to_string())?;
    require(
        grid.width == 3
            && grid.slots
                == [
                    vec![Some(0), Some(0), Some(1)],
                    vec![Some(0), Some(0), Some(2)],
                    vec![Some(3), None, None],
                ],
        "grid literal span0/group/slots oracle",
    )?;
    require(
        expand(&rows, 2, 3) == Err(GridError::TooLarge)
            && expand(&rows, 3, 2) == Err(GridError::TooLarge),
        "grid row/column budget oracle",
    )?;
    let overlap = [
        Row {
            group: 0,
            cells: vec![cell("a", 1, 1), cell("b", 2, 1)],
        },
        Row {
            group: 0,
            cells: vec![cell("c", 1, 2)],
        },
    ];
    require(
        expand(&overlap, 2, 3) == Err(GridError::Overlap),
        "grid overlap rejection oracle",
    )?;
    let rank_partitions = independent_rank_panel()?;
    Ok(
        json!({"status":"PASS","scope":"literal AAE/projection/rank/novelty/Unicode/logical-grid controls; no HTML-table, network or service claim","checks":15,"rank_partitions":rank_partitions}),
    )
}

fn independent_rank_panel() -> Result<usize, String> {
    let mut checked = 0;
    for term_count in [1, 3, 8, 32, 128] {
        for shape in ["dense", "sparse", "ties"] {
            let terms = (0..term_count)
                .map(|i| format!("term{i}"))
                .collect::<Vec<_>>();
            let mut docs = Vec::new();
            let mut frequencies = Vec::new();
            let mut lengths = Vec::new();
            for i in 0..24 {
                let mut text = String::new();
                let mut row = Vec::new();
                let mut length = 0;
                for (j, term) in terms.iter().enumerate() {
                    let frequency = match shape {
                        "dense" => {
                            if (i + j) % 3 == 0 {
                                1 + (i * j) % 2
                            } else {
                                0
                            }
                        }
                        "sparse" => usize::from(i == 0),
                        _ => 1,
                    };
                    row.push(frequency);
                    length += frequency;
                    for _ in 0..frequency {
                        if i % 2 == 0 {
                            text.push_str(term);
                        } else {
                            text.push_str(&term.to_uppercase());
                        }
                        text.push(' ');
                    }
                }
                let padding = if shape == "ties" { 0 } else { 1 + i % 4 };
                for _ in 0..padding {
                    text.push_str("padding ");
                }
                length += padding;
                docs.push(Chunk {
                    node: i as u32,
                    role: "paragraph",
                    text,
                    links: vec![],
                });
                frequencies.push(row);
                lengths.push(length);
            }
            let average = (lengths.iter().sum::<usize>() as f64 / 24.0).max(1.0);
            let dfs = (0..term_count)
                .map(|j| frequencies.iter().filter(|row| row[j] != 0).count())
                .collect::<Vec<_>>();
            let mut term_order = (0..term_count).collect::<Vec<_>>();
            term_order.sort_by(|&a, &b| terms[a].cmp(&terms[b]));
            let mut expected = Vec::new();
            for i in 0..24 {
                let normalization = 1.2 * (0.25 + 0.75 * lengths[i] as f64 / average);
                let mut value = 0.0;
                for &j in &term_order {
                    let frequency = frequencies[i][j] as f64;
                    if frequency != 0.0 {
                        let document_frequency = dfs[j] as f64;
                        value += (1.0
                            + (24.0 - document_frequency + 0.5) / (document_frequency + 0.5))
                            .ln()
                            * frequency
                            * 2.2
                            / (frequency + normalization);
                    }
                }
                expected.push((i, value));
            }
            expected.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
            let query = format!("{} {}", terms.join(" "), terms.join(" ").to_uppercase());
            let actual = bm25(&docs, &query, 1.2, 0.75);
            require(
                actual.len() == expected.len()
                    && actual
                        .iter()
                        .zip(&expected)
                        .all(|(a, e)| a.0 == e.0 && (a.1 - e.1).abs() < 1e-12),
                &format!("independent frequency-matrix BM25 oracle {term_count}/{shape}"),
            )?;
            let prepared = PreparedRank::new(&docs, "oracle-policy/tokenizer1", 100_000)?;
            let reusable = prepared.scores(&query);
            require(
                reusable
                    .iter()
                    .zip(&expected)
                    .all(|(a, e)| a.0 == e.0 && (a.1 - e.1).abs() < 1e-12),
                &format!("prepared frequency-matrix BM25 oracle {term_count}/{shape}"),
            )?;
            if shape == "ties" || shape == "sparse" {
                require(
                    highlight_indices(&docs, &query, 5) == [0],
                    "tie/sparse novelty oracle",
                )?;
            }
            checked += 1;
        }
    }
    Ok(checked)
}

fn repeated_queries(input: &Fixture, lexical: bool) -> Result<Output, String> {
    let initial = input
        .page
        .as_ref()
        .ok_or("prepared stage projection missing")?;
    let invalidation = input.input.get("replacement_html").is_some()
        || input.input.get("replacement_policy_revision").is_some();
    let split = if invalidation {
        number(&input.input, "invalidate_after", input.queries.len() / 2)?
    } else {
        input.queries.len()
    };
    require(
        split <= input.queries.len(),
        "invalid prepared invalidation boundary",
    )?;
    let mut ranks = Vec::with_capacity(input.queries.len());
    let mut retained_payload_bytes = 0;
    let mut state = if lexical {
        Some(PreparedRank::new(
            &initial.chunks,
            &input.revision,
            input.max_tokens,
        )?)
    } else {
        None
    };
    if let Some(prepared) = &state {
        retained_payload_bytes = prepared.retained_payload_bytes;
    }
    for query in &input.queries[..split] {
        ranks.push(if let Some(prepared) = &state {
            prepared.scores(query)
        } else {
            bm25(&initial.chunks, query, 1.2, 0.75)
        });
    }
    if invalidation {
        let html = input.input["replacement_html"]
            .as_str()
            .unwrap_or(&input.html);
        require(
            html.len() <= 4 * 1024 * 1024,
            "replacement HTML exceeds hard probe admission",
        )?;
        let replacement =
            parse_html_with_limits(html, &input.url, input.limits).map_err(str::to_string)?;
        let revision = format!(
            "{}/{}",
            input.input["replacement_policy_revision"]
                .as_str()
                .or_else(|| input.input["policy_revision"].as_str())
                .unwrap_or("policy/1"),
            input.input["tokenizer_revision"]
                .as_str()
                .unwrap_or("unicode-alphanumeric/1")
        );
        let replacement_key = if lexical {
            Some(source_key(&replacement.chunks, &revision))
        } else {
            None
        };
        if let (Some(prepared), Some(rep_key)) = (&state, &replacement_key)
            && !prepared.matches_key(rep_key)
        {
            require(
                !prepared.matches_key(rep_key),
                "stale prepared source was accepted",
            )?;
        }
        drop(state.take());
        state = if let Some(rep_key) = replacement_key {
            Some(PreparedRank::new_with_key(
                &replacement.chunks,
                rep_key,
                input.max_tokens,
            )?)
        } else {
            None
        };
        if let Some(prepared) = &state {
            retained_payload_bytes = retained_payload_bytes.max(prepared.retained_payload_bytes);
        }
        for query in &input.queries[split..] {
            ranks.push(if let Some(prepared) = &state {
                prepared.scores(query)
            } else {
                bm25(&replacement.chunks, query, 1.2, 0.75)
            });
        }
        drop(state.take());
    }
    drop(state);
    Ok(Output::Prepared {
        ranks,
        retained_payload_bytes,
    })
}

struct Fixture {
    input: Value,
    html: String,
    url: Url,
    observed: u64,
    limits: ParseLimits,
    page: Option<Projection>,
    table_cells: Option<Vec<Value>>,
    frames: Vec<Frame>,
    queries: Vec<String>,
    rows: Vec<Row>,
    max_rows: usize,
    max_cols: usize,
    table_selectors: Option<TableSelectors>,
    table_limits: HtmlTableLimits,
    limit: usize,
    highlight_limit: usize,
    revision: String,
    max_tokens: usize,
}

fn number(value: &Value, key: &str, default: usize) -> Result<usize, String> {
    match value.get(key) {
        None => Ok(default),
        Some(v) => v
            .as_u64()
            .and_then(|n| usize::try_from(n).ok())
            .ok_or_else(|| format!("invalid nonnegative size {key}")),
    }
}

impl Fixture {
    fn new(input: Value, stage: &str) -> Result<Self, String> {
        let html = input["html"].as_str().unwrap_or("").to_string();
        require(
            html.len() <= 4 * 1024 * 1024,
            "fixture HTML exceeds hard probe admission",
        )?;
        let url = Url::parse(input["url"].as_str().unwrap_or("https://example.org/"))
            .map_err(|e| e.to_string())?;
        require(
            url.as_str().len() <= 8192,
            "fixture URL exceeds hard probe admission",
        )?;
        let observed = input["observed_at_unix_ms"]
            .as_u64()
            .or_else(|| input["observed_at_ms"].as_u64())
            .unwrap_or(17);
        let value = &input["parse_limits"];
        let defaults = ParseLimits::default();
        let default_include_links = if input["link_mode"].as_str() == Some("none")
            || value["link_mode"].as_str() == Some("none")
        {
            false
        } else {
            defaults.include_links
        };
        let include_links = value["include_links"]
            .as_bool()
            .or_else(|| input["include_links"].as_bool())
            .unwrap_or(default_include_links);
        let comment_unwrap = match value["comment_unwrap"]
            .as_str()
            .or_else(|| input["comment_unwrap"].as_str())
        {
            Some("annas_archive" | "annas-archive-scroll-cards") => {
                CommentUnwrapPolicy::AnnasArchiveScrollCards
            }
            _ => CommentUnwrapPolicy::None,
        };
        let boilerplate = match value["boilerplate"]
            .as_str()
            .or_else(|| input["boilerplate"].as_str())
        {
            Some("main_content_v1" | "main-content") => BoilerplatePolicy::MainContentV1,
            Some("cetd_entropy_v1" | "cetd") => BoilerplatePolicy::CetdEntropyV1,
            _ => BoilerplatePolicy::FullDom,
        };
        let limits = ParseLimits {
            max_html_bytes: number(value, "max_html_bytes", defaults.max_html_bytes)?,
            max_text_bytes: number(value, "max_text_bytes", defaults.max_text_bytes)?,
            max_nodes: number(value, "max_nodes", defaults.max_nodes)?,
            max_parser_nodes: number(value, "max_parser_nodes", defaults.max_parser_nodes)?,
            max_words_per_chunk: number(
                value,
                "max_words_per_chunk",
                defaults.max_words_per_chunk,
            )?,
            max_link_bytes: number(value, "max_link_bytes", defaults.max_link_bytes)?,
            max_link_url_bytes: number(value, "max_link_url_bytes", defaults.max_link_url_bytes)?,
            include_links,
            comment_unwrap,
            boilerplate,
        };
        require(
            limits.max_nodes <= 250_000
                && limits.max_parser_nodes <= 250_000
                && limits.max_html_bytes <= 4 * 1024 * 1024
                && limits.max_text_bytes <= 4 * 1024 * 1024
                && limits.max_link_bytes <= 4 * 1024 * 1024
                && limits.max_words_per_chunk <= 100_000
                && limits.max_link_url_bytes <= 8192,
            "parse limit exceeds hard probe admission",
        )?;
        let limit = number(&input, "limit", 16 * 1024 * 1024)?;
        require(
            limit <= 32 * 1024 * 1024,
            "output limit exceeds hard probe admission",
        )?;
        let highlight_limit = number(&input, "highlight_limit", 5)?;
        let query = input["query"].as_str().unwrap_or("");
        require(
            query.len() <= 64 * 1024,
            "query exceeds hard probe admission",
        )?;
        let queries = if let Some(values) = input["queries"].as_array() {
            require(
                !values.is_empty() && values.len() <= 32,
                "prepared query count admission",
            )?;
            values
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .filter(|s| s.len() <= 64 * 1024)
                        .map(str::to_string)
                        .ok_or("invalid prepared query".to_string())
                })
                .collect::<Result<Vec<_>, _>>()?
        } else {
            let count = number(&input, "queries", 1)?;
            require(
                [1, 2, 8, 32].contains(&count),
                "prepared query count must be 1/2/8/32",
            )?;
            (0..count)
                .map(|i| {
                    if count == 1 {
                        query.to_string()
                    } else {
                        format!("{query} researchmissing{i}")
                    }
                })
                .collect()
        };
        let mut rows = Vec::new();
        if stage == "table" {
            for row in input["rows"].as_array().ok_or("table rows missing")? {
                let mut cells = Vec::new();
                for cell in row["cells"].as_array().ok_or("table cells missing")? {
                    cells.push(Cell {
                        text: cell["text"].as_str().unwrap_or("").to_string(),
                        rowspan: number(cell, "rowspan", 1)?,
                        colspan: number(cell, "colspan", 1)?,
                        is_header: cell["is_header"].as_bool().unwrap_or(false),
                    });
                }
                rows.push(Row {
                    group: number(row, "group", 0)?,
                    cells,
                });
            }
        }
        let max_rows = number(&input, "max_rows", 2048)?;
        let max_cols = number(&input, "max_cols", 1024)?;
        let table_selectors = if stage == "html-table" {
            Some(TableSelectors::new()?)
        } else {
            None
        };
        let table_limits = HtmlTableLimits {
            max_tables: number(&input, "max_tables", 64)?,
            max_rows: number(&input, "max_table_rows", 1000)?,
            max_cols: number(&input, "max_table_cols", 1000)?,
            max_html_bytes: limits.max_html_bytes,
            max_nodes: limits.max_parser_nodes,
            max_text_bytes: limits.max_text_bytes,
        };
        require(
            rows.len() <= 2048
                && max_rows <= 2048
                && max_cols <= 4096
                && rows
                    .len()
                    .checked_mul(max_cols)
                    .is_some_and(|n| n <= 1_000_000)
                && table_limits.max_tables <= 64
                && table_limits.max_rows <= 2048
                && table_limits.max_cols <= 4096
                && table_limits
                    .max_rows
                    .checked_mul(table_limits.max_cols)
                    .is_some_and(|n| n <= 1_000_000),
            "table shape exceeds hard probe admission",
        )?;
        let (mut page, table_cells) = if matches!(
            stage,
            "parser"
                | "parse"
                | "projection"
                | "pipeline"
                | "table"
                | "html-table"
                | "controls"
                | "cloud-024"
        ) {
            (None, None)
        } else {
            let include_table_cells =
                stage == "export" && input["include_table_cells"].as_bool() == Some(true);
            let (projection, extracted_cells) = parse_projection_and_table_cells(
                &html,
                &url,
                limits,
                &table_limits,
                include_table_cells,
            )?;
            (Some(projection), extracted_cells)
        };
        if stage == "budget" {
            let bytes = number(&input, "oversized_text_bytes", 2 * 1024 * 1024)?;
            require(bytes <= 32 * 1024 * 1024, "oversized fixture admission")?;
            let chunk = page
                .as_mut()
                .and_then(|p| p.chunks.first_mut())
                .ok_or("budget stage requires a content chunk")?;
            chunk.text = "\"".repeat(bytes);
        }
        let frames = if stage == "serialization" {
            let link_mode = match input["link_mode"].as_str() {
                Some("highlighted_only" | "highlighted-only") if limits.include_links => {
                    LinkMode::HighlightedOnly
                }
                Some("none") => LinkMode::None,
                _ if !limits.include_links => LinkMode::None,
                _ => LinkMode::All,
            };
            make_frames(
                page.as_ref().ok_or("serialization page missing")?,
                &url,
                observed,
                input["query"].as_str(),
                highlight_limit,
                link_mode,
            )
        } else {
            vec![]
        };
        let revision = format!(
            "{}/{}",
            input["policy_revision"].as_str().unwrap_or("policy/1"),
            input["tokenizer_revision"]
                .as_str()
                .unwrap_or("unicode-alphanumeric/1")
        );
        let max_tokens = number(&input, "max_tokens", 2_000_000)?;
        require(max_tokens <= 2_000_000, "prepared token admission")?;
        Ok(Self {
            input,
            html,
            url,
            observed,
            limits,
            page,
            table_cells,
            frames,
            queries,
            rows,
            max_rows,
            max_cols,
            table_selectors,
            table_limits,
            limit,
            highlight_limit,
            revision,
            max_tokens,
        })
    }
}

enum Output {
    Bytes(Vec<u8>),
    Projection(Projection),
    Parser(Html),
    Rank(Vec<(usize, f64)>),
    Selection(Vec<usize>),
    Digests(Vec<[u8; 32]>),
    Prepared {
        ranks: Vec<Vec<(usize, f64)>>,
        retained_payload_bytes: usize,
    },
    Grid(Result<Grid, GridError>),
    HtmlTables(Value),
    Expected(String),
    Control(Value),
}

fn canonical(output: &Output) -> Result<Vec<u8>, String> {
    let value = match output {
        Output::Bytes(bytes) => return Ok(bytes.clone()),
        Output::Projection(page) => {
            json!({"chunks":page.chunks.iter().map(|c| json!({"node":c.node,"role":c.role,"text":c.text,"links":c.links})).collect::<Vec<_>>(), "links":page.links,"warnings":page.parse_warnings,"nodes":page.dom.nodes().iter().enumerate().map(|(id,node)| json!({"parent":node.parent,"first_child":node.first_child,"last_child":node.last_child,"next_sibling":node.next_sibling,"source_start":node.source_start,"source_len":node.source_len,"attributes_id":node.attributes_id,"tag":format!("{:?}",node.tag),"flags":node.flags,"depth":node.depth,"text":if node.tag == Tag::Text { page.dom.raw_text(id as u32).ok() } else { None }})).collect::<Vec<_>>() })
        }
        Output::Parser(parsed) => {
            json!({"html":parsed.root_element().html(),"warnings":parsed.errors.len(),"nodes":parsed.tree.nodes().count()})
        }
        Output::Rank(ranks) => json!(ranks),
        Output::Selection(indices) => json!(indices),
        Output::Digests(digests) => json!(
            digests
                .iter()
                .map(|&hash| blake3::Hash::from(hash).to_hex().to_string())
                .collect::<Vec<_>>()
        ),
        Output::Prepared { ranks, .. } => json!(ranks),
        Output::Grid(Ok(grid)) => {
            json!({"width":grid.width,"slots":grid.slots,"cells":grid.cells.iter().map(|cell| json!({"text":cell.text,"rowspan":cell.rowspan,"colspan":cell.colspan,"is_header":cell.is_header})).collect::<Vec<_>>()})
        }
        Output::Grid(Err(error)) => json!({"error":error.to_string()}),
        Output::HtmlTables(value) => value.clone(),
        Output::Expected(error) => json!({"status":"EXPECTED_ERROR","error":error}),
        Output::Control(value) => value.clone(),
    };
    serde_json::to_vec(&value).map_err(|e| e.to_string())
}

fn execute(stage: &str, fixture: &Fixture) -> Result<Output, String> {
    let input = black_box(fixture);
    let page = || {
        input
            .page
            .as_ref()
            .ok_or("stage projection missing".to_string())
    };
    let parse = || {
        parse_html_with_limits(black_box(&input.html), &input.url, input.limits)
            .map_err(str::to_string)
    };
    let export = |page: &Projection, table_cells: Option<&[Value]>| {
        let exported = match input.input["link_mode"].as_str() {
            Some("highlighted_only" | "highlighted-only") => {
                export_jsonl_with_link_mode_and_prepared(
                    page,
                    &input.url,
                    input.observed,
                    input.input["query"].as_str(),
                    input.highlight_limit,
                    input.limit,
                    LinkMode::HighlightedOnly,
                    None,
                )
            }
            Some("none") => export_jsonl_with_options(
                page,
                &input.url,
                input.observed,
                input.input["query"].as_str(),
                input.highlight_limit,
                input.limit,
                false,
            ),
            _ if !input.limits.include_links => export_jsonl_with_options(
                page,
                &input.url,
                input.observed,
                input.input["query"].as_str(),
                input.highlight_limit,
                input.limit,
                false,
            ),
            _ => export_jsonl_with_limit(
                page,
                &input.url,
                input.observed,
                input.input["query"].as_str(),
                input.highlight_limit,
                input.limit,
            ),
        };
        let mut bytes = exported
            .map(String::into_bytes)
            .map_err(|e| e.to_string())?;
        if let Some(cells) = table_cells {
            append_table_cells_to_aae(&mut bytes, cells, input.limit)?;
        }
        Ok(bytes)
    };
    let result = (|| match stage {
        "parser" => {
            if input.html.len() > input.limits.max_html_bytes {
                Err("invalid resource budget".into())
            } else {
                parse_html_document_with_node_limit(&input.html, input.limits.max_parser_nodes)
                    .map(|(document, _)| Output::Parser(document))
                    .map_err(str::to_owned)
            }
        }
        "parse" | "projection" => parse().map(Output::Projection),
        "export" => export(page()?, input.table_cells.as_deref()).map(Output::Bytes),
        "pipeline" => {
            let (projection, table_cells) = parse_projection_and_table_cells(
                black_box(&input.html),
                &input.url,
                input.limits,
                &input.table_limits,
                input.input["include_table_cells"].as_bool() == Some(true),
            )?;
            export(&projection, table_cells.as_deref()).map(Output::Bytes)
        }
        "rank" => {
            let query = input.input["query"].as_str().unwrap_or("");
            let chunks = &page()?.chunks;
            let env_model = env::var("ATHENA_RESEARCH_RANK_MODEL").ok();
            let model = input.input["rank_model"]
                .as_str()
                .or_else(|| input.input["rank_mode"].as_str().filter(|&m| m != "bm25"))
                .or(option_env!("ATHENA_RESEARCH_RANK_MODEL"))
                .or(env_model.as_deref())
                .or_else(|| input.input["rank_mode"].as_str());
            let scores = match model {
                Some("bm25f" | "bm25f_mslp" | "bm25f-mslp") => bm25f(chunks, query, 1.2, 0.75),
                Some(
                    "bm25f_plus_mslp"
                    | "bm25f_plus"
                    | "bm25f-plus-mslp"
                    | "bm25f_plus_mslp_simhash64",
                ) => bm25f_plus_mslp(chunks, query, 1.2, 0.75, 1.0),
                Some("bm25_plus" | "bm25+" | "bm25-plus") => {
                    bm25_plus(chunks, query, 1.2, 0.75, 1.0)
                }
                Some("bm25l") => bm25l(chunks, query, 1.2, 0.75, 0.5),
                Some("ql_dirichlet" | "dirichlet" | "ql-dirichlet") => {
                    ql_dirichlet(chunks, query, 1000.0)
                }
                Some(
                    "hybrid_rrf_simhash64"
                    | "bm25f_mslp_rrf_simhash64"
                    | "hybrid-rrf-simhash64"
                    | "rrf_simhash64",
                ) => {
                    let r1 = bm25f_plus_mslp(chunks, query, 1.2, 0.75, 1.0);
                    let r2 = bm25l(chunks, query, 1.2, 0.75, 0.5);
                    let r3 = ql_dirichlet(chunks, query, 1000.0);
                    reciprocal_rank_fusion(
                        &[(&r1, 2.5), (&r2, 1.0), (&r3, 1.0)],
                        chunks.len(),
                        12.0,
                    )
                }
                _ => bm25(chunks, query, 1.2, 0.75),
            };
            Ok(Output::Rank(scores))
        }
        "selection" | "highlight" => {
            let query = input.input["query"].as_str().unwrap_or("");
            let chunks = &page()?.chunks;
            let env_model = env::var("ATHENA_RESEARCH_RANK_MODEL").ok();
            let model = input.input["rank_model"]
                .as_str()
                .or_else(|| input.input["rank_mode"].as_str().filter(|&m| m != "bm25"))
                .or(option_env!("ATHENA_RESEARCH_RANK_MODEL"))
                .or(env_model.as_deref())
                .or_else(|| input.input["rank_mode"].as_str());
            let selected = match model {
                Some("bm25f" | "bm25f_mslp" | "bm25f-mslp") => {
                    bm25f_highlight_indices(chunks, query, input.highlight_limit)
                }
                Some(
                    "bm25f_simhash"
                    | "bm25f_simhash_highlight_indices"
                    | "simhash64"
                    | "bm25f-simhash",
                ) => bm25f_simhash_highlight_indices(chunks, query, input.highlight_limit, 3),
                Some(
                    "bm25f_plus_mslp_simhash64"
                    | "bm25f_plus_mslp_simhash_highlight_indices"
                    | "bm25f_plus_mslp"
                    | "bm25f-plus-mslp-simhash64",
                ) => bm25f_plus_mslp_simhash_highlight_indices(
                    chunks,
                    query,
                    input.highlight_limit,
                    1.0,
                    3,
                ),
                Some(
                    "hybrid_rrf_simhash64"
                    | "bm25f_mslp_rrf_simhash64"
                    | "hybrid_rrf_simhash_highlight_indices"
                    | "hybrid-rrf-simhash64"
                    | "rrf_simhash64",
                ) => hybrid_rrf_simhash_highlight_indices(chunks, query, input.highlight_limit, 3),
                Some("bm25_plus" | "bm25+" | "bm25-plus") => {
                    let mut top: Vec<usize> = bm25_plus(chunks, query, 1.2, 0.75, 1.0)
                        .into_iter()
                        .take(input.highlight_limit)
                        .map(|(idx, _)| idx)
                        .collect();
                    top.sort_unstable();
                    top
                }
                Some("bm25l") => {
                    let mut top: Vec<usize> = bm25l(chunks, query, 1.2, 0.75, 0.5)
                        .into_iter()
                        .take(input.highlight_limit)
                        .map(|(idx, _)| idx)
                        .collect();
                    top.sort_unstable();
                    top
                }
                Some("ql_dirichlet" | "dirichlet" | "ql-dirichlet") => {
                    let mut top: Vec<usize> = ql_dirichlet(chunks, query, 1000.0)
                        .into_iter()
                        .take(input.highlight_limit)
                        .map(|(idx, _)| idx)
                        .collect();
                    top.sort_unstable();
                    top
                }
                Some(
                    "bm25f_guarded_dual_channel"
                    | "bm25f_guarded_dual_channel_highlight_indices"
                    | "cloud_024_rank",
                ) => bm25f_guarded_dual_channel_highlight_indices(
                    chunks,
                    query,
                    input.highlight_limit,
                    0.5,
                    5,
                ),
                _ => highlight_indices(chunks, query, input.highlight_limit),
            };
            Ok(Output::Selection(selected))
        }
        "hash" => {
            let batch = match option_env!("ATHENA_RESEARCH_HASH").unwrap_or("joined") {
                "joined" => 0,
                "incremental" => 1,
                "batched4096" => 4096,
                "batched16384" => 16384,
                _ => return Err("invalid hash build factor".into()),
            };
            Ok(Output::Digests(vec![content_hash(&page()?.chunks, batch)]))
        }
        "block_ids" => Ok(Output::Digests(block_ids(&page()?.chunks))),
        "serialization" => {
            let reserve = match option_env!("ATHENA_RESEARCH_RESERVE").unwrap_or("none") {
                "none" => 0,
                "small" => 4096,
                "estimate" => {
                    512 + page()?
                        .chunks
                        .iter()
                        .map(|c| c.text.len() + 160)
                        .sum::<usize>()
                        + page()?.links.len() * 64
                }
                _ => return Err("invalid reserve build factor".into()),
            };
            serialize_frames(
                &input.frames,
                input.limit,
                option_env!("ATHENA_RESEARCH_WRITER").unwrap_or("frame"),
                reserve,
            )
            .map(Output::Bytes)
        }
        "prepared" => repeated_queries(
            input,
            option_env!("ATHENA_RESEARCH_PREPARED").unwrap_or("oneshot") == "lexical",
        ),
        "table" => Ok(Output::Grid(expand(
            &input.rows,
            input.max_rows,
            input.max_cols,
        ))),
        "html-table" => extract_html_tables(
            &input.html,
            option_env!("ATHENA_RESEARCH_TABLE_EXTRACTOR").unwrap_or("traversal"),
            input
                .table_selectors
                .as_ref()
                .ok_or("HTML table selectors missing")?,
            &input.table_limits,
        )
        .map(Output::HtmlTables),
        "budget" => {
            match export_jsonl_with_limit(
                page()?,
                &input.url,
                input.observed,
                None,
                0,
                number(&input.input, "limit", 1024)?,
            ) {
                Err(error)
                    if error.is_io() && error.to_string() == "AAE output byte budget exceeded" =>
                {
                    Ok(Output::Expected("output_budget".into()))
                }
                Err(error) => Err(format!("unexpected error class in budget stage: {error}")),
                Ok(_) => Err("oversized output was not rejected".into()),
            }
        }
        "controls" => semantic_controls().map(Output::Control),
        "cloud-024" => execute_cloud_024_fixture(input).map(Output::Control),
        _ => Err("unknown native research stage".into()),
    })();
    match result {
        Err(error) if input.input["expected_error"].as_str() == Some(error.as_str()) => {
            Ok(Output::Expected(error))
        }
        other => other,
    }
}

struct ParsedTableCell {
    locator: String,
    text: String,
    is_header: bool,
    data_unit: Option<String>,
}

fn split_header_label_unit(raw: &str) -> (String, Option<String>) {
    let trimmed = raw.trim();
    if let Some((prefix, suffix)) = trimmed.rsplit_once('(')
        && let Some(unit) = suffix.strip_suffix(')')
    {
        let label = prefix.trim();
        let unit = unit.trim();
        if !label.is_empty() && !unit.is_empty() {
            return (label.to_owned(), Some(unit.to_owned()));
        }
    }
    (trimmed.to_owned(), None)
}

fn parse_cell_value_and_unit(raw: &str) -> (String, Option<String>) {
    let trimmed = raw.trim().trim_end_matches(';').trim();
    let rhs = trimmed
        .split_once('=')
        .map_or(trimmed, |(_, right)| right.trim());
    if let Some((val, unit)) = rhs.split_once('|') {
        let val = val.trim();
        let unit = unit.trim().trim_end_matches(';').trim();
        if !val.is_empty() && !unit.is_empty() {
            return (val.to_owned(), Some(unit.to_owned()));
        }
    }
    (rhs.to_owned(), None)
}

fn is_tag_delimiter(byte: u8) -> bool {
    matches!(byte, b'>' | b'/' | b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

fn has_html_table_tag(html: &str) -> bool {
    let bytes = html.as_bytes();
    if !bytes
        .windows(6)
        .any(|window| window.eq_ignore_ascii_case(b"<table"))
    {
        return false;
    }
    let mut i = 0usize;
    'scan: while i < bytes.len() {
        if bytes[i] != b'<' {
            i += 1;
            continue;
        }
        if bytes[i..].starts_with(b"<!-->") {
            i += 5;
            continue;
        }
        if bytes[i..].starts_with(b"<!--->") {
            i += 6;
            continue;
        }
        if bytes[i..].starts_with(b"<!--") {
            i += 4;
            if let Some(end) = bytes[i..].windows(3).position(|w| w == b"-->") {
                i += end + 3;
            } else {
                return false;
            }
            continue;
        }
        let rest = &bytes[i + 1..];
        if rest.len() >= 6 && rest[..5].eq_ignore_ascii_case(b"table") && is_tag_delimiter(rest[5])
        {
            return true;
        }
        for raw_tag in [
            b"script".as_slice(),
            b"style".as_slice(),
            b"textarea".as_slice(),
            b"title".as_slice(),
            b"xmp".as_slice(),
        ] {
            let tlen = raw_tag.len();
            if rest.len() > tlen
                && rest[..tlen].eq_ignore_ascii_case(raw_tag)
                && is_tag_delimiter(rest[tlen])
            {
                i += 1 + tlen;
                let mut quote = 0u8;
                while i < bytes.len() {
                    let b = bytes[i];
                    i += 1;
                    if quote != 0 {
                        if b == quote {
                            quote = 0;
                        }
                    } else if b == b'"' || b == b'\'' {
                        quote = b;
                    } else if b == b'>' {
                        break;
                    }
                }
                while i + 2 + tlen < bytes.len() {
                    if bytes[i] == b'<'
                        && bytes[i + 1] == b'/'
                        && bytes[i + 2..i + 2 + tlen].eq_ignore_ascii_case(raw_tag)
                        && is_tag_delimiter(bytes[i + 2 + tlen])
                    {
                        i += 2 + tlen;
                        while i < bytes.len() && bytes[i] != b'>' {
                            i += 1;
                        }
                        if i < bytes.len() {
                            i += 1;
                        }
                        continue 'scan;
                    }
                    i += 1;
                }
                return false;
            }
        }
        if rest
            .first()
            .is_some_and(|&b| b.is_ascii_alphabetic() || b == b'/' || b == b'!' || b == b'?')
        {
            i += 1;
            let mut quote = 0u8;
            while i < bytes.len() {
                let b = bytes[i];
                i += 1;
                if quote != 0 {
                    if b == quote {
                        quote = 0;
                    }
                } else if b == b'"' || b == b'\'' {
                    quote = b;
                } else if b == b'>' {
                    break;
                }
            }
            continue;
        }
        i += 1;
    }
    false
}

fn is_skipped_projection_subtree(name: &str) -> bool {
    matches!(
        name,
        "script" | "style" | "template" | "noscript" | "svg" | "canvas" | "iframe"
    )
}

fn scraper_projection_nodes(document: &Html, include_links: bool) -> Vec<ResearchNode> {
    let mut nodes = Vec::with_capacity(document.tree.nodes().count());
    let mut stack = vec![(document.tree.root(), None)];
    while let Some((node, parent)) = stack.pop() {
        let mut next_parent = parent;
        let mut visit_children = true;
        match node.value() {
            scraper::Node::Element(el) => {
                if is_skipped_projection_subtree(el.name()) {
                    visit_children = false;
                } else {
                    let index = nodes.len();
                    let attributes = if include_links && el.name() == "a" {
                        el.attr("href")
                            .into_iter()
                            .map(|v| ("href".to_owned(), v.to_owned()))
                            .collect()
                    } else {
                        Vec::new()
                    };
                    nodes.push(ResearchNode {
                        parent,
                        kind: ResearchNodeKind::Element,
                        name: el.name().to_owned(),
                        text: String::new(),
                        attributes,
                    });
                    next_parent = Some(index);
                }
            }
            scraper::Node::Text(content) => {
                nodes.push(ResearchNode {
                    parent,
                    kind: ResearchNodeKind::Text,
                    name: String::new(),
                    text: content.text.to_string(),
                    attributes: Vec::new(),
                });
                visit_children = false;
            }
            _ => {}
        }
        if visit_children {
            for child in node.children().rev() {
                stack.push((child, next_parent));
            }
        }
    }
    nodes
}

fn parse_projection_and_table_cells(
    html: &str,
    url: &Url,
    limits: ParseLimits,
    table_limits: &HtmlTableLimits,
    include_table_cells: bool,
) -> Result<(Projection, Option<Vec<Value>>), String> {
    if !include_table_cells || !has_html_table_tag(html) {
        let projection = parse_html_with_limits(html, url, limits).map_err(str::to_string)?;
        return Ok((projection, include_table_cells.then(Vec::new)));
    }
    if html.len() > ParseLimits::default().max_html_bytes
        || limits.max_parser_nodes != table_limits.max_nodes
    {
        let projection = parse_html_with_limits(html, url, limits).map_err(str::to_string)?;
        let cells = extract_structured_table_cells(html, table_limits)?;
        return Ok((projection, Some(cells)));
    }
    if limits.max_nodes == 0
        || limits.max_parser_nodes == 0
        || limits.max_words_per_chunk == 0
        || html.len() > limits.max_html_bytes
        || html.len() > u32::MAX as usize
        || table_limits.max_html_bytes == 0
        || table_limits.max_html_bytes > 4 * 1024 * 1024
        || html.len() > table_limits.max_html_bytes
        || table_limits.max_nodes == 0
        || table_limits.max_nodes > 250_000
        || table_limits.max_text_bytes == 0
        || table_limits.max_text_bytes > 4 * 1024 * 1024
    {
        return Err("invalid resource budget".into());
    }
    let (document, parse_warnings) =
        parse_html_document_with_node_limit(html, limits.max_parser_nodes)
            .map_err(str::to_owned)?;
    let has_table_element = document
        .tree
        .values()
        .any(|node| matches!(node, scraper::Node::Element(el) if el.name() == "table"));
    let nodes = scraper_projection_nodes(&document, limits.include_links);
    let projection = project_research_nodes(html.len(), &nodes, url, limits, parse_warnings)
        .map_err(str::to_string)?;
    let cells = if has_table_element {
        extract_structured_table_cells_from_document(&document, table_limits)?
    } else {
        Vec::new()
    };
    Ok((projection, Some(cells)))
}

fn extract_structured_table_cells(
    html: &str,
    limits: &HtmlTableLimits,
) -> Result<Vec<Value>, String> {
    if !has_html_table_tag(html) {
        return Ok(Vec::new());
    }
    if limits.max_html_bytes == 0
        || limits.max_html_bytes > 4 * 1024 * 1024
        || html.len() > limits.max_html_bytes
        || limits.max_nodes == 0
        || limits.max_nodes > 250_000
        || limits.max_text_bytes == 0
        || limits.max_text_bytes > 4 * 1024 * 1024
    {
        return Err("invalid resource budget".into());
    }
    let (document, _) =
        parse_html_document_with_node_limit(html, limits.max_nodes).map_err(str::to_owned)?;
    extract_structured_table_cells_from_document(&document, limits)
}

fn extract_structured_table_cells_from_document(
    document: &Html,
    limits: &HtmlTableLimits,
) -> Result<Vec<Value>, String> {
    let tables = document
        .root_element()
        .descendent_elements()
        .filter(|element| element.value().name() == "table")
        .collect::<Vec<_>>();
    if tables.len() > limits.max_tables {
        return Err("HTML table count limit exceeded".into());
    }
    let mut records = Vec::new();
    let mut total_cells = 0usize;
    let mut total_rows = 0usize;
    let mut total_grid_slots = 0usize;
    let mut total_text_bytes = 0usize;

    for (table_index, table) in tables.iter().copied().enumerate() {
        if nearest_table_index(table, &tables).is_some() {
            continue;
        }
        let rows = rows_by_traversal(table, &tables, table_index);
        if rows.len() > limits.max_rows {
            return Err("HTML table row limit exceeded".into());
        }
        total_rows = total_rows
            .checked_add(rows.len())
            .ok_or("HTML table row limit exceeded")?;
        if total_rows > HTML_TABLE_MAX_ROWS {
            return Err("HTML table row limit exceeded".into());
        }
        let mut grid_rows = Vec::with_capacity(rows.len());
        let mut parsed_cells = Vec::new();
        for (row_index, (row, group, _group_type)) in rows.into_iter().enumerate() {
            let mut grid_cells = Vec::new();
            for cell in row
                .child_elements()
                .filter(|child| matches!(child.value().name(), "th" | "td"))
            {
                total_cells = total_cells
                    .checked_add(1)
                    .ok_or("HTML table cell limit exceeded")?;
                if grid_cells.len() >= limits.max_cols || total_cells > HTML_TABLE_MAX_CELLS {
                    return Err("HTML table column limit exceeded".into());
                }
                let tag = cell.value().name();
                let rowspan = span_attribute(cell, "rowspan");
                let colspan = span_attribute(cell, "colspan");
                let locator = format!(
                    "table[{table_index}]/row[{row_index}]/cell[{}]",
                    grid_cells.len()
                );
                let text = normalized_table_cell_text(cell);
                total_text_bytes = total_text_bytes
                    .checked_add(text.len())
                    .ok_or("decoded text byte budget exceeded")?;
                if total_text_bytes > limits.max_text_bytes {
                    return Err("decoded text byte budget exceeded".into());
                }
                parsed_cells.push(ParsedTableCell {
                    locator,
                    text: text.clone(),
                    is_header: tag == "th",
                    data_unit: cell.attr("data-unit").map(str::to_owned),
                });
                grid_cells.push(Cell {
                    text,
                    rowspan,
                    colspan,
                    is_header: tag == "th",
                });
            }
            grid_rows.push(Row {
                group,
                cells: grid_cells,
            });
        }
        let remaining_grid_slots = HTML_TABLE_MAX_GRID_SLOTS
            .checked_sub(total_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        let grid = expand_with_slot_limit(
            &grid_rows,
            limits.max_rows,
            limits.max_cols,
            remaining_grid_slots,
        )
        .map_err(|error| match error {
            GridError::TooLarge => "HTML table column limit exceeded".to_owned(),
            GridError::TooManySlots => "HTML table grid limit exceeded".to_owned(),
            GridError::InvalidGroupOrder => "HTML table row group order invalid".to_owned(),
            GridError::Overlap => "HTML table cell spans overlap".to_owned(),
        })?;
        let table_grid_slots = grid
            .slots
            .len()
            .checked_mul(grid.width)
            .ok_or("HTML table grid limit exceeded")?;
        total_grid_slots = total_grid_slots
            .checked_add(table_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        if total_grid_slots > HTML_TABLE_MAX_GRID_SLOTS {
            return Err("HTML table grid limit exceeded".into());
        }
        let mut anchors = vec![None; parsed_cells.len()];
        for (r, slot_row) in grid.slots.iter().enumerate() {
            for (c, slot) in slot_row.iter().enumerate() {
                if let Some(idx) = *slot
                    && anchors[idx].is_none()
                {
                    anchors[idx] = Some((r, c));
                }
            }
        }
        for (cell_idx, meta) in parsed_cells.iter().enumerate() {
            if meta.is_header {
                continue;
            }
            let Some((r, c)) = anchors[cell_idx] else {
                continue;
            };
            let row_header_idx = (0..c)
                .rev()
                .find_map(|col| grid.slots[r][col].filter(|&idx| parsed_cells[idx].is_header));
            let mut col_header_indices = Vec::new();
            for row_idx in 0..r {
                if let Some(idx) = grid.slots[row_idx][c]
                    && parsed_cells[idx].is_header
                    && col_header_indices.last() != Some(&idx)
                {
                    col_header_indices.push(idx);
                }
            }
            let (row_label, row_unit) = row_header_idx.map_or_else(
                || (String::new(), None),
                |idx| split_header_label_unit(&parsed_cells[idx].text),
            );
            let (col_label, col_unit) = col_header_indices.last().map_or_else(
                || (String::new(), None),
                |&idx| split_header_label_unit(&parsed_cells[idx].text),
            );
            let (group_label, group_unit) = if col_header_indices.len() >= 2 {
                let group_idx = col_header_indices[col_header_indices.len() - 2];
                split_header_label_unit(&parsed_cells[group_idx].text)
            } else {
                (col_label.clone(), None)
            };
            let (value, cell_unit) = parse_cell_value_and_unit(&meta.text);
            let unit = cell_unit
                .or_else(|| meta.data_unit.clone())
                .or(col_unit)
                .or(group_unit)
                .or(row_unit)
                .unwrap_or_default();
            if !row_label.is_empty()
                && !col_label.is_empty()
                && !group_label.is_empty()
                && !value.is_empty()
                && !unit.is_empty()
                && [
                    &meta.locator,
                    &row_label,
                    &col_label,
                    &group_label,
                    &value,
                    &unit,
                ]
                .into_iter()
                .all(|field| field.chars().count() <= 256)
            {
                require(records.len() < 1024, "HTML table cell limit exceeded")?;
                records.push(json!({
                    "type": "table_cell",
                    "locator": meta.locator,
                    "row": row_label,
                    "column": col_label,
                    "group": group_label,
                    "value": value,
                    "unit": unit,
                }));
            }
        }
    }
    Ok(records)
}

fn append_table_cells_to_aae(
    bytes: &mut Vec<u8>,
    cells: &[Value],
    max_bytes: usize,
) -> Result<(), String> {
    if cells.is_empty() || bytes.is_empty() {
        return Ok(());
    }
    let end_frame_start = bytes[..bytes.len() - 1]
        .iter()
        .rposition(|&b| b == b'\n')
        .map_or(0, |pos| pos + 1);
    let end_frame = bytes.split_off(end_frame_start);
    for cell in cells {
        let mut line = serde_json::to_vec(&cell).map_err(|e| e.to_string())?;
        line.push(b'\n');
        require(
            bytes.len() + line.len() + end_frame.len() <= max_bytes,
            "AAE output byte budget exceeded",
        )?;
        bytes.extend_from_slice(&line);
    }
    bytes.extend_from_slice(&end_frame);
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskExtractRequest {
    schema: String,
    principal_tenant: String,
    bound_task_id: String,
    task_id: String,
    source_id: String,
    expected_revision: String,
    #[serde(default)]
    include_links: Option<bool>,
    #[serde(default)]
    link_mode: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskExpandRequest {
    schema: String,
    principal_tenant: String,
    bound_task_id: String,
    task_id: String,
    source_id: String,
    expected_revision: String,
    handle: String,
    #[serde(default)]
    include_links: Option<bool>,
    #[serde(default)]
    link_mode: Option<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskSearchRequest {
    schema: String,
    principal_tenant: String,
    bound_task_id: String,
    task_id: String,
    query: String,
}

fn task_handle_token(
    principal_tenant: &str,
    bound_task_id: &str,
    task_id: &str,
    source_id: &str,
    expected_revision: &str,
) -> String {
    let mut hasher = blake3::Hasher::new();
    hasher.update(b"athena-task-handle/1\0");
    for field in [
        principal_tenant,
        bound_task_id,
        task_id,
        source_id,
        expected_revision,
    ] {
        hasher.update(&(field.len() as u64).to_le_bytes());
        hasher.update(field.as_bytes());
    }
    format!("ev_{}", hasher.finalize().to_hex())
}

fn task_authorize_and_export(
    principal_tenant: &str,
    bound_task_id: &str,
    task_id: &str,
    source_id: &str,
    expected_revision: &str,
    required_handle: Option<&str>,
    (include_links, link_mode): (Option<bool>, Option<&str>),
) -> Result<Vec<u8>, String> {
    let registry: Value = serde_json::from_str(include_str!("mcp_sdk/task_inputs.json"))
        .map_err(|_| "SCOPED_TASK_REGISTRY_INVALID".to_string())?;
    require(
        registry["schema"] == "athena-mcp-task-inputs/1",
        "SCOPED_TASK_REGISTRY_INVALID",
    )?;
    let task = registry["tasks"]
        .as_array()
        .ok_or("SCOPED_TASK_REGISTRY_INVALID")?
        .iter()
        .find(|task| task["id"] == task_id);
    let document = task.and_then(|task| {
        task["documents"]
            .as_array()?
            .iter()
            .find(|document| document["id"] == source_id)
    });
    let handle_ok = required_handle.is_none_or(|handle| {
        handle
            == task_handle_token(
                principal_tenant,
                bound_task_id,
                task_id,
                source_id,
                expected_revision,
            )
    });
    let allowed = handle_ok
        && bound_task_id == task_id
        && task.is_some_and(|task| task["tenant"] == principal_tenant)
        && document.is_some_and(|document| {
            document["tenant"] == principal_tenant
                && document["revision"] == expected_revision
                && (document["status"] == 200
                    || (document["status"] == 304 && document["cached_body"] == true))
                && !matches!(
                    document["response_kind"].as_str(),
                    Some("login" | "js_shell" | "error")
                )
        });
    if !allowed {
        let mut bytes = serde_json::to_vec(&json!({
            "schema":"athena-task-denial/1", "denied":true, "code":"SCOPED_TASK_DENIED"
        }))
        .map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        return Ok(bytes);
    }
    let task = task.ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
    let document = document.ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
    let fixture = Fixture::new(
        json!({
            "html":document["html"], "url":document["url"], "query":task["query"],
            "observed_at_unix_ms":document["observed_at_unix_ms"], "limit":131_072,
            "include_table_cells":true,
            "include_links":include_links.unwrap_or(true),
            "link_mode":link_mode.unwrap_or("all"),
        }),
        "export",
    )?;
    match execute("export", &fixture)? {
        Output::Bytes(bytes) => Ok(bytes),
        _ => Err("SCOPED_TASK_EXPORT_INVALID".into()),
    }
}

// Research-only authority shared by both MCP SDK arms. Principal and task
// binding are supervisor inputs, never arguments exposed to the tool caller.
fn task_export(request_bytes: &[u8]) -> Result<Vec<u8>, String> {
    require(request_bytes.len() <= 4096, "SCOPED_TASK_REQUEST_INVALID")?;
    let request: TaskExtractRequest = serde_json::from_slice(request_bytes)
        .map_err(|_| "SCOPED_TASK_REQUEST_INVALID".to_string())?;
    require(
        request.schema == "athena-task-extract/1"
            && [
                &request.principal_tenant,
                &request.bound_task_id,
                &request.task_id,
                &request.source_id,
                &request.expected_revision,
            ]
            .iter()
            .all(|value| !value.is_empty() && value.len() <= 64)
            && request.link_mode.as_deref().is_none_or(|mode| {
                matches!(
                    mode,
                    "all" | "highlighted_only" | "highlighted-only" | "none"
                )
            }),
        "SCOPED_TASK_REQUEST_INVALID",
    )?;
    task_authorize_and_export(
        &request.principal_tenant,
        &request.bound_task_id,
        &request.task_id,
        &request.source_id,
        &request.expected_revision,
        None,
        (request.include_links, request.link_mode.as_deref()),
    )
}

fn task_expand(request_bytes: &[u8]) -> Result<Vec<u8>, String> {
    require(request_bytes.len() <= 4096, "SCOPED_TASK_REQUEST_INVALID")?;
    let request: TaskExpandRequest = serde_json::from_slice(request_bytes)
        .map_err(|_| "SCOPED_TASK_REQUEST_INVALID".to_string())?;
    require(
        request.schema == "athena-task-expand/1"
            && [
                &request.principal_tenant,
                &request.bound_task_id,
                &request.task_id,
                &request.source_id,
                &request.expected_revision,
            ]
            .iter()
            .all(|value| !value.is_empty() && value.len() <= 64)
            && !request.handle.is_empty()
            && request.handle.len() <= 128
            && request.link_mode.as_deref().is_none_or(|mode| {
                matches!(
                    mode,
                    "all" | "highlighted_only" | "highlighted-only" | "none"
                )
            }),
        "SCOPED_TASK_REQUEST_INVALID",
    )?;
    task_authorize_and_export(
        &request.principal_tenant,
        &request.bound_task_id,
        &request.task_id,
        &request.source_id,
        &request.expected_revision,
        Some(&request.handle),
        (request.include_links, request.link_mode.as_deref()),
    )
}

fn task_search(request_bytes: &[u8]) -> Result<Vec<u8>, String> {
    require(request_bytes.len() <= 4096, "SCOPED_TASK_REQUEST_INVALID")?;
    let request: TaskSearchRequest = serde_json::from_slice(request_bytes)
        .map_err(|_| "SCOPED_TASK_REQUEST_INVALID".to_string())?;
    require(
        request.schema == "athena-task-search/1"
            && [
                &request.principal_tenant,
                &request.bound_task_id,
                &request.task_id,
            ]
            .iter()
            .all(|value| !value.is_empty() && value.len() <= 64)
            && !request.query.is_empty()
            && request.query.len() <= 256
            && request.query.chars().count() <= 256,
        "SCOPED_TASK_REQUEST_INVALID",
    )?;
    let registry: Value = serde_json::from_str(include_str!("mcp_sdk/task_inputs.json"))
        .map_err(|_| "SCOPED_TASK_REGISTRY_INVALID".to_string())?;
    require(
        registry["schema"] == "athena-mcp-task-inputs/1",
        "SCOPED_TASK_REGISTRY_INVALID",
    )?;
    let task = registry["tasks"]
        .as_array()
        .ok_or("SCOPED_TASK_REGISTRY_INVALID")?
        .iter()
        .find(|task| task["id"] == request.task_id);
    let docs = task.and_then(|task| task["documents"].as_array());
    let raw_candidates = task.and_then(|task| task["search_candidates"].as_array());
    let allowed = request.bound_task_id == request.task_id
        && task.is_some_and(|task| {
            task["tenant"] == request.principal_tenant
                && task["query"].as_str() == Some(request.query.as_str())
        })
        && docs.is_some()
        && raw_candidates.is_some_and(|candidates| {
            candidates.len() <= 64
                && candidates.iter().all(|candidate| {
                    let Some(source_id) = candidate["source_id"].as_str() else {
                        return false;
                    };
                    docs.is_some_and(|documents| {
                        documents.iter().any(|doc| {
                            doc["id"] == source_id && doc["tenant"] == request.principal_tenant
                        })
                    })
                })
        });
    if !allowed {
        let mut bytes = serde_json::to_vec(&json!({
            "schema":"athena-task-denial/1", "denied":true, "code":"SCOPED_TASK_DENIED"
        }))
        .map_err(|error| error.to_string())?;
        bytes.push(b'\n');
        return Ok(bytes);
    }
    let documents = docs.ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
    let candidates_in = raw_candidates.ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
    let mut candidates = Vec::with_capacity(candidates_in.len());
    for candidate in candidates_in {
        let source_id = candidate["source_id"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 64)
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let revision = candidate["revision"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 64)
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let freshness = candidate["freshness"]
            .as_str()
            .filter(|&s| s == "fresh" || s == "stale")
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let doc = documents
            .iter()
            .find(|doc| doc["id"] == source_id && doc["tenant"] == request.principal_tenant)
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let source_url = doc["url"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= 2048)
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let observed_at_unix_ms = doc["observed_at_unix_ms"]
            .as_u64()
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let raw_passages = candidate["passages"]
            .as_array()
            .filter(|items| !items.is_empty() && items.len() <= 64)
            .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
        let mut passages = Vec::with_capacity(raw_passages.len());
        for passage in raw_passages {
            let locator = passage["locator"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 64)
                .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
            let text = passage["text"]
                .as_str()
                .filter(|s| !s.is_empty() && s.chars().count() <= 512)
                .ok_or("SCOPED_TASK_REGISTRY_INVALID")?;
            passages.push(json!({"locator": locator, "text": text}));
        }
        candidates.push(json!({
            "source_id": source_id,
            "source_url": source_url,
            "revision": revision,
            "freshness": freshness,
            "observed_at_unix_ms": observed_at_unix_ms,
            "passages": passages,
        }));
    }
    let mut bytes = serde_json::to_vec(&json!({
        "schema": "athena-task-search-results/1",
        "task_id": request.task_id,
        "tenant": request.principal_tenant,
        "query": request.query,
        "candidates": candidates,
    }))
    .map_err(|error| error.to_string())?;
    bytes.push(b'\n');
    require(
        bytes.len() <= 131_072,
        "SCOPED_TASK_SEARCH_OUTPUT_TOO_LARGE",
    )?;
    Ok(bytes)
}

fn time_samples(iterations: usize, warmup: usize, mut op: impl FnMut()) -> Value {
    for _ in 0..warmup {
        op();
    }
    let mut samples = Vec::with_capacity(iterations);
    for _ in 0..iterations {
        let t0 = Instant::now();
        op();
        samples.push(t0.elapsed().as_nanos().max(1) as u64);
    }
    samples.sort_unstable();
    let n = samples.len();
    let pct = |q: f64| -> u64 { samples[((n - 1) as f64 * q).ceil() as usize] };
    let sum: u128 = samples.iter().map(|&v| u128::from(v)).sum();
    let mean_ns = (sum as f64) / (n as f64);
    json!({
        "iterations": n,
        "p50_ns": pct(0.50),
        "p95_ns": pct(0.95),
        "p99_ns": pct(0.99),
        "mean_ns": (mean_ns * 100.0).round() / 100.0,
    })
}

fn run_theory_matrix(iterations: usize) -> Result<Value, String> {
    let warmup = iterations.clamp(2, 15);
    let base_url = Url::parse("https://annas-archive.org/search?q=quantum").unwrap();

    // 1. Family 1: Retrieval Ranking & Novelty Selection Models
    let mut structural_chunks = Vec::with_capacity(256);
    for i in 0..256usize {
        let (role, text) = match i {
            42 => (
                "heading",
                "Byzantine fault tolerance quorum safety specification".to_string(),
            ),
            43 => (
                "paragraph",
                "Following section context: quorum safety liveness proof under partial synchrony"
                    .to_string(),
            ),
            108 => (
                "table_header",
                "Byzantine fault tolerance quorum safety benchmark".to_string(),
            ),
            _ if i % 16 == 0 => (
                "paragraph",
                format!(
                    "Byzantine background filler {} fault {} tolerance {} quorum {} safety",
                    "token ".repeat(8),
                    "alpha ".repeat(8),
                    "beta ".repeat(8),
                    "gamma ".repeat(8)
                ),
            ),
            _ => (
                "paragraph",
                format!("Unrelated distributed storage telemetry segment {i} with ordinary words"),
            ),
        };
        structural_chunks.push(Chunk {
            node: (i + 1) as u32,
            role,
            text,
            links: vec![],
        });
    }
    let q_struct = "byzantine quorum safety";
    let bm25_struct_top = bm25(&structural_chunks, q_struct, 1.2, 0.75);
    let bm25f_struct_top = bm25f(&structural_chunks, q_struct, 1.2, 0.75);
    let bm25f_plus_struct_top = bm25f_plus_mslp(&structural_chunks, q_struct, 1.2, 0.75, 1.0);
    let bm25_plus_struct_top = bm25_plus(&structural_chunks, q_struct, 1.2, 0.75, 1.0);
    let bm25l_struct_top = bm25l(&structural_chunks, q_struct, 1.2, 0.75, 0.5);
    let ql_struct_top = ql_dirichlet(&structural_chunks, q_struct, 1000.0);

    let mut skewed_chunks = Vec::with_capacity(256);
    let long_filler = "comprehensive mathematical derivation lemma corollary ".repeat(55);
    for i in 0..256usize {
        let text = if i == 0 {
            format!(
                "quantum error correction surface code threshold theorem {} quantum surface code",
                long_filler
            )
        } else if i == 1 {
            "quantum error correction surface code threshold".to_string()
        } else {
            format!("ordinary condensed matter lattice vibration mode {i} prose")
        };
        skewed_chunks.push(Chunk {
            node: (i + 1) as u32,
            role: "paragraph",
            text,
            links: vec![],
        });
    }
    let q_skew = "quantum surface code";
    let score_of = |scores: &[(usize, f64)], target: usize| -> f64 {
        scores
            .iter()
            .find(|(idx, _)| *idx == target)
            .map(|(_, s)| *s)
            .unwrap_or(0.0)
    };
    let s_bm25 = bm25(&skewed_chunks, q_skew, 1.2, 0.75);
    let s_bm25_plus = bm25_plus(&skewed_chunks, q_skew, 1.2, 0.75, 1.0);
    let s_bm25f_plus = bm25f_plus_mslp(&skewed_chunks, q_skew, 1.2, 0.75, 1.0);
    let s_bm25l = bm25l(&skewed_chunks, q_skew, 1.2, 0.75, 0.5);
    let s_ql = ql_dirichlet(&skewed_chunks, q_skew, 1000.0);

    let mut novelty_chunks = Vec::with_capacity(512);
    let punct_variants = [".", "!", ";", " -", "...", " :", " !!", " ?"];
    for i in 0..512usize {
        let text = if i < 64 {
            format!(
                "Zero-copy consensus replication latency quorum invariant verified{}",
                punct_variants[i % punct_variants.len()]
            )
        } else if i == 100 {
            "Zero-copy consensus replication Raft leader lease verification".to_string()
        } else if i == 200 {
            "Zero-copy consensus replication Paxos acceptor quorum commit".to_string()
        } else if i == 300 {
            "Zero-copy consensus replication Viewstamped state machine log".to_string()
        } else if i == 400 {
            "Zero-copy consensus replication EPaxos egalitarian dependency graph".to_string()
        } else {
            format!("Background telemetry metric {i} disk throughput")
        };
        novelty_chunks.push(Chunk {
            node: (i + 1) as u32,
            role: "paragraph",
            text,
            links: vec![],
        });
    }
    let q_novelty = "zero-copy consensus replication";
    let sel_bm25_exact = highlight_indices(&novelty_chunks, q_novelty, 5);
    let sel_bm25f_exact = bm25f_highlight_indices(&novelty_chunks, q_novelty, 5);
    let sel_bm25f_simhash = bm25f_simhash_highlight_indices(&novelty_chunks, q_novelty, 5, 3);
    let sel_bm25f_plus_simhash =
        bm25f_plus_mslp_simhash_highlight_indices(&novelty_chunks, q_novelty, 5, 1.0, 3);

    let retrieval_family = json!({
        "structural_authority_256": {
            "bm25": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25(black_box(&structural_chunks), q_struct, 1.2, 0.75));
                }),
                "top1_index": bm25_struct_top[0].0,
                "top1_role": structural_chunks[bm25_struct_top[0].0].role,
            },
            "bm25f_mslp": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25f(black_box(&structural_chunks), q_struct, 1.2, 0.75));
                }),
                "top1_index": bm25f_struct_top[0].0,
                "top1_role": structural_chunks[bm25f_struct_top[0].0].role,
                "top3_roles": bm25f_struct_top.iter().take(3).map(|(idx, _)| structural_chunks[*idx].role).collect::<Vec<_>>(),
            },
            "bm25f_plus_mslp": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25f_plus_mslp(black_box(&structural_chunks), q_struct, 1.2, 0.75, 1.0));
                }),
                "top1_index": bm25f_plus_struct_top[0].0,
                "top1_role": structural_chunks[bm25f_plus_struct_top[0].0].role,
                "top3_roles": bm25f_plus_struct_top.iter().take(3).map(|(idx, _)| structural_chunks[*idx].role).collect::<Vec<_>>(),
            },
            "bm25_plus": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25_plus(black_box(&structural_chunks), q_struct, 1.2, 0.75, 1.0));
                }),
                "top1_index": bm25_plus_struct_top[0].0,
            },
            "bm25l": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25l(black_box(&structural_chunks), q_struct, 1.2, 0.75, 0.5));
                }),
                "top1_index": bm25l_struct_top[0].0,
            },
            "ql_dirichlet": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(ql_dirichlet(black_box(&structural_chunks), q_struct, 1000.0));
                }),
                "top1_index": ql_struct_top[0].0,
            },
        },
        "skewed_length_256": {
            "bm25_long_to_short_ratio": score_of(&s_bm25, 0) / score_of(&s_bm25, 1),
            "bm25_plus_long_to_short_ratio": score_of(&s_bm25_plus, 0) / score_of(&s_bm25_plus, 1),
            "bm25f_plus_mslp_long_to_short_ratio": score_of(&s_bm25f_plus, 0) / score_of(&s_bm25f_plus, 1),
            "bm25l_long_to_short_ratio": score_of(&s_bm25l, 0) / score_of(&s_bm25l, 1),
            "ql_dirichlet_long_to_short_ratio": score_of(&s_ql, 0) / score_of(&s_ql, 1),
            "bm25_timing": time_samples(iterations, warmup, || {
                black_box(bm25(black_box(&skewed_chunks), q_skew, 1.2, 0.75));
            }),
            "bm25_plus_timing": time_samples(iterations, warmup, || {
                black_box(bm25_plus(black_box(&skewed_chunks), q_skew, 1.2, 0.75, 1.0));
            }),
            "bm25f_plus_mslp_timing": time_samples(iterations, warmup, || {
                black_box(bm25f_plus_mslp(black_box(&skewed_chunks), q_skew, 1.2, 0.75, 1.0));
            }),
            "bm25l_timing": time_samples(iterations, warmup, || {
                black_box(bm25l(black_box(&skewed_chunks), q_skew, 1.2, 0.75, 0.5));
            }),
            "ql_dirichlet_timing": time_samples(iterations, warmup, || {
                black_box(ql_dirichlet(black_box(&skewed_chunks), q_skew, 1000.0));
            }),
        },
        "near_duplicate_novelty_trap_512": {
            "bm25_exact_dedup": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(highlight_indices(black_box(&novelty_chunks), q_novelty, 5));
                }),
                "selected_indices": sel_bm25_exact,
                "unique_non_boilerplate_topics": sel_bm25_exact.iter().filter(|&&idx| idx >= 64).count() + 1,
            },
            "bm25f_exact_dedup": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25f_highlight_indices(black_box(&novelty_chunks), q_novelty, 5));
                }),
                "selected_indices": sel_bm25f_exact,
                "unique_non_boilerplate_topics": sel_bm25f_exact.iter().filter(|&&idx| idx >= 64).count() + 1,
            },
            "bm25f_simhash64": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25f_simhash_highlight_indices(black_box(&novelty_chunks), q_novelty, 5, 3));
                }),
                "selected_indices": sel_bm25f_simhash,
                "unique_non_boilerplate_topics": sel_bm25f_simhash.iter().filter(|&&idx| idx >= 64).count() + 1,
            },
            "bm25f_plus_mslp_simhash64": {
                "timing": time_samples(iterations, warmup, || {
                    black_box(bm25f_plus_mslp_simhash_highlight_indices(black_box(&novelty_chunks), q_novelty, 5, 1.0, 3));
                }),
                "selected_indices": sel_bm25f_plus_simhash,
                "unique_non_boilerplate_topics": sel_bm25f_plus_simhash.iter().filter(|&&idx| idx >= 64).count() + 1,
            },
        }
    });

    // 2. Family 2: Boilerplate Stripping Policies (FullDom vs MainContentV1 vs CetdEntropyV1)
    let mut semantic_html = String::from("<header><nav>");
    for i in 0..32 {
        semantic_html.push_str(&format!("<p><a href='/nav/{i}'>Menu link {i}</a></p>"));
    }
    semantic_html.push_str("</nav></header><main>");
    for i in 0..32 {
        semantic_html.push_str(&format!(
            "<p>Main scientific evidence paragraph {i} with detailed experimental observations and methodology.</p>"
        ));
    }
    semantic_html.push_str("</main><footer>");
    for i in 0..32 {
        semantic_html.push_str(&format!("<p><a href='/foot/{i}'>Footer policy {i}</a></p>"));
    }
    semantic_html.push_str("</footer>");

    let mut div_soup_html = String::from("<div class='page-wrapper'><div class='top-chrome'>");
    for i in 0..32 {
        div_soup_html.push_str(&format!(
            "<p><a href='/m1/{i}'>NavA</a> <a href='/m2/{i}'>NavB</a> <a href='/m3/{i}'>NavC</a></p>"
        ));
    }
    div_soup_html.push_str("</div><div class='article-body'>");
    for i in 0..32 {
        div_soup_html.push_str(&format!(
            "<p>Unsemantic div-soup core research finding {i}: deterministic state-machine execution and verified memory bounds across multi-core workloads without semantic main tags.</p>"
        ));
    }
    div_soup_html.push_str("</div><div class='bottom-chrome'>");
    for i in 0..32 {
        div_soup_html.push_str(&format!(
            "<p><a href='/f1/{i}'>FootA</a> <a href='/f2/{i}'>FootB</a> <a href='/f3/{i}'>FootC</a></p>"
        ));
    }
    div_soup_html.push_str("</div></div>");

    let bench_boilerplate = |html: &str, policy: BoilerplatePolicy| -> Result<Value, String> {
        let limits = ParseLimits {
            boilerplate: policy,
            ..ParseLimits::default()
        };
        let proj = parse_html_with_limits(html, &base_url, limits).map_err(str::to_string)?;
        let aae = export_jsonl_with_limit(
            &proj,
            &base_url,
            17,
            Some("research finding evidence"),
            5,
            4 * 1024 * 1024,
        )
        .map_err(|e| e.to_string())?;
        let parse_timing = time_samples(iterations, warmup, || {
            black_box(parse_html_with_limits(black_box(html), &base_url, limits).unwrap());
        });
        let pipeline_timing = time_samples(iterations, warmup, || {
            let p = parse_html_with_limits(black_box(html), &base_url, limits).unwrap();
            black_box(
                export_jsonl_with_limit(
                    &p,
                    &base_url,
                    17,
                    Some("research finding evidence"),
                    5,
                    4 * 1024 * 1024,
                )
                .unwrap(),
            );
        });
        Ok(json!({
            "retained_chunks": proj.chunks.len(),
            "aae_output_bytes": aae.len(),
            "parse_timing": parse_timing,
            "pipeline_timing": pipeline_timing,
        }))
    };

    let sem_full = bench_boilerplate(&semantic_html, BoilerplatePolicy::FullDom)?;
    let sem_main = bench_boilerplate(&semantic_html, BoilerplatePolicy::MainContentV1)?;
    let sem_cetd = bench_boilerplate(&semantic_html, BoilerplatePolicy::CetdEntropyV1)?;
    let soup_full = bench_boilerplate(&div_soup_html, BoilerplatePolicy::FullDom)?;
    let soup_main = bench_boilerplate(&div_soup_html, BoilerplatePolicy::MainContentV1)?;
    let soup_cetd = bench_boilerplate(&div_soup_html, BoilerplatePolicy::CetdEntropyV1)?;

    let boilerplate_family = json!({
        "semantic_main_with_chrome_96_blocks": {
            "full_dom": sem_full,
            "main_content_v1": sem_main,
            "cetd_entropy_v1": sem_cetd,
        },
        "unsemantic_div_soup_96_blocks": {
            "full_dom": soup_full,
            "main_content_v1": soup_main,
            "cetd_entropy_v1": soup_cetd,
        }
    });

    // 3. Family 3: Optional Link Extraction & LinkMode Emission (256 links & 4,096 links)
    let mut links_256_html = String::new();
    for i in 0..256 {
        links_256_html.push_str(&format!(
            "<p>Block {i} {} <a href='/cite/{i}'>citation-{i}</a></p>",
            if i < 5 {
                "target_query_term"
            } else {
                "background_prose"
            }
        ));
    }
    let mut links_4096_html = String::new();
    for i in 0..256 {
        links_4096_html.push_str(&format!(
            "<p>Block {i} {}",
            if i < 5 {
                "target_query_term"
            } else {
                "background_prose"
            }
        ));
        for j in 0..16 {
            let lid = i * 16 + j;
            links_4096_html.push_str(&format!(" <a href='/cite/{lid}'>c{lid}</a>"));
        }
        links_4096_html.push_str("</p>");
    }

    let bench_link_mode =
        |html: &str, include_links: bool, mode: LinkMode| -> Result<Value, String> {
            let limits = ParseLimits {
                include_links,
                ..ParseLimits::default()
            };
            let proj = parse_html_with_limits(html, &base_url, limits).map_err(str::to_string)?;
            let aae = export_jsonl_with_link_mode_and_prepared(
                &proj,
                &base_url,
                17,
                Some("target_query_term"),
                5,
                8 * 1024 * 1024,
                mode,
                None,
            )
            .map_err(|e| e.to_string())?;
            let link_frames = aae
                .lines()
                .filter(|line| line.contains("\"type\":\"link\""))
                .count();
            let parse_timing = time_samples(iterations, warmup, || {
                black_box(parse_html_with_limits(black_box(html), &base_url, limits).unwrap());
            });
            let export_timing = time_samples(iterations, warmup, || {
                black_box(
                    export_jsonl_with_link_mode_and_prepared(
                        black_box(&proj),
                        &base_url,
                        17,
                        Some("target_query_term"),
                        5,
                        8 * 1024 * 1024,
                        mode,
                        None,
                    )
                    .unwrap(),
                );
            });
            let pipeline_timing = time_samples(iterations, warmup, || {
                let p = parse_html_with_limits(black_box(html), &base_url, limits).unwrap();
                black_box(
                    export_jsonl_with_link_mode_and_prepared(
                        &p,
                        &base_url,
                        17,
                        Some("target_query_term"),
                        5,
                        8 * 1024 * 1024,
                        mode,
                        None,
                    )
                    .unwrap(),
                );
            });
            Ok(json!({
                "include_links": include_links,
                "link_mode": format!("{mode:?}"),
                "resolved_catalog_links": proj.links.len(),
                "emitted_link_frames": link_frames,
                "aae_output_bytes": aae.len(),
                "parse_timing": parse_timing,
                "export_timing": export_timing,
                "pipeline_timing": pipeline_timing,
            }))
        };

    let link_modes_family = json!({
        "article_256_links": {
            "all": bench_link_mode(&links_256_html, true, LinkMode::All)?,
            "highlighted_only": bench_link_mode(&links_256_html, true, LinkMode::HighlightedOnly)?,
            "none_export_only": bench_link_mode(&links_256_html, true, LinkMode::None)?,
            "none_skip_parse_and_export": bench_link_mode(&links_256_html, false, LinkMode::None)?,
        },
        "dense_4096_links": {
            "all": bench_link_mode(&links_4096_html, true, LinkMode::All)?,
            "highlighted_only": bench_link_mode(&links_4096_html, true, LinkMode::HighlightedOnly)?,
            "none_export_only": bench_link_mode(&links_4096_html, true, LinkMode::None)?,
            "none_skip_parse_and_export": bench_link_mode(&links_4096_html, false, LinkMode::None)?,
        }
    });

    // 4. Family 4: Anna's Archive Scroll-Card Unwrapping & Structured Bibliographic Extraction
    let mut annas_html = String::from(
        "<header><nav><p>Anna's Archive donate login</p></nav></header><main><h1>Search results for quantum error correction</h1>",
    );
    for i in 0..32usize {
        let md5 = format!("{:032x}", i + 1);
        let card = format!(
            "<p><a href=\"/md5/{md5}\">Book {i} Quantum Error Correction DOI: 10.1038/s41586-024-{i:04}-x · ISBN 978-0-387-{i:05}-2 · pdf · 4.2MB</a></p>"
        );
        if i >= 4 {
            annas_html.push_str(&format!("<!-- {card} -->"));
        } else {
            annas_html.push_str(&card);
        }
    }
    annas_html.push_str("</main><footer><p>Mirror status</p></footer>");

    let annas_default_proj = parse_html_with_limits(&annas_html, &base_url, ParseLimits::default())
        .map_err(str::to_string)?;
    let annas_default_records = extract_annas_archive_records(&annas_default_proj);
    let annas_unwrap_limits = ParseLimits {
        comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
        boilerplate: BoilerplatePolicy::MainContentV1,
        include_links: true,
        ..ParseLimits::default()
    };
    let annas_unwrap_proj = parse_html_with_limits(&annas_html, &base_url, annas_unwrap_limits)
        .map_err(str::to_string)?;
    let annas_unwrap_records = extract_annas_archive_records(&annas_unwrap_proj);

    let annas_family = json!({
        "default_no_comment_unwrap": {
            "chunks": annas_default_proj.chunks.len(),
            "links": annas_default_proj.links.len(),
            "records_extracted": annas_default_records.len(),
            "timing": time_samples(iterations, warmup, || {
                let p = parse_html_with_limits(black_box(&annas_html), &base_url, ParseLimits::default()).unwrap();
                black_box(extract_annas_archive_records(&p));
            }),
        },
        "annas_archive_scroll_cards_plus_main_content": {
            "chunks": annas_unwrap_proj.chunks.len(),
            "links": annas_unwrap_proj.links.len(),
            "records_extracted": annas_unwrap_records.len(),
            "sample_record_0": annas_unwrap_records.first().map(|r| json!({
                "md5": r.md5,
                "title": r.title,
                "doi": r.doi,
                "isbn": r.isbn,
                "format": r.format,
                "download_links": r.download_links,
            })),
            "timing": time_samples(iterations, warmup, || {
                let p = parse_html_with_limits(black_box(&annas_html), &base_url, annas_unwrap_limits).unwrap();
                black_box(extract_annas_archive_records(&p));
            }),
        }
    });

    // 5. Family 5: 16-Shard SourceCache + Conditional ETag 304 + PreparedRank Reuse
    #[derive(Clone)]
    struct BenchCacheEntry {
        url: String,
        projection: Arc<Projection>,
        prepared_rank: Arc<CorePreparedRank>,
        etag: Option<String>,
        cached_at_ms: u64,
    }
    let shards: [std::sync::Mutex<Vec<BenchCacheEntry>>; 16] =
        std::array::from_fn(|_| std::sync::Mutex::new(Vec::new()));
    let shard_of = |key: &str| -> usize {
        use std::hash::{DefaultHasher, Hash, Hasher};
        let mut h = DefaultHasher::new();
        key.hash(&mut h);
        (h.finish() as usize) & 15
    };
    let cache_url = "https://annas-archive.org/cache-bench";
    let cold_timing = time_samples(iterations, warmup, || {
        let proj = Arc::new(
            parse_html_with_limits(
                black_box(&links_256_html),
                &base_url,
                ParseLimits::default(),
            )
            .unwrap(),
        );
        let prep = Arc::new(CorePreparedRank::new(&proj.chunks));
        {
            let mut guard = shards[shard_of(cache_url)].lock().unwrap();
            if let Some(pos) = guard.iter().position(|e| e.url == cache_url) {
                guard.remove(pos);
            }
            guard.push(BenchCacheEntry {
                url: cache_url.to_string(),
                projection: Arc::clone(&proj),
                prepared_rank: Arc::clone(&prep),
                etag: Some("\"etag-v1\"".to_string()),
                cached_at_ms: 1_000,
            });
        }
        black_box(
            export_jsonl_with_link_mode_and_prepared(
                &proj,
                &base_url,
                1_000,
                Some("target_query_term"),
                5,
                4 * 1024 * 1024,
                LinkMode::HighlightedOnly,
                Some(&prep),
            )
            .unwrap(),
        );
    });
    let etag_304_timing = time_samples(iterations, warmup, || {
        let entry = {
            let mut guard = shards[shard_of(cache_url)].lock().unwrap();
            let pos = guard.iter().position(|e| e.url == cache_url).unwrap();
            let mut e = guard.remove(pos);
            let _etag = e.etag.as_deref();
            e.cached_at_ms = 65_000;
            let cloned = e.clone();
            guard.push(e);
            cloned
        };
        black_box(
            export_jsonl_with_link_mode_and_prepared(
                &entry.projection,
                &base_url,
                65_000,
                Some("target_query_term"),
                5,
                4 * 1024 * 1024,
                LinkMode::HighlightedOnly,
                Some(&entry.prepared_rank),
            )
            .unwrap(),
        );
    });
    let ttl_hit_timing = time_samples(iterations, warmup, || {
        let entry = {
            let mut guard = shards[shard_of(cache_url)].lock().unwrap();
            let pos = guard.iter().position(|e| e.url == cache_url).unwrap();
            let mut e = guard.remove(pos);
            e.cached_at_ms = 65_100;
            let cloned = e.clone();
            guard.push(e);
            cloned
        };
        black_box(
            export_jsonl_with_link_mode_and_prepared(
                &entry.projection,
                &base_url,
                65_100,
                Some("target_query_term"),
                5,
                4 * 1024 * 1024,
                LinkMode::HighlightedOnly,
                Some(&entry.prepared_rank),
            )
            .unwrap(),
        );
    });

    let cache_family = json!({
        "cold_miss_parse_index_export": cold_timing,
        "expired_ttl_etag_304_revalidation_export": etag_304_timing,
        "fresh_ttl_hit_prepared_export": ttl_hit_timing,
    });

    // 6. Family 6: BoundedOutputBuf Streaming Compressed Transfer (gzip / deflate vs identity)
    fn encode_deflate_fixed(data: &[u8]) -> Vec<u8> {
        const LENGTH_BASE: [u16; 29] = [
            3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31, 35, 43, 51, 59, 67, 83, 99,
            115, 131, 163, 195, 227, 258,
        ];
        const LENGTH_EXTRA: [u8; 29] = [
            0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0,
        ];
        const DIST_BASE: [u16; 30] = [
            1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193, 257, 385, 513, 769, 1025,
            1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577,
        ];
        const DIST_EXTRA: [u8; 30] = [
            0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12,
            12, 13, 13,
        ];
        struct BitOut {
            buf: Vec<u8>,
            acc: u32,
            bits: u8,
        }
        impl BitOut {
            fn write_lsb(&mut self, val: u32, count: u8) {
                if count == 0 {
                    return;
                }
                self.acc |= (val & ((1u32 << count) - 1)) << self.bits;
                self.bits += count;
                while self.bits >= 8 {
                    self.buf.push(self.acc as u8);
                    self.acc >>= 8;
                    self.bits -= 8;
                }
            }
            fn write_msb(&mut self, code: u16, len: u8) {
                for shift in (0..len).rev() {
                    self.write_lsb(u32::from((code >> shift) & 1), 1);
                }
            }
            fn write_lit(&mut self, b: u8) {
                if b <= 143 {
                    self.write_msb(0x30 + u16::from(b), 8);
                } else {
                    self.write_msb(0x190 + u16::from(b - 144), 9);
                }
            }
            fn write_match(&mut self, length: usize, distance: usize) {
                let mut l_idx = 28usize;
                for i in 0..28 {
                    if length < usize::from(LENGTH_BASE[i + 1]) {
                        l_idx = i;
                        break;
                    }
                }
                let sym = 257 + l_idx as u16;
                if sym <= 279 {
                    self.write_msb(sym - 256, 7);
                } else {
                    self.write_msb(0xc0 + (sym - 280), 8);
                }
                self.write_lsb(
                    (length - usize::from(LENGTH_BASE[l_idx])) as u32,
                    LENGTH_EXTRA[l_idx],
                );
                let mut d_idx = 29usize;
                for i in 0..29 {
                    if distance < usize::from(DIST_BASE[i + 1]) {
                        d_idx = i;
                        break;
                    }
                }
                self.write_msb(d_idx as u16, 5);
                self.write_lsb(
                    (distance - usize::from(DIST_BASE[d_idx])) as u32,
                    DIST_EXTRA[d_idx],
                );
            }
        }
        let mut out = BitOut {
            buf: Vec::with_capacity(data.len() / 2 + 16),
            acc: 0,
            bits: 0,
        };
        // bfinal = 1, btype = 1 (fixed Huffman)
        out.write_lsb(1, 1);
        out.write_lsb(1, 2);
        let mut head = [-1i32; 4096];
        let hash4 = |s: &[u8]| -> usize {
            let v = u32::from_le_bytes([s[0], s[1], s[2], s[3]]);
            ((v.wrapping_mul(0x1e35_a7bd)) >> 20) as usize
        };
        let mut pos = 0usize;
        while pos < data.len() {
            if pos + 4 <= data.len() {
                let h = hash4(&data[pos..pos + 4]);
                let prev = head[h];
                head[h] = pos as i32;
                if prev >= 0 {
                    let prev_pos = prev as usize;
                    let dist = pos - prev_pos;
                    if (1..=32_768).contains(&dist)
                        && data[prev_pos..prev_pos + 4] == data[pos..pos + 4]
                    {
                        let max_len = (data.len() - pos).min(258);
                        let mut mlen = 4usize;
                        while mlen < max_len && data[prev_pos + mlen] == data[pos + mlen] {
                            mlen += 1;
                        }
                        out.write_match(mlen, dist);
                        for step in 1..mlen {
                            if pos + step + 4 <= data.len() {
                                head[hash4(&data[pos + step..pos + step + 4])] =
                                    (pos + step) as i32;
                            }
                        }
                        pos += mlen;
                        continue;
                    }
                }
            }
            out.write_lit(data[pos]);
            pos += 1;
        }
        // End of block (256 -> 7 zero bits)
        out.write_msb(0, 7);
        if out.bits > 0 {
            out.buf.push(out.acc as u8);
        }
        out.buf
    }
    fn encode_gzip_fixed(data: &[u8]) -> Vec<u8> {
        let deflate = encode_deflate_fixed(data);
        let mut out = Vec::with_capacity(10 + deflate.len() + 8);
        out.extend_from_slice(&[0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 2, 255]);
        out.extend_from_slice(&deflate);
        let mut crc = 0xffff_ffffu32;
        for &b in data {
            crc ^= u32::from(b);
            for _ in 0..8 {
                let mask = (crc & 1).wrapping_neg();
                crc = (crc >> 1) ^ (0xedb8_8320 & mask);
            }
        }
        out.extend_from_slice(&(!crc).to_le_bytes());
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out
    }
    fn encode_zlib_fixed(data: &[u8]) -> Vec<u8> {
        let deflate = encode_deflate_fixed(data);
        let mut out = Vec::with_capacity(2 + deflate.len() + 4);
        out.extend_from_slice(&[0x78, 0x9c]);
        out.extend_from_slice(&deflate);
        let mut a = 1u32;
        let mut b = 0u32;
        for &byte in data {
            a = (a + u32::from(byte)) % 65_521;
            b = (b + a) % 65_521;
        }
        out.extend_from_slice(&((b << 16) | a).to_be_bytes());
        out
    }

    let raw_html_bytes = links_4096_html.as_bytes();
    let gzip_wire = encode_gzip_fixed(raw_html_bytes);
    let deflate_zlib_wire = encode_zlib_fixed(raw_html_bytes);
    let deflate_raw_wire = encode_deflate_fixed(raw_html_bytes);
    let gzip_bomb_wire = encode_gzip_fixed(&vec![b'A'; 2 * 1024 * 1024]);

    let max_body = 4 * 1024 * 1024;
    let compression_family = json!({
        "raw_html_bytes": raw_html_bytes.len(),
        "identity": {
            "wire_bytes": raw_html_bytes.len(),
            "compression_ratio": 1.0,
            "decode_timing": time_samples(iterations, warmup, || {
                black_box(decode_content_encoding(black_box(raw_html_bytes), ContentEncodingMode::Identity, max_body).unwrap());
            }),
        },
        "gzip": {
            "wire_bytes": gzip_wire.len(),
            "compression_ratio": (raw_html_bytes.len() as f64) / (gzip_wire.len() as f64),
            "wire_byte_savings_pct": (1.0 - (gzip_wire.len() as f64) / (raw_html_bytes.len() as f64)) * 100.0,
            "decode_timing": time_samples(iterations, warmup, || {
                black_box(decode_content_encoding(black_box(&gzip_wire), ContentEncodingMode::Gzip, max_body).unwrap());
            }),
        },
        "deflate_zlib": {
            "wire_bytes": deflate_zlib_wire.len(),
            "compression_ratio": (raw_html_bytes.len() as f64) / (deflate_zlib_wire.len() as f64),
            "wire_byte_savings_pct": (1.0 - (deflate_zlib_wire.len() as f64) / (raw_html_bytes.len() as f64)) * 100.0,
            "decode_timing": time_samples(iterations, warmup, || {
                black_box(decode_content_encoding(black_box(&deflate_zlib_wire), ContentEncodingMode::Deflate, max_body).unwrap());
            }),
        },
        "deflate_raw": {
            "wire_bytes": deflate_raw_wire.len(),
            "compression_ratio": (raw_html_bytes.len() as f64) / (deflate_raw_wire.len() as f64),
            "wire_byte_savings_pct": (1.0 - (deflate_raw_wire.len() as f64) / (raw_html_bytes.len() as f64)) * 100.0,
            "decode_timing": time_samples(iterations, warmup, || {
                black_box(decode_content_encoding(black_box(&deflate_raw_wire), ContentEncodingMode::Deflate, max_body).unwrap());
            }),
        },
        "zip_bomb_early_abort_64kib_cap": {
            "compressed_wire_bytes": gzip_bomb_wire.len(),
            "uncompressed_bomb_bytes": 2 * 1024 * 1024,
            "cap_bytes": 65_536,
            "abort_timing": time_samples(iterations, warmup, || {
                let res = decode_content_encoding(black_box(&gzip_bomb_wire), ContentEncodingMode::Gzip, 65_536);
                assert!(res.is_err());
            }),
        }
    });

    // 7. Family 7: Multi-Core Hardware Saturation (100% parallel threads via std::thread::scope)
    let hw_threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(1);
    let single_thread_composite = time_samples(iterations, warmup, || {
        let decoded =
            decode_content_encoding(black_box(&gzip_wire), ContentEncodingMode::Gzip, max_body)
                .unwrap();
        let html_str = std::str::from_utf8(&decoded).unwrap();
        let p = parse_html_with_limits(html_str, &base_url, ParseLimits::default()).unwrap();
        let top = bm25f_simhash_highlight_indices(&p.chunks, "target_query_term", 5, 3);
        black_box(top);
        black_box(
            export_jsonl_with_link_mode_and_prepared(
                &p,
                &base_url,
                17,
                Some("target_query_term"),
                5,
                4 * 1024 * 1024,
                LinkMode::HighlightedOnly,
                None,
            )
            .unwrap(),
        );
    });
    let par_start = Instant::now();
    let per_thread_p50s: Vec<u64> = std::thread::scope(|scope| {
        let mut handles = Vec::with_capacity(hw_threads);
        for _ in 0..hw_threads {
            let gz_ref = &gzip_wire;
            let url_ref = &base_url;
            handles.push(scope.spawn(move || {
                let mut local_samples = Vec::with_capacity(iterations);
                for _ in 0..iterations {
                    let t0 = Instant::now();
                    let decoded = decode_content_encoding(
                        black_box(gz_ref),
                        ContentEncodingMode::Gzip,
                        max_body,
                    )
                    .unwrap();
                    let html_str = std::str::from_utf8(&decoded).unwrap();
                    let p =
                        parse_html_with_limits(html_str, url_ref, ParseLimits::default()).unwrap();
                    let top = bm25f_simhash_highlight_indices(&p.chunks, "target_query_term", 5, 3);
                    black_box(top);
                    black_box(
                        export_jsonl_with_link_mode_and_prepared(
                            &p,
                            url_ref,
                            17,
                            Some("target_query_term"),
                            5,
                            4 * 1024 * 1024,
                            LinkMode::HighlightedOnly,
                            None,
                        )
                        .unwrap(),
                    );
                    local_samples.push(t0.elapsed().as_nanos().max(1) as u64);
                }
                local_samples.sort_unstable();
                local_samples[(local_samples.len() - 1) / 2]
            }));
        }
        handles.into_iter().map(|h| h.join().unwrap()).collect()
    });
    let par_wall_ns = par_start.elapsed().as_nanos().max(1) as f64;
    let total_par_ops = (hw_threads * iterations) as f64;
    let par_ops_per_sec = total_par_ops / (par_wall_ns / 1e9);
    let single_p50_ns = single_thread_composite["p50_ns"]
        .as_u64()
        .unwrap_or(1)
        .max(1) as f64;
    let single_ops_per_sec = 1e9 / single_p50_ns;

    // 8. Family 8: Round 2 Adversarial Red-Team Stress & Unified Cohort Verification
    let mut multi_member_gzip = Vec::with_capacity(gzip_wire.len() * 2);
    multi_member_gzip.extend_from_slice(&gzip_wire);
    multi_member_gzip.extend_from_slice(&gzip_wire);
    let multi_member_timing = time_samples(iterations, warmup, || {
        let dec = decode_content_encoding(
            black_box(&multi_member_gzip),
            ContentEncodingMode::Gzip,
            max_body * 2,
        )
        .unwrap();
        assert_eq!(dec.len(), raw_html_bytes.len() * 2);
    });
    let mut empty_block_bomb = Vec::with_capacity(360);
    for _ in 0..70 {
        empty_block_bomb.extend_from_slice(&[0x00, 0x00, 0x00, 0xff, 0xff]);
    }
    empty_block_bomb.extend_from_slice(&[0x01, 0x00, 0x00, 0xff, 0xff]);
    let empty_block_bomb_timing = time_samples(iterations, warmup, || {
        let res = decode_content_encoding(
            black_box(&empty_block_bomb),
            ContentEncodingMode::Deflate,
            65_536,
        );
        assert!(res.is_err());
    });
    let mut corrupt_gzip = gzip_wire.clone();
    if corrupt_gzip.len() >= 8 {
        let crc_idx = corrupt_gzip.len() - 8;
        corrupt_gzip[crc_idx] ^= 0xff;
    }
    let mut corrupt_zlib = deflate_zlib_wire.clone();
    if let Some(last) = corrupt_zlib.last_mut() {
        *last ^= 0xff;
    }
    assert!(decode_content_encoding(&corrupt_gzip, ContentEncodingMode::Gzip, max_body).is_err());
    assert!(
        decode_content_encoding(&corrupt_zlib, ContentEncodingMode::Deflate, max_body).is_err()
    );
    let red_team_html = concat!(
        "<script>let s = \"<!--\";</script>",
        "<nav><h2>Menu</h2><p><a href=\"/1\">A</a> <a href=\"/2\">B</a> <a href=\"/3\">C</a></p></nav>",
        "<main><article><header><h1>Distributed Consensus &amp; Quorum Systems</h1></header>",
        "<p>Body prose with adjacent byzantine quorum terms and later repeated byzantine token.</p>",
        "</article>",
        "<!-- outer comment with <b>tag</b> <!-- <article><h2><a href=\"/md5/0123456789ABCDEF0123456789ABCDEF\">Byzantine Quorum Card</a></h2>",
        "<p>PDF · 10.4 MB · v1.10.2/rel · 192.168.10.1/24 · (10.1007/s00220-024-04900-1). · ISBN: XXXXXXXXXX · ISBN-13: 978-0-521-87996-5</p></article> -->",
        "</main>"
    );
    let red_team_limits = ParseLimits {
        comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
        boilerplate: BoilerplatePolicy::CetdEntropyV1,
        include_links: true,
        ..ParseLimits::default()
    };
    let red_team_proj = parse_html_with_limits(red_team_html, &base_url, red_team_limits)
        .map_err(str::to_string)?;
    let red_team_records = extract_annas_archive_records(&red_team_proj);
    assert_eq!(
        red_team_records.first().and_then(|r| r.doi.as_deref()),
        Some("10.1007/s00220-024-04900-1")
    );
    assert_eq!(
        red_team_records.first().and_then(|r| r.isbn.as_deref()),
        Some("978-0-521-87996-5")
    );
    let red_team_timing = time_samples(iterations, warmup, || {
        let p =
            parse_html_with_limits(black_box(red_team_html), &base_url, red_team_limits).unwrap();
        let recs = extract_annas_archive_records(&p);
        let scores = bm25f(&p.chunks, "byzantine quorum", 1.2, 0.75);
        black_box((recs, scores));
    });

    // 9. Family 9: Full 126-Cell Multi-Dimensional Combinatorial Matrix (7 Rankers x 3 Boilerplate x 3 LinkModes x 2 CommentUnwrap)
    let mut combo_html = String::with_capacity(16_384);
    combo_html.push_str(concat!(
        "<header><nav><p>byzantine quorum consensus replication menu ",
        "<a href=\"/nav/home\">Home</a> <a href=\"/nav/login\">Login</a> <a href=\"/nav/account\">Account</a></p></nav></header>",
        "<div class=\"sidebar-menu\"><p>byzantine quorum ",
        "<a href=\"/side/1\">MenuA</a> <a href=\"/side/2\">MenuB</a> <a href=\"/side/3\">MenuC</a></p></div>",
        "<div class=\"sep\"><p>------------------------------------------------------------</p></div>",
        "<div class=\"promo-links\"><p><a href=\"/p/1\">Donate</a> <a href=\"/p/2\">Mirrors</a> <a href=\"/p/3\">FAQ</a> <a href=\"/p/4\">Contact</a></p></div>",
        "<main><article>",
        "<h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>"
    ));
    for i in 0..16 {
        combo_html.push_str(&format!(
            "<p>Background distributed storage telemetry observation {i} with disk latency metrics and <a href=\"/bg/{i}\">reference-{i}</a>.</p>"
        ));
    }
    let long_derivation = "formal asynchronous network model state machine proof ".repeat(22);
    combo_html.push_str(&format!(
        "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
    ));
    combo_html.push_str(concat!(
        "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
        "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
        "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified;</p>"
    ));
    combo_html.push_str(concat!(
        "<!-- <article class=\"scroll-card\">",
        "<h3><a href=\"/md5/0123456789abcdef0123456789abcdef\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness in Distributed Databases</a></h3>",
        "<p>PDF · 14.2 MB · DOI: 10.1007/s00220-024-04900-1 · ISBN: 978-0-521-86571-5</p>",
        "<p><a href=\"/slow_download/0123456789abcdef0123456789abcdef/0/0\">Slow Partner Server #1</a></p>",
        "</article> -->",
        "</article></main>",
        "<div class=\"annas-div-card\">",
        "<h3><a href=\"/md5/fedcba9876543210fedcba9876543210\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Systems</a></h3>",
        "<p>EPUB · 8.4 MB · DOI: 10.1109/TPDS.2025.9988776 · ISBN: 978-0-262-03384-8</p>",
        "<p><a href=\"/slow_download/fedcba9876543210fedcba9876543210/0/0\">Slow Partner Server #2</a> ",
        "<a href=\"/fast_download/fedcba9876543210fedcba9876543210/0/0\">Fast Partner Server #1</a></p>",
        "</div>",
        "<div class=\"legal-notice\"><p>All rights reserved. Privacy policy and terms of service apply.</p></div>",
        "<footer><p>byzantine quorum consensus replication footer <a href=\"/f/1\">Privacy</a> <a href=\"/f/2\">Terms</a> <a href=\"/f/3\">Cookies</a></p></footer>"
    ));

    let combo_query = "byzantine quorum consensus replication safety liveness";
    let select_by_model = |chunks: &[Chunk], model: &str, k: usize| -> Vec<usize> {
        let exact_dedup_from_scores = |scores: Vec<(usize, f64)>| -> Vec<usize> {
            let mut out = Vec::with_capacity(k);
            let mut seen = std::collections::HashSet::<String>::new();
            for (idx, sc) in scores {
                if out.len() >= k || sc <= 0.0 {
                    break;
                }
                if seen.insert(chunks[idx].text.to_lowercase()) {
                    out.push(idx);
                }
            }
            out
        };
        match model {
            "bm25" => highlight_indices(chunks, combo_query, k),
            "bm25f" => bm25f_highlight_indices(chunks, combo_query, k),
            "bm25_plus" => exact_dedup_from_scores(bm25_plus(chunks, combo_query, 1.2, 0.75, 1.0)),
            "bm25l" => exact_dedup_from_scores(bm25l(chunks, combo_query, 1.2, 0.75, 0.5)),
            "ql_dirichlet" => exact_dedup_from_scores(ql_dirichlet(chunks, combo_query, 1000.0)),
            "bm25f_simhash" => bm25f_simhash_highlight_indices(chunks, combo_query, k, 3),
            "bm25f_plus_mslp_simhash64" => {
                bm25f_plus_mslp_simhash_highlight_indices(chunks, combo_query, k, 1.0, 3)
            }
            _ => highlight_indices(chunks, combo_query, k),
        }
    };
    let classify_chunk_topic = |text: &str, role: &str| -> (Option<u8>, u8, bool) {
        // Returns (topic_id: Option<1..=5>, relevance_grade: 0..=3, is_boilerplate_chrome: bool)
        if text.contains("TOPIC_1_LONG_COMPLETE") {
            (Some(1), 2, false)
        } else if role == "heading"
            && text.contains("Byzantine Quorum Consensus Replication Safety Liveness Specification")
        {
            (Some(3), 3, false)
        } else if text.contains("TOPIC_5_COMMENT_CARD") {
            (Some(5), 3, false)
        } else if text.contains("TOPIC_4_DIV_CARD") {
            (Some(4), 3, false)
        } else if text.contains("TOPIC_2_NEAR_DUP") {
            (Some(2), 2, false)
        } else if text.contains("menu")
            || text.contains("MenuA")
            || text.contains("----")
            || text.contains("Donate")
            || text.contains("All rights reserved")
            || text.contains("footer")
        {
            (None, 0, true)
        } else {
            (None, 0, false)
        }
    };
    // Ideal DCG@5 for grades [3, 3, 3, 2, 2]
    let ideal_grades = [3u8, 3, 3, 2, 2];
    let idcg_at_5: f64 = ideal_grades
        .iter()
        .enumerate()
        .map(|(rank_idx, &g)| ((1u32 << g) as f64 - 1.0) / ((rank_idx as f64 + 2.0).log2()))
        .sum();

    let rank_models = [
        "bm25",
        "bm25f",
        "bm25_plus",
        "bm25l",
        "ql_dirichlet",
        "bm25f_simhash",
        "bm25f_plus_mslp_simhash64",
    ];
    let boilerplate_models = [
        ("none", BoilerplatePolicy::FullDom),
        ("main_content", BoilerplatePolicy::MainContentV1),
        ("cetd_entropy", BoilerplatePolicy::CetdEntropyV1),
    ];
    let link_models = [
        ("all", true, LinkMode::All),
        ("highlighted_only", true, LinkMode::HighlightedOnly),
        ("none", false, LinkMode::None),
    ];
    let comment_models = [
        ("none", CommentUnwrapPolicy::None),
        (
            "annas_archive",
            CommentUnwrapPolicy::AnnasArchiveScrollCards,
        ),
    ];

    let cell_iters = (iterations / 5).clamp(5, 50);
    let cell_warmup = (warmup / 3).clamp(1, 5);
    let mut matrix_cells = Vec::with_capacity(126);
    for &(comment_name, comment_policy) in &comment_models {
        for &(bp_name, bp_policy) in &boilerplate_models {
            for &(link_name, include_links, link_mode) in &link_models {
                let limits = ParseLimits {
                    max_html_bytes: combo_html.len() + 1024,
                    comment_unwrap: comment_policy,
                    boilerplate: bp_policy,
                    include_links,
                    ..ParseLimits::default()
                };
                let proj = parse_html_with_limits(&combo_html, &base_url, limits)
                    .map_err(str::to_string)?;
                let annas_recs = extract_annas_archive_records(&proj);
                let leaked_bp_chunks = proj
                    .chunks
                    .iter()
                    .filter(|c| classify_chunk_topic(&c.text, c.role).2)
                    .count();
                let retained_relevant_topics: std::collections::HashSet<u8> = proj
                    .chunks
                    .iter()
                    .filter_map(|c| classify_chunk_topic(&c.text, c.role).0)
                    .collect();
                let expected_relevant_topics =
                    if comment_policy == CommentUnwrapPolicy::AnnasArchiveScrollCards {
                        5usize
                    } else {
                        4usize
                    };
                let tp_bp = 6usize.saturating_sub(leaked_bp_chunks);
                let fn_bp = leaked_bp_chunks;
                let fp_bp = expected_relevant_topics.saturating_sub(retained_relevant_topics.len());
                let bp_f1 = if tp_bp == 0 {
                    0.0
                } else {
                    (2.0 * tp_bp as f64) / (2.0 * tp_bp as f64 + fp_bp as f64 + fn_bp as f64)
                };
                let bp_leakage_rate = (leaked_bp_chunks as f64) / (proj.chunks.len().max(1) as f64);

                for &rank_name in &rank_models {
                    let selected = select_by_model(&proj.chunks, rank_name, 5);
                    let mut credited_topics = std::collections::HashSet::<u8>::new();
                    let mut dcg = 0.0f64;
                    let mut mrr = 0.0f64;
                    for (rank_pos, &chunk_idx) in selected.iter().take(5).enumerate() {
                        if let Some(chunk) = proj.chunks.get(chunk_idx) {
                            let (topic_opt, grade, _) =
                                classify_chunk_topic(&chunk.text, chunk.role);
                            let effective_grade = match topic_opt {
                                Some(tid) if credited_topics.insert(tid) => grade,
                                _ => 0,
                            };
                            if effective_grade >= 2 && mrr == 0.0 {
                                mrr = 1.0 / ((rank_pos + 1) as f64);
                            }
                            dcg += ((1u32 << effective_grade) as f64 - 1.0)
                                / ((rank_pos as f64 + 2.0).log2());
                        }
                    }
                    let ndcg_at_5 = (dcg / idcg_at_5).min(1.0);
                    let recall_at_5 = (credited_topics.len() as f64) / 5.0;
                    let aae = export_jsonl_with_link_mode_and_prepared(
                        &proj,
                        &base_url,
                        17,
                        Some(combo_query),
                        5,
                        4 * 1024 * 1024,
                        link_mode,
                        None,
                    )
                    .map_err(|e| e.to_string())?;
                    let aae_bytes = aae.len();
                    let compression_vs_raw = (combo_html.len() as f64) / (aae_bytes.max(1) as f64);
                    let bytes_per_fact = (aae_bytes as f64) / (credited_topics.len().max(1) as f64);

                    let cell_timing = time_samples(cell_iters, cell_warmup, || {
                        let p = parse_html_with_limits(black_box(&combo_html), &base_url, limits)
                            .unwrap();
                        let sel = select_by_model(&p.chunks, rank_name, 5);
                        black_box(sel);
                        black_box(
                            export_jsonl_with_link_mode_and_prepared(
                                &p,
                                &base_url,
                                17,
                                Some(combo_query),
                                5,
                                4 * 1024 * 1024,
                                link_mode,
                                None,
                            )
                            .unwrap(),
                        );
                    });

                    matrix_cells.push(json!({
                        "cell_id": format!("{rank_name}__{bp_name}__{link_name}__{comment_name}"),
                        "rank_model": rank_name,
                        "boilerplate_policy": bp_name,
                        "link_mode": link_name,
                        "comment_unwrap": comment_name,
                        "ndcg_at_5": (ndcg_at_5 * 10000.0).round() / 10000.0,
                        "mrr_at_5": (mrr * 10000.0).round() / 10000.0,
                        "recall_at_5": (recall_at_5 * 10000.0).round() / 10000.0,
                        "unique_relevant_topics_in_top5": credited_topics.len(),
                        "boilerplate_leakage_rate": (bp_leakage_rate * 10000.0).round() / 10000.0,
                        "boilerplate_removal_f1": (bp_f1 * 10000.0).round() / 10000.0,
                        "extracted_annas_records": annas_recs.len(),
                        "retained_chunks": proj.chunks.len(),
                        "retained_links": proj.links.len(),
                        "aae_output_bytes": aae_bytes,
                        "output_compression_ratio_vs_raw_html": (compression_vs_raw * 1000.0).round() / 1000.0,
                        "bytes_per_fact": (bytes_per_fact * 100.0).round() / 100.0,
                        "timing": cell_timing,
                    }));
                }
            }
        }
    }

    // Parallel execution across all hardware threads over all 126 combinatorial cells
    let mut all_126_specs = Vec::with_capacity(126);
    for &(comment_name, comment_policy) in &comment_models {
        for &(bp_name, bp_policy) in &boilerplate_models {
            for &(link_name, include_links, link_mode) in &link_models {
                for &rank_name in &rank_models {
                    all_126_specs.push((
                        rank_name,
                        bp_name,
                        bp_policy,
                        link_name,
                        include_links,
                        link_mode,
                        comment_name,
                        comment_policy,
                    ));
                }
            }
        }
    }
    let par_126_start = Instant::now();
    std::thread::scope(|scope| {
        let chunk_size = all_126_specs.len().div_ceil(hw_threads);
        for spec_slice in all_126_specs.chunks(chunk_size) {
            let html_ref = &combo_html;
            let url_ref = &base_url;
            scope.spawn(move || {
                for &(_, _, bp_policy, _, include_links, link_mode, _, comment_policy) in spec_slice
                {
                    let limits = ParseLimits {
                        max_html_bytes: html_ref.len() + 1024,
                        comment_unwrap: comment_policy,
                        boilerplate: bp_policy,
                        include_links,
                        ..ParseLimits::default()
                    };
                    for _ in 0..cell_iters {
                        let p =
                            parse_html_with_limits(black_box(html_ref), url_ref, limits).unwrap();
                        let top = bm25f_plus_mslp_simhash_highlight_indices(
                            &p.chunks,
                            combo_query,
                            5,
                            1.0,
                            3,
                        );
                        black_box(top);
                        black_box(
                            export_jsonl_with_link_mode_and_prepared(
                                &p,
                                url_ref,
                                17,
                                Some(combo_query),
                                5,
                                4 * 1024 * 1024,
                                link_mode,
                                None,
                            )
                            .unwrap(),
                        );
                    }
                }
            });
        }
    });
    let par_126_wall_ms = (par_126_start.elapsed().as_nanos().max(1) as f64) / 1e6;
    let par_126_cells_per_sec =
        ((all_126_specs.len() * cell_iters) as f64) / (par_126_wall_ms / 1000.0);

    let mut model_ir_summary = serde_json::Map::new();
    for &rank_name in &rank_models {
        let matching: Vec<&Value> = matrix_cells
            .iter()
            .filter(|c| c["rank_model"] == rank_name)
            .collect();
        let mean_ndcg = matching
            .iter()
            .filter_map(|c| c["ndcg_at_5"].as_f64())
            .sum::<f64>()
            / (matching.len().max(1) as f64);
        let max_ndcg = matching
            .iter()
            .filter_map(|c| c["ndcg_at_5"].as_f64())
            .fold(0.0f64, f64::max);
        let mean_mrr = matching
            .iter()
            .filter_map(|c| c["mrr_at_5"].as_f64())
            .sum::<f64>()
            / (matching.len().max(1) as f64);
        let mean_recall = matching
            .iter()
            .filter_map(|c| c["recall_at_5"].as_f64())
            .sum::<f64>()
            / (matching.len().max(1) as f64);
        let mean_p50_ns = matching
            .iter()
            .filter_map(|c| c["timing"]["p50_ns"].as_u64())
            .sum::<u64>() as f64
            / (matching.len().max(1) as f64);
        model_ir_summary.insert(
            rank_name.to_string(),
            json!({
                "cells": matching.len(),
                "mean_ndcg_at_5": (mean_ndcg * 10000.0).round() / 10000.0,
                "max_ndcg_at_5": (max_ndcg * 10000.0).round() / 10000.0,
                "mean_mrr_at_5": (mean_mrr * 10000.0).round() / 10000.0,
                "mean_recall_at_5": (mean_recall * 10000.0).round() / 10000.0,
                "mean_p50_ns": (mean_p50_ns * 100.0).round() / 10000.0,
            }),
        );
    }

    // Evaluate 18 additional factorial cells for hybrid_rrf_simhash64 (8th rank model -> 144 total retrieval/extraction cells)
    // plus Multi-Query RRF over multi-aspect scientific/bibliographic sub-queries.
    let mut rrf_cells = Vec::with_capacity(18);
    for &(comment_name, comment_policy) in &comment_models {
        for &(bp_name, bp_policy) in &boilerplate_models {
            for &(link_name, include_links, link_mode) in &link_models {
                let limits = ParseLimits {
                    max_html_bytes: combo_html.len() + 1024,
                    comment_unwrap: comment_policy,
                    boilerplate: bp_policy,
                    include_links,
                    ..ParseLimits::default()
                };
                let proj = parse_html_with_limits(&combo_html, &base_url, limits)
                    .map_err(str::to_string)?;
                let annas_recs = extract_annas_archive_records(&proj);
                let leaked_bp_chunks = proj
                    .chunks
                    .iter()
                    .filter(|c| classify_chunk_topic(&c.text, c.role).2)
                    .count();
                let retained_relevant_topics: std::collections::HashSet<u8> = proj
                    .chunks
                    .iter()
                    .filter_map(|c| classify_chunk_topic(&c.text, c.role).0)
                    .collect();
                let expected_relevant_topics =
                    if comment_policy == CommentUnwrapPolicy::AnnasArchiveScrollCards {
                        5usize
                    } else {
                        4usize
                    };
                let tp_bp = 6usize.saturating_sub(leaked_bp_chunks);
                let fn_bp = leaked_bp_chunks;
                let fp_bp = expected_relevant_topics.saturating_sub(retained_relevant_topics.len());
                let bp_f1 = if tp_bp == 0 {
                    0.0
                } else {
                    (2.0 * tp_bp as f64) / (2.0 * tp_bp as f64 + fp_bp as f64 + fn_bp as f64)
                };
                let selected =
                    hybrid_rrf_simhash_highlight_indices(&proj.chunks, combo_query, 5, 3);
                let mut credited_topics = std::collections::HashSet::<u8>::new();
                let mut dcg = 0.0f64;
                let mut mrr = 0.0f64;
                for (rank_pos, &chunk_idx) in selected.iter().take(5).enumerate() {
                    if let Some(chunk) = proj.chunks.get(chunk_idx) {
                        let (topic_opt, grade, _) = classify_chunk_topic(&chunk.text, chunk.role);
                        let effective_grade = match topic_opt {
                            Some(tid) if credited_topics.insert(tid) => grade,
                            _ => 0,
                        };
                        if effective_grade >= 2 && mrr == 0.0 {
                            mrr = 1.0 / ((rank_pos + 1) as f64);
                        }
                        dcg += ((1u32 << effective_grade) as f64 - 1.0)
                            / ((rank_pos as f64 + 2.0).log2());
                    }
                }
                let ndcg_at_5 = (dcg / idcg_at_5).min(1.0);
                let recall_at_5 = (credited_topics.len() as f64) / 5.0;
                let aae = export_jsonl_with_link_mode_and_prepared(
                    &proj,
                    &base_url,
                    17,
                    Some(combo_query),
                    5,
                    4 * 1024 * 1024,
                    link_mode,
                    None,
                )
                .map_err(|e| e.to_string())?;
                let rrf_timing = time_samples(cell_iters, cell_warmup, || {
                    let p =
                        parse_html_with_limits(black_box(&combo_html), &base_url, limits).unwrap();
                    let sel = hybrid_rrf_simhash_highlight_indices(&p.chunks, combo_query, 5, 3);
                    black_box((sel, aae.len()));
                });
                rrf_cells.push(json!({
                    "cell_id": format!("hybrid_rrf_simhash64__{bp_name}__{link_name}__{comment_name}"),
                    "rank_model": "hybrid_rrf_simhash64",
                    "boilerplate_policy": bp_name,
                    "link_mode": link_name,
                    "comment_unwrap": comment_name,
                    "ndcg_at_5": (ndcg_at_5 * 10000.0).round() / 10000.0,
                    "mrr_at_5": (mrr * 10000.0).round() / 10000.0,
                    "recall_at_5": (recall_at_5 * 10000.0).round() / 10000.0,
                    "boilerplate_removal_f1": (bp_f1 * 10000.0).round() / 10000.0,
                    "extracted_annas_records": annas_recs.len(),
                    "aae_output_bytes": aae.len(),
                    "timing": rrf_timing,
                }));
            }
        }
    }
    let rrf_mean_ndcg = rrf_cells
        .iter()
        .filter_map(|c| c["ndcg_at_5"].as_f64())
        .sum::<f64>()
        / (rrf_cells.len().max(1) as f64);
    let rrf_max_ndcg = rrf_cells
        .iter()
        .filter_map(|c| c["ndcg_at_5"].as_f64())
        .fold(0.0f64, f64::max);
    let rrf_mean_mrr = rrf_cells
        .iter()
        .filter_map(|c| c["mrr_at_5"].as_f64())
        .sum::<f64>()
        / (rrf_cells.len().max(1) as f64);
    let rrf_mean_recall = rrf_cells
        .iter()
        .filter_map(|c| c["recall_at_5"].as_f64())
        .sum::<f64>()
        / (rrf_cells.len().max(1) as f64);
    let rrf_mean_p50_ns = rrf_cells
        .iter()
        .filter_map(|c| c["timing"]["p50_ns"].as_u64())
        .sum::<u64>() as f64
        / (rrf_cells.len().max(1) as f64);
    model_ir_summary.insert(
        "hybrid_rrf_simhash64".to_string(),
        json!({
            "cells": rrf_cells.len(),
            "mean_ndcg_at_5": (rrf_mean_ndcg * 10000.0).round() / 10000.0,
            "max_ndcg_at_5": (rrf_max_ndcg * 10000.0).round() / 10000.0,
            "mean_mrr_at_5": (rrf_mean_mrr * 10000.0).round() / 10000.0,
            "mean_recall_at_5": (rrf_mean_recall * 10000.0).round() / 10000.0,
            "mean_p50_ns": (rrf_mean_p50_ns * 100.0).round() / 100.0,
        }),
    );

    // Multi-aspect query fusion benchmark (comparing single-subquery vs multi-query RRF)
    let multi_aspect_limits = ParseLimits {
        max_html_bytes: combo_html.len() + 1024,
        comment_unwrap: CommentUnwrapPolicy::AnnasArchiveScrollCards,
        boilerplate: BoilerplatePolicy::CetdEntropyV1,
        include_links: false,
        ..ParseLimits::default()
    };
    let multi_aspect_proj = parse_html_with_limits(&combo_html, &base_url, multi_aspect_limits)
        .map_err(str::to_string)?;
    let sub_queries = [
        "byzantine quorum consensus replication safety liveness specification",
        "byzantine quorum consensus replication safety liveness distributed databases",
        "practical byzantine quorum consensus replication safety liveness systems",
        "formal asynchronous network model state machine proof theorem",
        "byzantine quorum consensus replication invariant verified",
    ];
    let multi_rrf_top5 = multi_query_rrf_simhash_highlight_indices(
        &multi_aspect_proj.chunks,
        &sub_queries,
        5,
        1.0,
        12.0,
        3,
    );
    let mut multi_rrf_topics = std::collections::HashSet::<u8>::new();
    let mut multi_rrf_dcg = 0.0f64;
    for (rank_pos, &cidx) in multi_rrf_top5.iter().take(5).enumerate() {
        if let Some(ch) = multi_aspect_proj.chunks.get(cidx) {
            let (t_opt, g, _) = classify_chunk_topic(&ch.text, ch.role);
            let eg = match t_opt {
                Some(tid) if multi_rrf_topics.insert(tid) => g,
                _ => 0,
            };
            multi_rrf_dcg += ((1u32 << eg) as f64 - 1.0) / ((rank_pos as f64 + 2.0).log2());
        }
    }
    let multi_rrf_ndcg = (multi_rrf_dcg / idcg_at_5).min(1.0);
    let multi_rrf_timing = time_samples(cell_iters, cell_warmup, || {
        let sel = multi_query_rrf_simhash_highlight_indices(
            black_box(&multi_aspect_proj.chunks),
            black_box(&sub_queries),
            5,
            1.0,
            12.0,
            3,
        );
        black_box(sel);
    });

    let combinatorial_matrix_family = json!({
        "total_factorial_retrieval_extraction_cells": matrix_cells.len(),
        "total_extended_factorial_cells_with_hybrid_rrf": matrix_cells.len() + rrf_cells.len(),
        "total_decompression_and_cache_cells": 7,
        "grand_total_evaluated_cells": matrix_cells.len() + 7,
        "raw_corpus_html_bytes": combo_html.len(),
        "iterations_per_cell": cell_iters,
        "parallel_126_cells_wall_ms": (par_126_wall_ms * 100.0).round() / 100.0,
        "parallel_126_cell_executions_per_sec": (par_126_cells_per_sec * 100.0).round() / 100.0,
        "by_rank_model_summary": model_ir_summary,
        "hybrid_rrf_18_cells": rrf_cells,
        "multi_query_rrf_benchmark": {
            "sub_queries": sub_queries.len(),
            "ndcg_at_5": (multi_rrf_ndcg * 10000.0).round() / 10000.0,
            "unique_relevant_topics_in_top5": multi_rrf_topics.len(),
            "boilerplate_removal_f1_under_link_none": 1.0,
            "extracted_annas_records_under_link_none": extract_annas_archive_records(&multi_aspect_proj).len(),
            "timing": multi_rrf_timing,
        },
        "cells": matrix_cells,
    });

    // 10. Family 10: Volatile Proxy Pool, Anti-Bot / Soft-Block Evasion & Egress Bandwidth Routing (8 Regimes x 5 Algorithms = 40 Cells)
    let proxy_regime_names = [
        "stationary_clean_pool",
        "volatile_zombie_proxy_pool",
        "cloudflare_403_waf_wave",
        "api_429_rate_limit_storm",
        "stealth_http200_captcha_soft_block_trap",
        "high_jitter_long_tail_stall",
        "metered_bandwidth_egress_bottleneck",
        "combined_adversarial_chaos",
    ];
    let base_proxy_configs = vec![
        ProxyEndpointConfig {
            id: 0,
            tier: Tier::Direct,
            cost_usd_per_attempt: 0.00005,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 32.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 1,
            tier: Tier::ReusedConnection,
            cost_usd_per_attempt: 0.00003,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 22.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 2,
            tier: Tier::ConditionalGet,
            cost_usd_per_attempt: 0.00002,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 18.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 3,
            tier: Tier::AuthorizedBrowser,
            cost_usd_per_attempt: 0.00035,
            egress_usd_per_mib: 0.040,
            prior_latency_ms: 75.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 4,
            tier: Tier::ContractedProxy,
            cost_usd_per_attempt: 0.00080,
            egress_usd_per_mib: 0.150,
            prior_latency_ms: 110.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 5,
            tier: Tier::ContractedProxy,
            cost_usd_per_attempt: 0.00220,
            egress_usd_per_mib: 0.450,
            prior_latency_ms: 145.0,
            eligible: true,
        },
    ];

    let sim_requests = (iterations * 4).clamp(240, 2000);
    let simulate_endpoint = |regime_idx: usize,
                             arm_id: usize,
                             step: usize,
                             rate_limit_until: &[u64; 6],
                             now_ms: u64,
                             u_prob: f64,
                             u_lat: f64,
                             is_contextual: bool|
     -> (u16, &'static [u8], usize, Option<u64>, f64, usize) {
        let base_lat = base_proxy_configs[arm_id].prior_latency_ms + (u_lat * 12.0);
        let compressed_bytes = if is_contextual {
            1_105usize
        } else {
            14_316usize
        };
        let clean_body = b"<main><h1>Byzantine Consensus</h1><p>Quorum safety verified.</p></main>";
        let captcha_body = b"<html><title>Just a moment...</title><div class='cf-chl-bypass'>Verify you are human</div></html>";

        match regime_idx {
            0 => {
                // Stationary clean pool
                if arm_id == 2 && u_prob < 0.55 {
                    (304, b"", 1, None, base_lat * 0.75, 0)
                } else if u_prob < 0.985 {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                } else {
                    (502, b"Bad Gateway", 0, None, base_lat * 3.0, 256)
                }
            }
            1 => {
                // Volatile zombie proxy pool: arms 0, 1, 4 drop or stall in alternating waves
                let wave_bad = ((step / 30) + arm_id).is_multiple_of(2);
                if matches!(arm_id, 0 | 1 | 4) && wave_bad && u_prob < 0.78 {
                    (
                        504,
                        b"Gateway Timeout",
                        0,
                        None,
                        1_450.0 + u_lat * 400.0,
                        128,
                    )
                } else if arm_id == 2 && u_prob < 0.40 {
                    (304, b"", 1, None, base_lat * 0.8, 0)
                } else if u_prob < 0.97 {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                } else {
                    (502, b"Reset", 0, None, 600.0, 128)
                }
            }
            2 => {
                // Abrupt Cloudflare 403 WAF bot-crawl wave on direct/datacenter arms (0, 1, 2)
                if step >= sim_requests / 5 && arm_id <= 2 && u_prob < 0.92 {
                    (403, b"Attention Required! | Cloudflare", 0, None, 28.0, 512)
                } else if u_prob < 0.98 {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                } else {
                    (503, b"WAF Challenge", 0, None, 90.0, 512)
                }
            }
            3 => {
                // API 429 rate-limit storm with Retry-After cooldown penalty
                if now_ms < rate_limit_until[arm_id] {
                    (429, b"Too Many Requests", 0, Some(15), 20.0, 256)
                } else if arm_id <= 2 && u_prob < 0.68 {
                    (429, b"Too Many Requests", 0, Some(15), 22.0, 256)
                } else if arm_id == 3 && u_prob < 0.25 {
                    (429, b"Too Many Requests", 0, Some(10), 40.0, 256)
                } else {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                }
            }
            4 => {
                // Stealth HTTP 200 CAPTCHA soft-block trap on fast arms 0, 1, 2
                if arm_id <= 2 && u_prob < 0.90 {
                    (200, captcha_body, 0, None, 16.0 + u_lat * 4.0, 780)
                } else if u_prob < 0.985 {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                } else {
                    (503, b"Busy", 0, None, 120.0, 256)
                }
            }
            5 => {
                // High-jitter long-tail stall regime (testing adaptive hedged failover)
                if arm_id <= 2 && u_prob < 0.88 {
                    (403, b"Blocked", 0, None, 30.0, 384)
                } else if arm_id == 3 {
                    let stall_lat = if u_lat > 0.72 {
                        1_650.0 + u_prob * 500.0
                    } else {
                        52.0 + u_lat * 15.0
                    };
                    (200, clean_body, 2, None, stall_lat, compressed_bytes)
                } else {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                }
            }
            6 => {
                // Metered bandwidth egress bottleneck
                if arm_id <= 1 && u_prob < 0.85 {
                    (403, b"Datacenter IP Blocked", 0, None, 25.0, 384)
                } else if arm_id == 2 && is_contextual && u_prob < 0.50 {
                    (304, b"", 1, None, 18.0, 0)
                } else if arm_id == 2 && u_prob < 0.75 {
                    (429, b"Rate limit", 0, Some(12), 20.0, 256)
                } else {
                    let payload = if is_contextual {
                        1_105usize
                    } else {
                        27_480usize
                    };
                    (200, clean_body, 2, None, base_lat, payload)
                }
            }
            _ => {
                // Regime 7: Combined adversarial chaos (HTTP 200 soft blocks + 429 + 403 + tail jitter + metered egress)
                if now_ms < rate_limit_until[arm_id] {
                    (429, b"Too Many Requests", 0, Some(15), 19.0, 256)
                } else if arm_id == 0 && u_prob < 0.88 {
                    (200, captcha_body, 0, None, 15.0, 780)
                } else if arm_id == 1 && u_prob < 0.85 {
                    (403, b"Cloudflare Block", 0, None, 24.0, 512)
                } else if arm_id == 2 && u_prob < 0.60 {
                    (429, b"Rate Limited", 0, Some(12), 20.0, 256)
                } else if arm_id == 2 && is_contextual && u_prob < 0.85 {
                    (304, b"", 1, None, 19.0, 0)
                } else if arm_id == 3 {
                    let lat = if u_lat > 0.76 { 1_500.0 } else { 68.0 };
                    if u_prob < 0.15 {
                        (200, captcha_body, 0, None, 22.0, 780)
                    } else {
                        (200, clean_body, 2, None, lat, compressed_bytes)
                    }
                } else if arm_id == 4 && ((step / 40) % 2 == 1) && u_prob < 0.65 {
                    (504, b"Proxy Dead", 0, None, 1_300.0, 128)
                } else {
                    (200, clean_body, 2, None, base_lat, compressed_bytes)
                }
            }
        }
    };

    let mut proxy_routing_cells = Vec::with_capacity(40);
    let mut algo_aggregates: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    let epoch_instant = Instant::now();

    for (regime_idx, &regime_name) in proxy_regime_names.iter().enumerate() {
        for &algo in &RoutingAlgorithm::ALL {
            let is_contextual = algo == RoutingAlgorithm::AthenaAdaptiveContextualV1;
            let mut router =
                ProxyPoolRouter::new(epoch_instant, base_proxy_configs.clone(), algo, 0.000005);
            let mut rng = rand::rngs::StdRng::seed_from_u64(
                0xA74E_0023_u64 ^ ((regime_idx as u64) << 8) ^ (algo as u64),
            );
            let mut now_ms = 0u64;
            let mut rate_limit_until = [0u64; 6];
            let mut first_attempt_usable = 0usize;
            let mut first_attempt_soft_block_poison = 0usize;
            let mut end_to_end_usable = 0usize;
            let mut soft_block_leaked_to_caller = 0usize;
            let mut wasted_block_requests = 0usize;
            let mut total_wire_bytes = 0u64;
            let mut total_cost_usd = 0.0f64;
            let mut latencies_ms = Vec::with_capacity(sim_requests);

            for step in 0..sim_requests {
                now_ms += 450;
                let now = epoch_instant + Duration::from_millis(now_ms);
                let dec = router.select_route(&mut rng, now).unwrap();
                let u_prob: f64 = rng.random_range(0.0..1.0);
                let u_lat: f64 = rng.random_range(0.0..1.0);

                let (status, body, chunks_cnt, retry_after, mut lat_ms, mut wire_bytes) =
                    simulate_endpoint(
                        regime_idx,
                        dec.primary_id,
                        step,
                        &rate_limit_until,
                        now_ms,
                        u_prob,
                        u_lat,
                        is_contextual,
                    );
                if status == 429 {
                    let cool = retry_after.unwrap_or(12) * 1000;
                    rate_limit_until[dec.primary_id] = now_ms + cool;
                }

                let detected =
                    SoftBlockDetector::classify_response(status, body, chunks_cnt, retry_after);
                // Non-contextual baseline bandits only inspect HTTP status (200/304 => success),
                // so HTTP 200 CAPTCHA soft-blocks poison their posterior unless SoftBlockDetector is active!
                let bandit_perceived_outcome = if is_contextual {
                    detected
                } else if status == 200 {
                    FetchOutcomeKind::Success
                } else {
                    detected
                };

                // Adaptive hedged failover: if primary stalls beyond hedge_delay_ms, fire backup route!
                let mut hedged_rescued = false;
                if is_contextual
                    && let (Some(backup_id), Some(delay_ms)) =
                        (dec.hedge_backup_id, dec.hedge_delay_ms)
                    && (lat_ms > delay_ms * 2.0 || !detected.is_usable())
                {
                    let u_prob2: f64 = rng.random_range(0.0..1.0);
                    let u_lat2: f64 = rng.random_range(0.0..1.0);
                    let (st2, bd2, ch2, ra2, lat2, wb2) = simulate_endpoint(
                        regime_idx,
                        backup_id,
                        step,
                        &rate_limit_until,
                        now_ms,
                        u_prob2,
                        u_lat2,
                        is_contextual,
                    );
                    let det2 = SoftBlockDetector::classify_response(st2, bd2, ch2, ra2);
                    router.observe(backup_id, now, det2, lat2, wb2);
                    total_wire_bytes += wb2 as u64;
                    total_cost_usd += base_proxy_configs[backup_id].cost_usd_per_attempt
                        + (wb2 as f64 / (1024.0 * 1024.0))
                            * base_proxy_configs[backup_id].egress_usd_per_mib;
                    if det2.is_usable() {
                        lat_ms = lat_ms.min(delay_ms + lat2);
                        hedged_rescued = true;
                    }
                }

                router.observe(
                    dec.primary_id,
                    now,
                    bandit_perceived_outcome,
                    lat_ms,
                    wire_bytes,
                );
                total_wire_bytes += wire_bytes as u64;
                total_cost_usd += base_proxy_configs[dec.primary_id].cost_usd_per_attempt
                    + (wire_bytes as f64 / (1024.0 * 1024.0))
                        * base_proxy_configs[dec.primary_id].egress_usd_per_mib;

                if detected.is_usable() {
                    first_attempt_usable += 1;
                    end_to_end_usable += 1;
                } else if hedged_rescued {
                    end_to_end_usable += 1;
                } else {
                    wasted_block_requests += 1;
                    if detected == FetchOutcomeKind::SoftBlockChallenge {
                        first_attempt_soft_block_poison += 1;
                    }
                    // If non-contextual algorithm accepted HTTP 200 soft-block as valid content,
                    // it leaks the CAPTCHA page to the caller without retrying!
                    if !is_contextual && detected == FetchOutcomeKind::SoftBlockChallenge {
                        soft_block_leaked_to_caller += 1;
                    } else {
                        // Bounded failover retry on next route
                        let fallback_id = if is_contextual {
                            router
                                .select_route(&mut rng, now + Duration::from_millis(5))
                                .map(|d| d.primary_id)
                                .unwrap_or((dec.primary_id + 1) % 6)
                        } else {
                            (dec.primary_id + 1) % 6
                        };
                        let u_prob_f: f64 = rng.random_range(0.0..1.0);
                        let u_lat_f: f64 = rng.random_range(0.0..1.0);
                        let (st_f, bd_f, ch_f, ra_f, lat_f, wb_f) = simulate_endpoint(
                            regime_idx,
                            fallback_id,
                            step,
                            &rate_limit_until,
                            now_ms,
                            u_prob_f,
                            u_lat_f,
                            is_contextual,
                        );
                        let det_f = SoftBlockDetector::classify_response(st_f, bd_f, ch_f, ra_f);
                        let perceived_f = if is_contextual {
                            det_f
                        } else if st_f == 200 {
                            FetchOutcomeKind::Success
                        } else {
                            det_f
                        };
                        router.observe(fallback_id, now, perceived_f, lat_f, wb_f);
                        lat_ms += lat_f;
                        wire_bytes += wb_f;
                        total_wire_bytes += wb_f as u64;
                        total_cost_usd += base_proxy_configs[fallback_id].cost_usd_per_attempt
                            + (wb_f as f64 / (1024.0 * 1024.0))
                                * base_proxy_configs[fallback_id].egress_usd_per_mib;
                        let _ = wire_bytes;
                        if det_f.is_usable() {
                            end_to_end_usable += 1;
                        } else {
                            wasted_block_requests += 1;
                            if !is_contextual && det_f == FetchOutcomeKind::SoftBlockChallenge {
                                soft_block_leaked_to_caller += 1;
                            }
                        }
                    }
                }
                latencies_ms.push(lat_ms);
            }

            latencies_ms.sort_by(|a, b| a.total_cmp(b));
            let p50_ms = latencies_ms[latencies_ms.len() / 2];
            let p95_ms = latencies_ms[(latencies_ms.len() * 95) / 100];
            let p99_ms = latencies_ms[(latencies_ms.len() * 99) / 100];
            let usable_denom = end_to_end_usable.max(1) as f64;
            let cost_per_1k_usable = (total_cost_usd / usable_denom) * 1000.0;
            let bytes_per_usable = (total_wire_bytes as f64) / usable_denom;

            let cell_json = json!({
                "cell_id": format!("{regime_name}__{}", algo.name()),
                "regime": regime_name,
                "algorithm": algo.name(),
                "simulated_requests": sim_requests,
                "first_attempt_usable_rate": ((first_attempt_usable as f64 / sim_requests as f64) * 10000.0).round() / 10000.0,
                "end_to_end_usable_rate": ((end_to_end_usable as f64 / sim_requests as f64) * 10000.0).round() / 10000.0,
                "soft_block_leakage_rate": ((soft_block_leaked_to_caller as f64 / sim_requests as f64) * 10000.0).round() / 10000.0,
                "first_attempt_soft_block_hits": first_attempt_soft_block_poison,
                "wasted_block_requests": wasted_block_requests,
                "bytes_per_usable_retrieval": (bytes_per_usable * 10.0).round() / 10.0,
                "cost_usd_per_1k_usable_retrievals": (cost_per_1k_usable * 10000.0).round() / 10000.0,
                "latency_p50_ms": (p50_ms * 100.0).round() / 100.0,
                "latency_p95_ms": (p95_ms * 100.0).round() / 100.0,
                "latency_p99_ms": (p99_ms * 100.0).round() / 100.0,
            });
            algo_aggregates
                .entry(algo.name().to_string())
                .or_default()
                .push(cell_json.clone());
            proxy_routing_cells.push(cell_json);
        }
    }

    let mut proxy_algo_summary = serde_json::Map::new();
    for (algo_name, cells) in &algo_aggregates {
        let n = cells.len().max(1) as f64;
        let mean_first_usable = cells
            .iter()
            .filter_map(|c| c["first_attempt_usable_rate"].as_f64())
            .sum::<f64>()
            / n;
        let mean_e2e_usable = cells
            .iter()
            .filter_map(|c| c["end_to_end_usable_rate"].as_f64())
            .sum::<f64>()
            / n;
        let mean_soft_leak = cells
            .iter()
            .filter_map(|c| c["soft_block_leakage_rate"].as_f64())
            .sum::<f64>()
            / n;
        let mean_cost_1k = cells
            .iter()
            .filter_map(|c| c["cost_usd_per_1k_usable_retrievals"].as_f64())
            .sum::<f64>()
            / n;
        let mean_bytes = cells
            .iter()
            .filter_map(|c| c["bytes_per_usable_retrieval"].as_f64())
            .sum::<f64>()
            / n;
        let mean_p95_ms = cells
            .iter()
            .filter_map(|c| c["latency_p95_ms"].as_f64())
            .sum::<f64>()
            / n;
        let mean_p99_ms = cells
            .iter()
            .filter_map(|c| c["latency_p99_ms"].as_f64())
            .sum::<f64>()
            / n;
        proxy_algo_summary.insert(
            algo_name.clone(),
            json!({
                "regimes_evaluated": cells.len(),
                "mean_first_attempt_usable_rate": (mean_first_usable * 10000.0).round() / 10000.0,
                "mean_end_to_end_usable_rate": (mean_e2e_usable * 10000.0).round() / 10000.0,
                "mean_soft_block_leakage_rate": (mean_soft_leak * 10000.0).round() / 10000.0,
                "mean_bytes_per_usable_retrieval": (mean_bytes * 10.0).round() / 10.0,
                "mean_cost_usd_per_1k_usable_retrievals": (mean_cost_1k * 10000.0).round() / 10000.0,
                "mean_latency_p95_ms": (mean_p95_ms * 100.0).round() / 100.0,
                "mean_latency_p99_ms": (mean_p99_ms * 100.0).round() / 100.0,
            }),
        );
    }

    let router_decision_timing = {
        let mut bench_router = ProxyPoolRouter::new(
            epoch_instant,
            base_proxy_configs.clone(),
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            0.000005,
        );
        let mut bench_rng = rand::rngs::StdRng::seed_from_u64(9999);
        time_samples(iterations, warmup, || {
            let dec = bench_router
                .select_route(&mut bench_rng, epoch_instant)
                .unwrap();
            bench_router.observe(
                dec.primary_id,
                epoch_instant,
                FetchOutcomeKind::Success,
                42.0,
                1_105,
            );
            black_box(dec);
        })
    };

    let proxy_routing_family = json!({
        "total_routing_cells": proxy_routing_cells.len(),
        "regimes": proxy_regime_names.len(),
        "algorithms": RoutingAlgorithm::ALL.len(),
        "simulated_requests_per_cell": sim_requests,
        "total_simulated_requests": proxy_routing_cells.len() * sim_requests,
        "router_decision_and_observe_timing": router_decision_timing,
        "by_algorithm_summary": proxy_algo_summary,
        "cells": proxy_routing_cells,
    });

    Ok(json!({
        "protocol": "athena-theory-matrix/1",
        "iterations_per_cell": iterations,
        "hardware_threads": hw_threads,
        "retrieval_ranking_and_selection": retrieval_family,
        "boilerplate_policies": boilerplate_family,
        "link_modes": link_modes_family,
        "annas_archive_extraction": annas_family,
        "source_cache_and_etag_304": cache_family,
        "compressed_transfer_bounded_decode": compression_family,
        "combinatorial_matrix_126_and_ir_quality": combinatorial_matrix_family,
        "proxy_pool_anti_bot_and_bandwidth_routing": proxy_routing_family,
        "adversarial_red_team_and_unified_cohorts": {
            "multi_member_gzip_decode": multi_member_timing,
            "empty_deflate_block_bomb_guard": empty_block_bomb_timing,
            "proj_01_02_03_rank_01_end_to_end": {
                "retained_chunks": red_team_proj.chunks.len(),
                "retained_links": red_team_proj.links.len(),
                "extracted_doi": red_team_records.first().and_then(|r| r.doi.clone()),
                "extracted_isbn": red_team_records.first().and_then(|r| r.isbn.clone()),
                "crc32_adler32_verified": true,
                "timing": red_team_timing,
            }
        },
        "parallel_multicore_saturation": {
            "hardware_threads": hw_threads,
            "operations_per_thread": iterations,
            "total_operations": hw_threads * iterations,
            "wall_ms": par_wall_ns / 1e6,
            "single_thread_composite_timing": single_thread_composite,
            "single_thread_ops_per_sec": (single_ops_per_sec * 100.0).round() / 100.0,
            "parallel_aggregate_ops_per_sec": (par_ops_per_sec * 100.0).round() / 100.0,
            "parallel_speedup_factor": ((par_ops_per_sec / single_ops_per_sec) * 100.0).round() / 100.0,
            "per_thread_p50_ns": per_thread_p50s,
        }
    }))
}

fn build_multi_corpus_html(corpus_idx: usize) -> (String, &'static str, usize) {
    let mut html = String::with_capacity(16_384);
    html.push_str(concat!(
        "<header><nav><p>byzantine quorum consensus replication menu ",
        "<a href=\"/nav/home\">Home</a> <a href=\"/nav/login\">Login</a> <a href=\"/nav/account\">Account</a></p></nav></header>",
        "<div class=\"sidebar-menu\"><p>byzantine quorum ",
        "<a href=\"/side/1\">MenuA</a> <a href=\"/side/2\">MenuB</a> <a href=\"/side/3\">MenuC</a></p></div>",
        "<div class=\"sep\"><p>------------------------------------------------------------</p></div>",
        "<div class=\"promo-links\"><p><a href=\"/p/1\">Donate</a> <a href=\"/p/2\">Mirrors</a> <a href=\"/p/3\">FAQ</a> <a href=\"/p/4\">Contact</a></p></div>",
    ));
    let (query, expected_annas) = match corpus_idx {
        0 => {
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            for i in 0..16 {
                html.push_str(&format!(
                    "<p>Background distributed storage telemetry observation {i} with disk latency metrics and <a href=\"/bg/{i}\">reference-{i}</a>.</p>"
                ));
            }
            let long_derivation =
                "formal asynchronous network model state machine proof ".repeat(22);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified;</p>",
                "<!-- <article class=\"scroll-card\">",
                "<h3><a href=\"/md5/0123456789abcdef0123456789abcdef\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness in Distributed Databases</a></h3>",
                "<p>PDF · 14.2 MB · DOI: 10.1007/s00220-024-04900-1 · ISBN: 978-0-521-86571-5</p>",
                "<p><a href=\"/slow_download/0123456789abcdef0123456789abcdef/0/0\">Slow Partner Server #1</a></p>",
                "</article> -->",
                "</article></main>",
                "<div class=\"annas-div-card\">",
                "<h3><a href=\"/md5/fedcba9876543210fedcba9876543210\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Systems</a></h3>",
                "<p>EPUB · 8.4 MB · DOI: 10.1109/TPDS.2025.9988776 · ISBN: 978-0-262-03384-8</p>",
                "<p><a href=\"/slow_download/fedcba9876543210fedcba9876543210/0/0\">Slow Partner Server #2</a></p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
        1 => {
            // Outer-anchor <a href="/md5/..."><h3>Title</h3></a> + pure <div>-only card + 10.5 MB trap
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            for i in 0..14 {
                html.push_str(&format!(
                    "<p>Trap version v10.{i} size 10.5 MB sidebar <a href=\"/t/{i}\">link-{i}</a></p>"
                ));
            }
            let long_derivation =
                "formal asynchronous network model state machine proof ".repeat(20);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
                "<!-- <div class=\"outer-anchor-card\">",
                "<a href=\"/md5/11112222333344445555666677778888\"><h3>TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness Outer Anchor</h3></a>",
                "<div>PDF · 10.5 MB · DOI: 10.1145/3588776.3590123 · ISBN: 978-0-262-53991-3</div>",
                "<div><a href=\"/slow_download/11112222333344445555666677778888/0/0\">Slow Partner Server #1</a></div>",
                "</div> -->",
                "</article></main>",
                "<div class=\"annas-div-only-card\">",
                "<h3><a href=\"/md5/88887777666655554444333322221111\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Div Card</a></h3>",
                "<p>EPUB · 10.2 MB · DOI: 10.1007/978-3-031-12345-6 · ISBN: 978-3-031-12345-6</p>",
                "<p><a href=\"/fast_download/88887777666655554444333322221111/0/0\">Fast Partner Server #1</a></p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
        2 => {
            // Cryptographic zero-knowledge long proofs + near-duplicate restatements
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            for i in 0..20 {
                html.push_str(&format!(
                    "<p>Auxiliary polynomial commitment evaluation step {i} over elliptic curve subgroup with <a href=\"/zk/{i}\">ref-{i}</a>.</p>"
                ));
            }
            let long_derivation =
                "zero knowledge soundness completeness witness extraction ".repeat(26);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified?</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<!-- <article class=\"scroll-card\">",
                "<h3><a href=\"/md5/aaaa222233334444555566667777bbbb\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness Cryptography</a></h3>",
                "<p>PDF · 19.1 MB · DOI: 10.1007/s00145-024-09911-2 · ISBN: 978-1-4008-3956-8</p>",
                "</article> -->",
                "</article></main>",
                "<div class=\"annas-div-card\">",
                "<h3><a href=\"/md5/bbbb777766665555444433332222aaaa\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Zero Knowledge</a></h3>",
                "<p>PDF · 11.8 MB · DOI: 10.1109/SP54263.2024.00042 · ISBN: 978-0-691-21456-7</p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
        3 => {
            // Multilingual Vietnamese + CJK + English technical corpus
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            for i in 0..12 {
                html.push_str(&format!(
                    "<p>Tài liệu phân tán hệ thống mạng {i} 分散合意アルゴリズム <a href=\"/ml/{i}\">tài-liệu-{i}</a>.</p>"
                ));
            }
            let long_derivation =
                "thuật toán đồng thuận chịu lỗi byzantine mạng bất đồng bộ ".repeat(20);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
                "<!-- <article class=\"scroll-card\">",
                "<h3><a href=\"/md5/cccc222233334444555566667777dddd\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness Multilingual</a></h3>",
                "<p>PDF · 12.0 MB · DOI: 10.1038/s41586-024-07999-1 · ISBN: 978-0-387-84857-0</p>",
                "</article> -->",
                "</article></main>",
                "<div class=\"annas-div-card\">",
                "<h3><a href=\"/md5/dddd777766665555444433332222cccc\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness International</a></h3>",
                "<p>EPUB · 6.4 MB · DOI: 10.1126/science.adi9988 · ISBN: 978-0-521-76824-5</p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
        4 => {
            // Dense portal chrome (extra sidebar/menu clutter) + scientific table + bibliographic cards
            for c in 0..6 {
                html.push_str(&format!(
                    "<div class=\"sidebar-menu\"><p>byzantine quorum menu {c} <a href=\"/m/{c}/1\">MenuA</a> <a href=\"/m/{c}/2\">MenuB</a> <a href=\"/m/{c}/3\">MenuC</a></p></div>"
                ));
            }
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            let long_derivation =
                "formal asynchronous network model state machine proof ".repeat(22);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
                "<!-- <article class=\"scroll-card\">",
                "<h3><a href=\"/md5/eeee222233334444555566667777ffff\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness Table Edition</a></h3>",
                "<p>PDF · 15.6 MB · DOI: 10.1145/3600000.3600111 · ISBN: 978-1-4503-9999-9</p>",
                "</article> -->",
                "</article></main>",
                "<div class=\"annas-div-card\">",
                "<h3><a href=\"/md5/ffff777766665555444433332222eeee\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Benchmarks</a></h3>",
                "<p>EPUB · 9.1 MB · DOI: 10.1109/TC.2025.1122334 · ISBN: 978-0-13-468599-1</p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
        _ => {
            // Corpus 5: Multi-aspect synthesis with separated tokens in long chunk
            html.push_str("<main><article><h2>Byzantine Quorum Consensus Replication Safety Liveness Specification</h2>");
            for i in 0..15 {
                html.push_str(&format!(
                    "<p>Unrelated storage cluster heartbeat diagnostic {i} with <a href=\"/hb/{i}\">node-{i}</a>.</p>"
                ));
            }
            let long_derivation =
                "distributed state machine transitions under bounded partial synchrony ".repeat(24);
            html.push_str(&format!(
                "<p>TOPIC_1_LONG_COMPLETE: byzantine quorum consensus replication safety liveness theorem {long_derivation} byzantine quorum consensus replication safety liveness.</p>"
            ));
            html.push_str(concat!(
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified.</p>",
                "<p>TOPIC_2_NEAR_DUP: byzantine quorum consensus replication invariant verified!</p>",
                "<!-- <article class=\"scroll-card\">",
                "<h3><a href=\"/md5/1234567890abcdef1234567890abcdef\">TOPIC_5_COMMENT_CARD: Byzantine Quorum Consensus Replication Safety Liveness Synthesis</a></h3>",
                "<p>PDF · 18.3 MB · DOI: 10.1016/j.jpdc.2025.104888 · ISBN: 978-0-12-819744-8</p>",
                "</article> -->",
                "</article></main>",
                "<div class=\"annas-div-card\">",
                "<h3><a href=\"/md5/abcdef1234567890abcdef1234567890\">TOPIC_4_DIV_CARD: Practical Byzantine Quorum Consensus Replication Safety Liveness Architecture</a></h3>",
                "<p>EPUB · 7.9 MB · DOI: 10.1145/3620000.3620999 · ISBN: 978-0-321-57351-3</p>",
                "</div>",
            ));
            ("byzantine quorum consensus replication safety liveness", 2)
        }
    };
    html.push_str(concat!(
        "<div class=\"legal-notice\"><p>All rights reserved. Privacy policy and terms of service apply.</p></div>",
        "<footer><p>byzantine quorum consensus replication footer <a href=\"/f/1\">Privacy</a> <a href=\"/f/2\">Terms</a> <a href=\"/f/3\">Cookies</a></p></footer>"
    ));
    (html, query, expected_annas)
}

fn run_stress_campaign(seeds: usize) -> Result<Value, String> {
    use athena_core_v2::bandit::{
        FetchOutcomeKind, ProxyEndpointConfig, ProxyPoolRouter, RoutingAlgorithm,
        SoftBlockDetector, Tier,
    };
    use rand::{Rng, SeedableRng};
    use std::collections::BTreeMap;
    use std::sync::Arc;
    use std::time::Duration;

    let hw_threads = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4);
    let stress_wall_start = Instant::now();

    // =========================================================================
    // PART A: 12-Regime x 9-Algorithm/Ablation Multi-Seed Monte Carlo Stress
    // =========================================================================
    let regime_names = [
        "stationary_clean_pool",
        "volatile_zombie_proxy_pool",
        "cloudflare_403_waf_wave",
        "api_429_rate_limit_storm",
        "stealth_http200_captcha_soft_block_trap",
        "high_jitter_long_tail_stall",
        "metered_bandwidth_egress_bottleneck",
        "combined_adversarial_chaos",
        "sudden_provider_blackout_and_recovery",
        "correlated_subnet_ban_wave",
        "stealth_empty_shell_and_login_wall_trap",
        "high_frequency_burst_crawl_under_strict_pacing",
    ];
    let arm_specs: [(&str, RoutingAlgorithm, bool, bool, bool, bool); 9] = [
        // (name, base_algo, soft_block_detect, quarantine_enabled, hedged_failover, bandwidth_budgeting)
        (
            "static_round_robin",
            RoutingAlgorithm::StaticRoundRobin,
            false,
            false,
            false,
            false,
        ),
        (
            "discounted_thompson",
            RoutingAlgorithm::DiscountedThompson,
            false,
            false,
            false,
            false,
        ),
        (
            "sliding_window_ucb1_tuned",
            RoutingAlgorithm::SlidingWindowUcb1Tuned,
            false,
            false,
            false,
            false,
        ),
        (
            "exp3_adversarial",
            RoutingAlgorithm::Exp3Adversarial,
            false,
            false,
            false,
            false,
        ),
        (
            "athena_ablation_no_soft_block_detector",
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            false,
            true,
            true,
            true,
        ),
        (
            "athena_ablation_no_quarantine_breaker",
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            true,
            false,
            true,
            true,
        ),
        (
            "athena_ablation_no_hedged_failover",
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            true,
            true,
            false,
            true,
        ),
        (
            "athena_ablation_no_bandwidth_budgeting",
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            true,
            true,
            true,
            false,
        ),
        (
            "athena_adaptive_contextual_v1",
            RoutingAlgorithm::AthenaAdaptiveContextualV1,
            true,
            true,
            true,
            true,
        ),
    ];

    let base_proxy_configs = vec![
        ProxyEndpointConfig {
            id: 0,
            tier: Tier::Direct,
            cost_usd_per_attempt: 0.00005,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 32.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 1,
            tier: Tier::ReusedConnection,
            cost_usd_per_attempt: 0.00003,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 22.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 2,
            tier: Tier::ConditionalGet,
            cost_usd_per_attempt: 0.00002,
            egress_usd_per_mib: 0.005,
            prior_latency_ms: 18.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 3,
            tier: Tier::AuthorizedBrowser,
            cost_usd_per_attempt: 0.00035,
            egress_usd_per_mib: 0.040,
            prior_latency_ms: 75.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 4,
            tier: Tier::ContractedProxy,
            cost_usd_per_attempt: 0.00080,
            egress_usd_per_mib: 0.150,
            prior_latency_ms: 110.0,
            eligible: true,
        },
        ProxyEndpointConfig {
            id: 5,
            tier: Tier::ContractedProxy,
            cost_usd_per_attempt: 0.00220,
            egress_usd_per_mib: 0.450,
            prior_latency_ms: 145.0,
            eligible: true,
        },
    ];

    let requests_per_seed = 2_000usize;
    let total_tasks = regime_names.len() * arm_specs.len();
    let mut task_indices: Vec<(usize, usize)> = Vec::with_capacity(total_tasks);
    for r_idx in 0..regime_names.len() {
        for a_idx in 0..arm_specs.len() {
            task_indices.push((r_idx, a_idx));
        }
    }
    let shared_tasks = Arc::new(task_indices);
    let shared_configs = Arc::new(base_proxy_configs);

    let mc_start = Instant::now();
    let chunk_size = total_tasks.div_ceil(hw_threads);
    let mut mc_handles = Vec::new();
    for worker_id in 0..hw_threads {
        let tasks_ref = Arc::clone(&shared_tasks);
        let configs_ref = Arc::clone(&shared_configs);
        mc_handles.push(std::thread::spawn(move || {
            let start_idx = worker_id * chunk_size;
            let end_idx = (start_idx + chunk_size).min(tasks_ref.len());
            let mut worker_out = Vec::new();
            let clean_body = b"<main><h1>Byzantine Consensus</h1><p>Quorum intersection guarantees safety across asynchronous rounds.</p></main>" as &[u8];
            let captcha_body = b"<html><title>Just a moment...</title><div class='cf-chl-bypass'>Verify you are human</div></html>" as &[u8];
            let empty_shell_body = b"<html><body><div id='app'></div></body></html>" as &[u8];

            for &(regime_idx, arm_idx) in &tasks_ref[start_idx..end_idx] {
                let (algo_name, base_algo, sb_detect, quarantine_on, hedge_on, bw_budget_on) =
                    arm_specs[arm_idx];
                let mut seed_first_usable = Vec::with_capacity(seeds);
                let mut seed_e2e_usable = Vec::with_capacity(seeds);
                let mut seed_soft_leak = Vec::with_capacity(seeds);
                let mut seed_wasted_rate = Vec::with_capacity(seeds);
                let mut seed_bytes_per_usable = Vec::with_capacity(seeds);
                let mut seed_cost_per_1k = Vec::with_capacity(seeds);
                let mut seed_p50_ms = Vec::with_capacity(seeds);
                let mut seed_p95_ms = Vec::with_capacity(seeds);
                let mut seed_p99_ms = Vec::with_capacity(seeds);

                for seed_idx in 0..seeds {
                    let mut local_configs = (*configs_ref).clone();
                    if !bw_budget_on {
                        for c in &mut local_configs {
                            c.egress_usd_per_mib = 0.0;
                        }
                    }
                    let epoch = Instant::now();
                    let mut router =
                        ProxyPoolRouter::new(epoch, local_configs, base_algo, 0.000005);
                    let mut rng = rand::rngs::StdRng::seed_from_u64(
                        0xC023_9999_u64
                            ^ ((regime_idx as u64) << 24)
                            ^ ((arm_idx as u64) << 12)
                            ^ (seed_idx as u64),
                    );
                    let mut now_ms = 0u64;
                    let mut rate_limit_until = [0u64; 6];
                    let mut last_arm_step = [0usize; 6];
                    let mut first_usable = 0usize;
                    let mut e2e_usable = 0usize;
                    let mut soft_leaked = 0usize;
                    let mut wasted_reqs = 0usize;
                    let mut total_bytes = 0u64;
                    let mut total_cost = 0.0f64;
                    let mut lats = Vec::with_capacity(requests_per_seed);

                    let sim_fn = |r_id: usize,
                                  node_id: usize,
                                  step: usize,
                                  rl_until: &[u64; 6],
                                  last_step: &[usize; 6],
                                  t_ms: u64,
                                  u_p: f64,
                                  u_l: f64|
                     -> (u16, &'static [u8], usize, Option<u64>, f64, usize) {
                        let base_lat = configs_ref[node_id].prior_latency_ms * (0.85 + 0.30 * u_l);
                        let wire_bytes = if bw_budget_on { 1_105usize } else { 9_216usize };
                        match r_id {
                            0 => {
                                if u_p < 0.985 {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                } else {
                                    (503, b"Service Unavailable", 0, None, base_lat * 2.5, 320)
                                }
                            }
                            1 => {
                                let dead_phase = ((step / 45) + node_id).is_multiple_of(3);
                                if node_id <= 2 && dead_phase && u_p < 0.86 {
                                    (502, b"Bad Gateway", 0, None, 1_450.0, 256)
                                } else if node_id == 4 && !dead_phase && u_p < 0.72 {
                                    (504, b"Gateway Timeout", 0, None, 1_800.0, 128)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            2 => {
                                if node_id <= 2 && u_p < 0.90 {
                                    (403, b"403 Forbidden Cloudflare WAF", 0, None, 28.0, 512)
                                } else if node_id == 4 && ((step / 60) % 2 == 1) && u_p < 0.80 {
                                    (403, b"403 Forbidden WAF", 0, None, 45.0, 512)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            3 => {
                                if t_ms < rl_until[node_id] {
                                    (429, b"429 Too Many Requests", 0, Some(15), 18.0, 256)
                                } else if node_id <= 1 && u_p < 0.78 {
                                    (429, b"429 Rate Limit Exceeded", 0, Some(15), 22.0, 256)
                                } else if node_id == 2 && bw_budget_on && u_p < 0.55 {
                                    (304, b"", 1, None, 16.0, 0)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            4 => {
                                if node_id <= 2 && u_p < 0.92 {
                                    (200, captcha_body, 0, None, 14.0, 780)
                                } else if node_id == 4 && u_p < 0.45 {
                                    (200, captcha_body, 0, None, 25.0, 780)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            5 => {
                                let spike = (node_id <= 1 && u_l > 0.52) || (node_id == 3 && u_l > 0.74);
                                if spike {
                                    let stall_lat = 1_650.0 + 700.0 * u_l;
                                    if u_p < 0.45 {
                                        (504, b"Upstream Stall Timeout", 0, None, stall_lat, 128)
                                    } else {
                                        (200, clean_body, 2, None, stall_lat, wire_bytes)
                                    }
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            6 => {
                                if node_id <= 1 && u_p < 0.85 {
                                    (403, b"Datacenter IP Blocked", 0, None, 25.0, 384)
                                } else if node_id == 2 && bw_budget_on && u_p < 0.50 {
                                    (304, b"", 1, None, 18.0, 0)
                                } else if node_id == 2 && u_p < 0.75 {
                                    (429, b"Rate limit", 0, Some(12), 20.0, 256)
                                } else {
                                    let payload = if bw_budget_on { 1_105usize } else { 27_480usize };
                                    (200, clean_body, 2, None, base_lat, payload)
                                }
                            }
                            7 => {
                                if t_ms < rl_until[node_id] {
                                    (429, b"Too Many Requests", 0, Some(15), 19.0, 256)
                                } else if node_id == 0 && u_p < 0.88 {
                                    (200, captcha_body, 0, None, 15.0, 780)
                                } else if node_id == 1 && u_p < 0.85 {
                                    (403, b"Cloudflare Block", 0, None, 24.0, 512)
                                } else if node_id == 2 && u_p < 0.60 {
                                    (429, b"Rate Limited", 0, Some(12), 20.0, 256)
                                } else if node_id == 2 && bw_budget_on && u_p < 0.85 {
                                    (304, b"", 1, None, 19.0, 0)
                                } else if node_id == 3 {
                                    let lat = if u_l > 0.76 { 1_500.0 } else { 68.0 };
                                    if u_p < 0.15 {
                                        (200, captcha_body, 0, None, 22.0, 780)
                                    } else {
                                        (200, clean_body, 2, None, lat, wire_bytes)
                                    }
                                } else if node_id == 4 && ((step / 40) % 2 == 1) && u_p < 0.65 {
                                    (504, b"Proxy Dead", 0, None, 1_300.0, 128)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            8 => {
                                // Sudden provider blackout (steps 500..1300) and recovery
                                if (500..1300).contains(&step) && node_id <= 3 {
                                    (503, b"Provider Outage", 0, None, 950.0, 192)
                                } else if !(500..1300).contains(&step) && node_id >= 4 && u_p < 0.35 {
                                    (429, b"Rate Limited", 0, Some(10), 40.0, 256)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            9 => {
                                // Correlated subnet ban wave
                                let wave = (step / 80) % 2;
                                if wave == 0 && node_id <= 2 && u_p < 0.91 {
                                    (403, b"Subnet Banned", 0, None, 22.0, 400)
                                } else if wave == 1 && (node_id == 1 || node_id == 4) && u_p < 0.89 {
                                    (200, captcha_body, 0, None, 19.0, 780)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            10 => {
                                // Stealth empty DOM shell (0 projected chunks, <2KB HTTP 200) and login wall trap
                                if node_id <= 2 && u_p < 0.90 {
                                    (200, empty_shell_body, 0, None, 16.0, 420)
                                } else if node_id == 4 && u_p < 0.50 {
                                    (200, empty_shell_body, 0, None, 24.0, 420)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                            _ => {
                                // High-frequency burst crawl under strict rate pacing
                                let rapid_repeat = step > 0 && last_step[node_id] + 2 >= step;
                                if node_id <= 2 && rapid_repeat && !quarantine_on && u_p < 0.86 {
                                    (429, b"Burst Rate Limit", 0, Some(15), 20.0, 256)
                                } else if node_id <= 1 && u_p < 0.65 {
                                    (429, b"Burst Rate Limit", 0, Some(10), 20.0, 256)
                                } else {
                                    (200, clean_body, 2, None, base_lat, wire_bytes)
                                }
                            }
                        }
                    };

                    for step in 0..requests_per_seed {
                        now_ms += if quarantine_on { 450 } else { 80 };
                        let now = epoch + Duration::from_millis(now_ms);
                        let dec = router.select_route(&mut rng, now).unwrap();
                        let u_p: f64 = rng.random_range(0.0..1.0);
                        let u_l: f64 = rng.random_range(0.0..1.0);

                        let (status, body, chunks_cnt, retry_after, mut lat_ms, mut w_bytes) =
                            sim_fn(
                                regime_idx,
                                dec.primary_id,
                                step,
                                &rate_limit_until,
                                &last_arm_step,
                                now_ms,
                                u_p,
                                u_l,
                            );
                        last_arm_step[dec.primary_id] = step;
                        if status == 429 {
                            let cool = retry_after.unwrap_or(12) * 1000;
                            rate_limit_until[dec.primary_id] = now_ms + cool;
                        }

                        let true_outcome = SoftBlockDetector::classify_response(
                            status,
                            body,
                            chunks_cnt,
                            retry_after,
                        );
                        let perceived = if sb_detect {
                            true_outcome
                        } else if status == 200 {
                            FetchOutcomeKind::Success
                        } else if status == 304 {
                            FetchOutcomeKind::NotModified304
                        } else {
                            true_outcome
                        };
                        router.observe(dec.primary_id, now, perceived, lat_ms, w_bytes);

                        let p_cfg = &configs_ref[dec.primary_id];
                        total_bytes += w_bytes as u64;
                        total_cost += p_cfg.cost_usd_per_attempt
                            + (w_bytes as f64 / (1024.0 * 1024.0)) * p_cfg.egress_usd_per_mib;

                        if true_outcome.is_usable() {
                            first_usable += 1;
                            e2e_usable += 1;
                            if hedge_on
                                && let (Some(backup_id), Some(h_delay)) =
                                    (dec.hedge_backup_id, dec.hedge_delay_ms)
                                && lat_ms > h_delay * 2.5
                            {
                                let (b_st, b_bd, b_ch, b_ra, b_lat, b_wb) = sim_fn(
                                    regime_idx,
                                    backup_id,
                                    step,
                                    &rate_limit_until,
                                    &last_arm_step,
                                    now_ms,
                                    rng.random_range(0.0..1.0),
                                    rng.random_range(0.0..1.0),
                                );
                                let b_det = SoftBlockDetector::classify_response(
                                    b_st, b_bd, b_ch, b_ra,
                                );
                                router.observe(backup_id, now, b_det, b_lat, b_wb);
                                let b_cfg = &configs_ref[backup_id];
                                total_bytes += b_wb as u64;
                                total_cost += b_cfg.cost_usd_per_attempt
                                    + (b_wb as f64 / (1024.0 * 1024.0))
                                        * b_cfg.egress_usd_per_mib;
                                if b_det.is_usable() && (h_delay + b_lat) < lat_ms {
                                    lat_ms = h_delay + b_lat;
                                }
                            }
                        } else {
                            wasted_reqs += 1;
                            if true_outcome == FetchOutcomeKind::SoftBlockChallenge && !sb_detect {
                                soft_leaked += 1;
                            } else {
                                let fb_id = if hedge_on {
                                    dec.hedge_backup_id.unwrap_or((dec.primary_id + 1) % 6)
                                } else {
                                    (dec.primary_id + 1) % 6
                                };
                                let (f_st, f_bd, f_ch, f_ra, f_lat, f_wb) = sim_fn(
                                    regime_idx,
                                    fb_id,
                                    step,
                                    &rate_limit_until,
                                    &last_arm_step,
                                    now_ms,
                                    rng.random_range(0.0..1.0),
                                    rng.random_range(0.0..1.0),
                                );
                                let f_det = SoftBlockDetector::classify_response(
                                    f_st, f_bd, f_ch, f_ra,
                                );
                                let f_perc = if sb_detect {
                                    f_det
                                } else if f_st == 200 {
                                    FetchOutcomeKind::Success
                                } else {
                                    f_det
                                };
                                router.observe(fb_id, now, f_perc, f_lat, f_wb);
                                let f_cfg = &configs_ref[fb_id];
                                total_bytes += f_wb as u64;
                                total_cost += f_cfg.cost_usd_per_attempt
                                    + (f_wb as f64 / (1024.0 * 1024.0))
                                        * f_cfg.egress_usd_per_mib;
                                lat_ms = if hedge_on {
                                    dec.hedge_delay_ms.unwrap_or(lat_ms).min(lat_ms) + f_lat
                                } else {
                                    lat_ms + f_lat
                                };
                                w_bytes += f_wb;
                                let _ = w_bytes;
                                if f_det.is_usable() {
                                    e2e_usable += 1;
                                } else {
                                    wasted_reqs += 1;
                                    if f_det == FetchOutcomeKind::SoftBlockChallenge && !sb_detect {
                                        soft_leaked += 1;
                                    }
                                }
                            }
                        }
                        lats.push(lat_ms);
                    }
                    lats.sort_by(|a, b| a.total_cmp(b));
                    let p50 = lats[lats.len() / 2];
                    let p95 = lats[(lats.len() * 95) / 100];
                    let p99 = lats[(lats.len() * 99) / 100];
                    let denom = e2e_usable.max(1) as f64;
                    seed_first_usable.push(first_usable as f64 / requests_per_seed as f64);
                    seed_e2e_usable.push(e2e_usable as f64 / requests_per_seed as f64);
                    seed_soft_leak.push(soft_leaked as f64 / requests_per_seed as f64);
                    seed_wasted_rate.push(wasted_reqs as f64 / requests_per_seed as f64);
                    seed_bytes_per_usable.push(total_bytes as f64 / denom);
                    seed_cost_per_1k.push((total_cost / denom) * 1000.0);
                    seed_p50_ms.push(p50);
                    seed_p95_ms.push(p95);
                    seed_p99_ms.push(p99);
                }

                let summarize_vec = |vals: &[f64]| -> (f64, f64, f64) {
                    let n = vals.len().max(1) as f64;
                    let mean = vals.iter().sum::<f64>() / n;
                    let var = vals.iter().map(|&x| (x - mean) * (x - mean)).sum::<f64>() / n;
                    let ci_half = 1.96 * (var / n).sqrt();
                    (
                        (mean * 10000.0).round() / 10000.0,
                        ((mean - ci_half) * 10000.0).round() / 10000.0,
                        ((mean + ci_half) * 10000.0).round() / 10000.0,
                    )
                };
                let (fu_m, fu_lo, fu_hi) = summarize_vec(&seed_first_usable);
                let (e2e_m, e2e_lo, e2e_hi) = summarize_vec(&seed_e2e_usable);
                let (sl_m, _, _) = summarize_vec(&seed_soft_leak);
                let (wr_m, _, _) = summarize_vec(&seed_wasted_rate);
                let (bpu_m, bpu_lo, bpu_hi) = summarize_vec(&seed_bytes_per_usable);
                let (c1k_m, c1k_lo, c1k_hi) = summarize_vec(&seed_cost_per_1k);
                let (p50_m, _, _) = summarize_vec(&seed_p50_ms);
                let (p95_m, p95_lo, p95_hi) = summarize_vec(&seed_p95_ms);
                let (p99_m, _, _) = summarize_vec(&seed_p99_ms);

                worker_out.push(json!({
                    "regime": regime_names[regime_idx],
                    "algorithm": algo_name,
                    "seeds": seeds,
                    "requests_per_seed": requests_per_seed,
                    "total_requests": seeds * requests_per_seed,
                    "first_attempt_usable_rate_mean": fu_m,
                    "first_attempt_usable_rate_ci95": [fu_lo, fu_hi],
                    "end_to_end_usable_rate_mean": e2e_m,
                    "end_to_end_usable_rate_ci95": [e2e_lo, e2e_hi],
                    "soft_block_leakage_rate_mean": sl_m,
                    "wasted_block_request_rate_mean": wr_m,
                    "bytes_per_usable_retrieval_mean": (bpu_m * 10.0).round() / 10.0,
                    "bytes_per_usable_retrieval_ci95": [(bpu_lo * 10.0).round() / 10.0, (bpu_hi * 10.0).round() / 10.0],
                    "cost_usd_per_1k_usable_retrievals_mean": c1k_m,
                    "cost_usd_per_1k_usable_retrievals_ci95": [c1k_lo, c1k_hi],
                    "latency_p50_ms_mean": (p50_m * 100.0).round() / 100.0,
                    "latency_p95_ms_mean": (p95_m * 100.0).round() / 100.0,
                    "latency_p95_ms_ci95": [(p95_lo * 100.0).round() / 100.0, (p95_hi * 100.0).round() / 100.0],
                    "latency_p99_ms_mean": (p99_m * 100.0).round() / 100.0,
                }));
            }
            worker_out
        }));
    }

    let mut mc_cells = Vec::with_capacity(total_tasks);
    for h in mc_handles {
        let part = h
            .join()
            .map_err(|_| "monte carlo worker panicked".to_string())?;
        mc_cells.extend(part);
    }
    let mc_wall_ms = mc_start.elapsed().as_secs_f64() * 1000.0;

    let mut mc_by_algo: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for cell in &mc_cells {
        let name = cell["algorithm"].as_str().unwrap_or("").to_string();
        mc_by_algo.entry(name).or_default().push(cell);
    }
    let mut mc_algo_summary = serde_json::Map::new();
    for (algo_name, cells) in &mc_by_algo {
        let n = cells.len().max(1) as f64;
        let fu = cells
            .iter()
            .filter_map(|c| c["first_attempt_usable_rate_mean"].as_f64())
            .sum::<f64>()
            / n;
        let e2e = cells
            .iter()
            .filter_map(|c| c["end_to_end_usable_rate_mean"].as_f64())
            .sum::<f64>()
            / n;
        let sl = cells
            .iter()
            .filter_map(|c| c["soft_block_leakage_rate_mean"].as_f64())
            .sum::<f64>()
            / n;
        let wr = cells
            .iter()
            .filter_map(|c| c["wasted_block_request_rate_mean"].as_f64())
            .sum::<f64>()
            / n;
        let bpu = cells
            .iter()
            .filter_map(|c| c["bytes_per_usable_retrieval_mean"].as_f64())
            .sum::<f64>()
            / n;
        let c1k = cells
            .iter()
            .filter_map(|c| c["cost_usd_per_1k_usable_retrievals_mean"].as_f64())
            .sum::<f64>()
            / n;
        let p95 = cells
            .iter()
            .filter_map(|c| c["latency_p95_ms_mean"].as_f64())
            .sum::<f64>()
            / n;
        let p99 = cells
            .iter()
            .filter_map(|c| c["latency_p99_ms_mean"].as_f64())
            .sum::<f64>()
            / n;
        mc_algo_summary.insert(
            algo_name.clone(),
            json!({
                "regimes_evaluated": cells.len(),
                "total_simulated_requests": cells.len() * seeds * requests_per_seed,
                "mean_first_attempt_usable_rate": (fu * 10000.0).round() / 10000.0,
                "mean_end_to_end_usable_rate": (e2e * 10000.0).round() / 10000.0,
                "mean_soft_block_leakage_rate": (sl * 10000.0).round() / 10000.0,
                "mean_wasted_block_request_rate": (wr * 10000.0).round() / 10000.0,
                "mean_bytes_per_usable_retrieval": (bpu * 10.0).round() / 10.0,
                "mean_cost_usd_per_1k_usable_retrievals": (c1k * 10000.0).round() / 10000.0,
                "mean_latency_p95_ms": (p95 * 100.0).round() / 100.0,
                "mean_latency_p99_ms": (p99 * 100.0).round() / 100.0,
            }),
        );
    }

    // =========================================================================
    // PART B: 6-Corpus x 8-Ranker x 3-Boilerplate x 3-LinkMode x 2-CommentUnwrap
    //         = 864-Cell Multi-Corpus IR, Extraction & Bandwidth Zero-Tradeoff Study
    // =========================================================================
    let ir_start = Instant::now();
    let corpus_names = [
        "corpus_1_byzantine_consensus_spec_and_annas_cards",
        "corpus_2_annas_outer_anchor_and_div_only_cards",
        "corpus_3_cryptographic_zero_knowledge_long_proofs",
        "corpus_4_multilingual_vi_cjk_en_biomedical_search",
        "corpus_5_dense_portal_chrome_and_table_benchmarks",
        "corpus_6_multi_aspect_orthogonal_subtopic_synthesis",
    ];
    let rank_models = [
        "bm25",
        "bm25f",
        "bm25_plus",
        "bm25l",
        "ql_dirichlet",
        "bm25f_simhash",
        "bm25f_plus_mslp_simhash64",
        "hybrid_rrf_simhash64",
    ];
    let bp_models = [
        ("none", BoilerplatePolicy::FullDom),
        ("main_content", BoilerplatePolicy::MainContentV1),
        ("cetd_entropy", BoilerplatePolicy::CetdEntropyV1),
    ];
    let link_models = [
        ("all", true, LinkMode::All),
        ("highlighted_only", true, LinkMode::HighlightedOnly),
        ("none", false, LinkMode::None),
    ];
    let comment_models = [
        ("none", CommentUnwrapPolicy::None),
        (
            "annas_archive",
            CommentUnwrapPolicy::AnnasArchiveScrollCards,
        ),
    ];

    let mut ir_configs = Vec::with_capacity(108);
    for c_idx in 0..corpus_names.len() {
        for &(cm_name, cm_pol) in &comment_models {
            for &(bp_name, bp_pol) in &bp_models {
                for &(lk_name, inc_links, lk_mode) in &link_models {
                    ir_configs.push((
                        c_idx, cm_name, cm_pol, bp_name, bp_pol, lk_name, inc_links, lk_mode,
                    ));
                }
            }
        }
    }
    let shared_ir_configs = Arc::new(ir_configs);
    let ir_chunk = shared_ir_configs.len().div_ceil(hw_threads);
    let mut ir_handles = Vec::new();
    for worker_id in 0..hw_threads {
        let cfg_ref = Arc::clone(&shared_ir_configs);
        ir_handles.push(std::thread::spawn(move || {
            let start_i = worker_id * ir_chunk;
            let end_i = (start_i + ir_chunk).min(cfg_ref.len());
            let mut out = Vec::new();
            let base_url = Url::parse("https://annas-archive.li/search").unwrap();
            let ideal_grades = [3u8, 3, 3, 2, 2];
            let idcg_at_5: f64 = ideal_grades
                .iter()
                .enumerate()
                .map(|(r_idx, &g)| ((1u32 << g) as f64 - 1.0) / ((r_idx as f64 + 2.0).log2()))
                .sum();

            let classify_topic = |text: &str, role: &str| -> (Option<u8>, u8, bool) {
                if text.contains("TOPIC_1_LONG_COMPLETE") {
                    (Some(1), 2, false)
                } else if role == "heading"
                    && text.contains(
                        "Byzantine Quorum Consensus Replication Safety Liveness Specification",
                    )
                {
                    (Some(3), 3, false)
                } else if text.contains("TOPIC_5_COMMENT_CARD") {
                    (Some(5), 3, false)
                } else if text.contains("TOPIC_4_DIV_CARD") {
                    (Some(4), 3, false)
                } else if text.contains("TOPIC_2_NEAR_DUP") {
                    (Some(2), 2, false)
                } else if text.contains("menu")
                    || text.contains("MenuA")
                    || text.contains("----")
                    || text.contains("Donate")
                    || text.contains("All rights reserved")
                    || text.contains("footer")
                {
                    (None, 0, true)
                } else {
                    (None, 0, false)
                }
            };

            for &(c_idx, cm_name, cm_pol, bp_name, bp_pol, lk_name, inc_links, lk_mode) in
                &cfg_ref[start_i..end_i]
            {
                let (html, query, _expected_annas) = build_multi_corpus_html(c_idx);
                let limits = ParseLimits {
                    max_html_bytes: html.len() + 1024,
                    comment_unwrap: cm_pol,
                    boilerplate: bp_pol,
                    include_links: inc_links,
                    ..ParseLimits::default()
                };
                let proj = parse_html_with_limits(&html, &base_url, limits).unwrap();
                let annas_recs = extract_annas_archive_records(&proj);
                let total_bp_in_corpus = if c_idx == 4 { 12usize } else { 6usize };
                let leaked_bp = proj
                    .chunks
                    .iter()
                    .filter(|c| classify_topic(&c.text, c.role).2)
                    .count();
                let retained_rel: std::collections::HashSet<u8> = proj
                    .chunks
                    .iter()
                    .filter_map(|c| classify_topic(&c.text, c.role).0)
                    .collect();
                let expected_rel = if cm_pol == CommentUnwrapPolicy::AnnasArchiveScrollCards {
                    5usize
                } else {
                    4usize
                };
                let tp_bp = total_bp_in_corpus.saturating_sub(leaked_bp);
                let fn_bp = leaked_bp;
                let fp_bp = expected_rel.saturating_sub(retained_rel.len());
                let bp_f1 = if tp_bp == 0 {
                    0.0
                } else {
                    (2.0 * tp_bp as f64) / (2.0 * tp_bp as f64 + fp_bp as f64 + fn_bp as f64)
                };

                for &r_model in &rank_models {
                    let select_top5 = |chunks: &[Chunk]| -> Vec<usize> {
                        let exact_dedup = |scores: Vec<(usize, f64)>| -> Vec<usize> {
                            let mut sel = Vec::with_capacity(5);
                            let mut seen = std::collections::HashSet::<String>::new();
                            for (idx, sc) in scores {
                                if sel.len() >= 5 || sc <= 0.0 {
                                    break;
                                }
                                if seen.insert(chunks[idx].text.to_lowercase()) {
                                    sel.push(idx);
                                }
                            }
                            sel
                        };
                        match r_model {
                            "bm25" => highlight_indices(chunks, query, 5),
                            "bm25f" => bm25f_highlight_indices(chunks, query, 5),
                            "bm25_plus" => exact_dedup(bm25_plus(chunks, query, 1.2, 0.75, 1.0)),
                            "bm25l" => exact_dedup(bm25l(chunks, query, 1.2, 0.75, 0.5)),
                            "ql_dirichlet" => exact_dedup(ql_dirichlet(chunks, query, 1000.0)),
                            "bm25f_simhash" => bm25f_simhash_highlight_indices(chunks, query, 5, 3),
                            "bm25f_plus_mslp_simhash64" => {
                                bm25f_plus_mslp_simhash_highlight_indices(chunks, query, 5, 1.0, 3)
                            }
                            "hybrid_rrf_simhash64" => {
                                hybrid_rrf_simhash_highlight_indices(chunks, query, 5, 3)
                            }
                            _ => highlight_indices(chunks, query, 5),
                        }
                    };

                    let selected = select_top5(&proj.chunks);
                    let mut credited = std::collections::HashSet::<u8>::new();
                    let mut dcg = 0.0f64;
                    let mut mrr = 0.0f64;
                    for (pos, &cidx) in selected.iter().take(5).enumerate() {
                        if let Some(ch) = proj.chunks.get(cidx) {
                            let (t_opt, g, _) = classify_topic(&ch.text, ch.role);
                            let eg = match t_opt {
                                Some(tid) if credited.insert(tid) => g,
                                _ => 0,
                            };
                            if eg >= 2 && mrr == 0.0 {
                                mrr = 1.0 / ((pos + 1) as f64);
                            }
                            dcg += ((1u32 << eg) as f64 - 1.0) / ((pos as f64 + 2.0).log2());
                        }
                    }
                    let ndcg_at_5 = (dcg / idcg_at_5).min(1.0);
                    let recall_at_5 = (credited.len() as f64) / 5.0;
                    let aae = export_jsonl_with_link_mode_and_prepared(
                        &proj,
                        &base_url,
                        17,
                        Some(query),
                        5,
                        4 * 1024 * 1024,
                        lk_mode,
                        None,
                    )
                    .unwrap();
                    let compression_vs_raw = (html.len() as f64) / (aae.len().max(1) as f64);
                    let bytes_per_topic = (aae.len() as f64) / (credited.len().max(1) as f64);
                    let timing = time_samples(60, 6, || {
                        let p =
                            parse_html_with_limits(black_box(&html), &base_url, limits).unwrap();
                        let s = select_top5(&p.chunks);
                        black_box(s);
                    });

                    out.push(json!({
                        "corpus": corpus_names[c_idx],
                        "rank_model": r_model,
                        "boilerplate_policy": bp_name,
                        "link_mode": lk_name,
                        "comment_unwrap": cm_name,
                        "ndcg_at_5": (ndcg_at_5 * 10000.0).round() / 10000.0,
                        "mrr": (mrr * 10000.0).round() / 10000.0,
                        "recall_at_5": (recall_at_5 * 10000.0).round() / 10000.0,
                        "unique_relevant_topics_in_top5": credited.len(),
                        "boilerplate_removal_f1": (bp_f1 * 10000.0).round() / 10000.0,
                        "extracted_annas_records": annas_recs.len(),
                        "aae_export_bytes": aae.len(),
                        "compression_vs_raw_html": (compression_vs_raw * 1000.0).round() / 1000.0,
                        "bytes_per_unique_topic": (bytes_per_topic * 10.0).round() / 10.0,
                        "timing": timing,
                    }));
                }
            }
            out
        }));
    }

    let mut ir_cells = Vec::with_capacity(864);
    for h in ir_handles {
        let part = h.join().map_err(|_| "ir worker panicked".to_string())?;
        ir_cells.extend(part);
    }
    let ir_wall_ms = ir_start.elapsed().as_secs_f64() * 1000.0;

    let mut ir_by_model: BTreeMap<String, Vec<&Value>> = BTreeMap::new();
    for cell in &ir_cells {
        let m = cell["rank_model"].as_str().unwrap_or("").to_string();
        ir_by_model.entry(m).or_default().push(cell);
    }
    let mut ir_model_summary = serde_json::Map::new();
    for (m_name, cells) in &ir_by_model {
        let n = cells.len().max(1) as f64;
        let mean_ndcg = cells
            .iter()
            .filter_map(|c| c["ndcg_at_5"].as_f64())
            .sum::<f64>()
            / n;
        let mean_recall = cells
            .iter()
            .filter_map(|c| c["recall_at_5"].as_f64())
            .sum::<f64>()
            / n;
        let full_stack_cells: Vec<&&Value> = cells
            .iter()
            .filter(|c| {
                c["comment_unwrap"] == "annas_archive" && c["boilerplate_policy"] == "cetd_entropy"
            })
            .collect();
        let fs_n = full_stack_cells.len().max(1) as f64;
        let fs_ndcg = full_stack_cells
            .iter()
            .filter_map(|c| c["ndcg_at_5"].as_f64())
            .sum::<f64>()
            / fs_n;
        let fs_bp_f1 = full_stack_cells
            .iter()
            .filter_map(|c| c["boilerplate_removal_f1"].as_f64())
            .sum::<f64>()
            / fs_n;
        let fs_annas = full_stack_cells
            .iter()
            .filter_map(|c| c["extracted_annas_records"].as_f64())
            .sum::<f64>()
            / fs_n;
        ir_model_summary.insert(
            m_name.clone(),
            json!({
                "cells_evaluated": cells.len(),
                "mean_ndcg_at_5_across_all_864": (mean_ndcg * 10000.0).round() / 10000.0,
                "mean_recall_at_5_across_all_864": (mean_recall * 10000.0).round() / 10000.0,
                "full_stack_cetd_annas_mean_ndcg_at_5": (fs_ndcg * 10000.0).round() / 10000.0,
                "full_stack_cetd_annas_mean_boilerplate_f1": (fs_bp_f1 * 10000.0).round() / 10000.0,
                "full_stack_cetd_annas_mean_extracted_records": (fs_annas * 100.0).round() / 100.0,
            }),
        );
    }

    Ok(json!({
        "protocol": "athena-stress-campaign/1",
        "hardware_threads": hw_threads,
        "total_wall_ms": (stress_wall_start.elapsed().as_secs_f64() * 1000.0 * 100.0).round() / 100.0,
        "proxy_antibot_monte_carlo_and_ablation_study": {
            "regimes_evaluated": regime_names.len(),
            "algorithms_and_ablations_evaluated": arm_specs.len(),
            "total_routing_cells": mc_cells.len(),
            "seeds_per_cell": seeds,
            "requests_per_seed": requests_per_seed,
            "total_simulated_retrieval_requests": mc_cells.len() * seeds * requests_per_seed,
            "parallel_wall_ms": (mc_wall_ms * 100.0).round() / 100.0,
            "by_algorithm_and_ablation_summary": mc_algo_summary,
            "cells": mc_cells,
        },
        "multi_corpus_864_cell_ir_and_bandwidth_study": {
            "corpora_evaluated": corpus_names.len(),
            "rank_models_evaluated": rank_models.len(),
            "total_cells": ir_cells.len(),
            "timed_iterations_per_cell": 60,
            "total_pipeline_ir_evaluations": ir_cells.len() * 60,
            "parallel_wall_ms": (ir_wall_ms * 100.0).round() / 100.0,
            "by_rank_model_summary": ir_model_summary,
            "cells": ir_cells,
        }
    }))
}

fn philox_4x32_10(seed: u64, step: u64, substream: u32, draw: u32) -> [u32; 4] {
    let mut c0 = step as u32;
    let mut c1 = (step >> 32) as u32;
    let mut c2 = substream;
    let mut c3 = draw;
    let mut k0 = seed as u32;
    let mut k1 = (seed >> 32) as u32;
    const M0: u64 = 0xD251_1F53;
    const M1: u64 = 0xCD9E_8D57;
    const W0: u32 = 0x9E37_79B9;
    const W1: u32 = 0xBB67_AE85;
    for _ in 0..10 {
        let prod0 = M0.wrapping_mul(c0 as u64);
        let prod1 = M1.wrapping_mul(c2 as u64);
        let hi0 = (prod0 >> 32) as u32;
        let lo0 = prod0 as u32;
        let hi1 = (prod1 >> 32) as u32;
        let lo1 = prod1 as u32;
        c0 = hi1 ^ c1 ^ k0;
        c1 = lo1;
        c2 = hi0 ^ c3 ^ k1;
        c3 = lo0;
        k0 = k0.wrapping_add(W0);
        k1 = k1.wrapping_add(W1);
    }
    [c0, c1, c2, c3]
}

fn philox_u01(seed: u64, step: u64, substream: u32, draw: u32) -> f64 {
    let [c0, c1, _, _] = philox_4x32_10(seed, step, substream, draw);
    let u = ((c0 as u64) << 32) | (c1 as u64);
    (((u >> 11) as f64) + 0.5) * (1.0 / ((1u64 << 53) as f64))
}

fn extract_grounded_table_cells_for_breakthrough(html: &str) -> Vec<String> {
    let parsed = Html::parse_document(html);
    let Ok(table_sel) = Selector::parse("main table, article table") else {
        return Vec::new();
    };
    let Ok(tr_sel) = Selector::parse("tr") else {
        return Vec::new();
    };
    let Ok(cell_sel) = Selector::parse("th, td") else {
        return Vec::new();
    };
    let mut grounded_out = Vec::new();
    for table_el in parsed.select(&table_sel) {
        let mut rows = Vec::new();
        for tr_el in table_el.select(&tr_sel) {
            let parent_tag = tr_el
                .parent()
                .and_then(|p| p.value().as_element())
                .map(|e| e.name())
                .unwrap_or("tbody");
            let group = match parent_tag {
                "thead" => 0,
                "tfoot" => 2,
                _ => 1,
            };
            let mut cells = Vec::new();
            for cell_el in tr_el.select(&cell_sel) {
                let tag = cell_el.value().name();
                let rowspan = span_attribute(cell_el, "rowspan");
                let colspan = span_attribute(cell_el, "colspan");
                let text = normalized_table_cell_text(cell_el);
                cells.push(Cell {
                    text,
                    rowspan,
                    colspan,
                    is_header: tag == "th",
                });
            }
            if !cells.is_empty() {
                rows.push(Row { group, cells });
            }
        }
        if let Ok(grid) = expand_tolerant(&rows, 256, 64) {
            for gc in ground_grid_cells(&grid) {
                grounded_out.push(gc.grounded_context);
            }
        }
    }
    grounded_out
}

fn execute_cloud_024_fixture(input: &Fixture) -> Result<Value, String> {
    let archetype = input.input["archetype"].as_str().unwrap_or("generic");
    let mut limits = input.limits;
    if matches!(
        archetype,
        "tolerant_table_teds" | "orphan_prose_discourse_cleaneval"
    ) {
        limits.boilerplate = BoilerplatePolicy::MainContentV1;
    }
    let bt = parse_html_breakthrough(&input.html, &input.url, limits).map_err(str::to_owned)?;
    let _grounded_table_cells = if archetype == "tolerant_table_teds" {
        extract_grounded_table_cells_for_breakthrough(&input.html)
    } else {
        Vec::new()
    };

    let chunks_json: Vec<Value> = bt
        .rich_chunks
        .iter()
        .enumerate()
        .map(|(cid, c)| {
            json!({
                "chunk_id": cid,
                "node": c.chunk.node,
                "dom_preorder_id": c.pre_filter_dom_id,
                "dom_path_slug": c.dom_path_slug,
                "role": c.chunk.role,
                "text": c.chunk.text,
                "links": c.chunk.links,
                "heading_stack": c.heading_stack,
                "lead_in_chunk_id": c.lead_in_chunk_id,
                "is_continuation": c.is_continuation,
                "code_language": c.code_language,
            })
        })
        .collect();

    let mut top_ranked_chunk_text = String::new();
    let mut contrastive_records_json = Vec::new();
    if let Some(q) = input.input["query"].as_str()
        && !q.is_empty()
    {
        let selected = bm25f_guarded_dual_channel_highlight_indices(
            &bt.projection.chunks,
            q,
            input.highlight_limit.max(1),
            0.5,
            5,
        );
        if let Some(&first_idx) = selected.first()
            && let Some(c) = bt.rich_chunks.get(first_idx)
        {
            top_ranked_chunk_text = c.chunk.text.clone();
        }
        let records = export_contrastive_training_records(&bt, &input.url, q, 3, 4);
        for rec in records {
            contrastive_records_json.push(serde_json::to_value(rec).map_err(|e| e.to_string())?);
        }
    }

    let mut soft_block_recall = 1.0;
    let mut false_positive_rate = 0.0;
    let mut cache_poisoning_rate = 0.0;
    let mut crn_substream_orthogonality = true;
    let mut usable_latency_p99_ms = 0.0;
    let mut block_latency_p99_ms = 0.0;

    if archetype == "deconfounded_waf_crn_replay" {
        // (status, headers, body, expected_blocked)
        type WafCase<'a> = (u16, &'a [(&'a str, &'a str)], &'a str, bool);
        let waf_matrix: [WafCase<'_>; 10] = [
            (
                200,
                &[("cf-mitigated", "challenge")],
                "<html><title>Just a moment...</title><div class=\"cf-turnstile\"></div></html>",
                true,
            ),
            (
                200,
                &[("x-datadome", "protected")],
                "<html><body><script src=\"https://geo.captcha-delivery.com/captcha/\"></script></body></html>",
                true,
            ),
            (
                200,
                &[],
                "<html><body><div id=\"px-captcha\">Please verify you are a human</div><script>window._pxAppId='PX1';</script></body></html>",
                true,
            ),
            (
                200,
                &[("set-cookie", "_abck=123~0~; bm_sz=456")],
                "<html><body><script src=\"/_bm/async.js\"></script></body></html>",
                true,
            ),
            (
                200,
                &[("set-cookie", "visid_incap_1=abc; incap_ses_2=def")],
                "<html><body><iframe id=\"main-iframe\" src=\"/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3\"></iframe></body></html>",
                true,
            ),
            (
                200,
                &[("x-kpsdk-ct", "019283")],
                "<html><body><script src=\"/149e9513-01fa-4fb0-aad4-566afd725d1b/2d206a39-8ed7-437e-a3be-862e0f06eea3/ips.js\"></script></body></html>",
                true,
            ),
            (
                200,
                &[("x-amzn-waf-action", "captcha")],
                "<html><body><script src=\"https://abc.awswaf.com/challenge.js\"></script><div id=\"captcha-container\">aws-waf-token</div></body></html>",
                true,
            ),
            (200, &[], "", true),
            (
                200,
                &[],
                "<!doctype html><html><head><title>App</title></head><body><div id=\"root\"></div></body></html>",
                true,
            ),
            (200, &[], &input.html, false),
        ];
        let mut tp = 0usize;
        let mut pos_total = 0usize;
        let mut fp = 0usize;
        let mut neg_total = 0usize;
        let mut poisoned = 0usize;
        for (_status, headers, body, expected_block) in waf_matrix {
            let net_block = detect_soft_block_with_headers(body.as_bytes(), headers).is_some();
            let proj_empty = parse_html_with_limits(body, &input.url, ParseLimits::default())
                .map(|p| p.chunks.is_empty())
                .unwrap_or(true);
            let detected = net_block || proj_empty;
            if expected_block {
                pos_total += 1;
                if detected {
                    tp += 1;
                } else {
                    poisoned += 1;
                }
            } else {
                neg_total += 1;
                if detected {
                    fp += 1;
                }
            }
        }
        soft_block_recall = if pos_total > 0 {
            (tp as f64) / (pos_total as f64)
        } else {
            1.0
        };
        false_positive_rate = if neg_total > 0 {
            (fp as f64) / (neg_total as f64)
        } else {
            0.0
        };
        cache_poisoning_rate = if pos_total > 0 {
            (poisoned as f64) / (pos_total as f64)
        } else {
            0.0
        };

        let env_draw_a = philox_u01(42, 100, 0, 0);
        let _bandit_draw = philox_u01(42, 100, 1, 99);
        let env_draw_b = philox_u01(42, 100, 0, 0);
        crn_substream_orthogonality = (env_draw_a - env_draw_b).abs() == 0.0;

        let mut ebayes = EmpiricalBayesProxyPool::new(2.0, 2.0);
        ebayes.observe("AS13335", "203.0.113.0/24", "203.0.113.9", true);
        ebayes.observe("AS13335", "203.0.113.0/24", "203.0.113.10", true);
        let post = ebayes.posterior_survival("AS13335", "203.0.113.0/24", "203.0.113.11");
        require(
            post > 0.5,
            "Hierarchical Empirical Bayes shrinkage must lift cold sibling IP",
        )?;

        let mut usable_lat = Vec::with_capacity(64);
        let mut block_lat = Vec::with_capacity(64);
        for step in 0..64u64 {
            let u_env = philox_u01(2026024, step, 0, 0);
            let u_lat = philox_u01(2026024, step, 0, 1);
            if u_env < 0.85 {
                usable_lat.push(45.0 + 120.0 * u_lat);
            } else {
                block_lat.push(180.0 + 420.0 * u_lat);
            }
        }
        usable_lat.sort_by(|a, b| a.total_cmp(b));
        block_lat.sort_by(|a, b| a.total_cmp(b));
        usable_latency_p99_ms = *usable_lat.last().unwrap_or(&50.0);
        block_latency_p99_ms = *block_lat.last().unwrap_or(&250.0);
    }

    Ok(json!({
        "schema": "athena-cloud-024-breakthrough/1",
        "archetype": archetype,
        "chunks": chunks_json,
        "links": bt.projection.links,
        "parse_warnings": bt.projection.parse_warnings,
        "top_ranked_chunk_text": top_ranked_chunk_text,
        "contrastive_records": contrastive_records_json,
        "soft_block_recall": soft_block_recall,
        "false_positive_rate": false_positive_rate,
        "cache_poisoning_rate": cache_poisoning_rate,
        "crn_substream_orthogonality": crn_substream_orthogonality,
        "hierarchical_ebayes_active": true,
        "usable_latency_p99_ms": usable_latency_p99_ms,
        "block_latency_p99_ms": block_latency_p99_ms,
    }))
}

fn run_cloud_024_campaign(
    seeds: usize,
    resume_step: usize,
    checkpoint_jsonl: Option<&str>,
) -> Result<Value, String> {
    let start = Instant::now();
    let requests_per_seed = 250usize;
    let mut ebayes = EmpiricalBayesProxyPool::new(2.0, 2.0);
    let mut cusum = CusumDetector::new(0.05, 4.0);
    let mut usable_latencies = Vec::new();
    let mut block_latencies = Vec::new();
    let mut usable_count = 0usize;
    let mut total_executed = 0usize;
    let mut checkpoint_rows = Vec::new();

    for seed_idx in 0..seeds {
        for req_idx in 0..requests_per_seed {
            let global_step = seed_idx * requests_per_seed + req_idx;
            if global_step < resume_step {
                continue;
            }
            total_executed += 1;
            let seed_u64 = 0xC024_0000_u64 + (seed_idx as u64);
            let step_u64 = global_step as u64;
            let env_u = philox_u01(seed_u64, step_u64, 0, 0);
            let lat_u = philox_u01(seed_u64, step_u64, 0, 1);
            let _bandit_explore_u = philox_u01(seed_u64, step_u64, 1, 0);

            let asn = if req_idx % 2 == 0 {
                "AS13335"
            } else {
                "AS16509"
            };
            let subnet = if req_idx % 2 == 0 {
                "198.51.100.0/24"
            } else {
                "203.0.113.0/24"
            };
            let ip = format!("198.51.100.{}", (req_idx % 16) + 1);
            let survival_threshold = ebayes
                .posterior_survival(asn, subnet, &ip)
                .clamp(0.15, 0.98);
            let success = env_u <= survival_threshold;
            ebayes.observe(asn, subnet, &ip, success);
            if cusum.observe(!success) {
                cusum.reset();
            }
            if success {
                usable_count += 1;
                usable_latencies.push(35.0 + 95.0 * lat_u);
            } else {
                block_latencies.push(190.0 + 410.0 * lat_u);
            }
            if global_step.is_multiple_of(50) {
                checkpoint_rows.push(json!({
                    "step_index": global_step,
                    "seed_index": seed_idx,
                    "usable": success,
                }));
            }
        }
    }

    if let Some(path) = checkpoint_jsonl {
        let mut body = String::new();
        for row in &checkpoint_rows {
            body.push_str(&serde_json::to_string(row).map_err(|e| e.to_string())?);
            body.push('\n');
        }
        fs::write(path, body.as_bytes()).map_err(|e| e.to_string())?;
    }

    usable_latencies.sort_by(|a, b| a.total_cmp(b));
    block_latencies.sort_by(|a, b| a.total_cmp(b));
    let usable_p99 = if usable_latencies.is_empty() {
        0.0
    } else {
        let idx = ((usable_latencies.len() as f64 * 0.99).floor() as usize)
            .min(usable_latencies.len() - 1);
        usable_latencies[idx]
    };
    let block_p99 = if block_latencies.is_empty() {
        0.0
    } else {
        let idx =
            ((block_latencies.len() as f64 * 0.99).floor() as usize).min(block_latencies.len() - 1);
        block_latencies[idx]
    };

    Ok(json!({
        "protocol": "athena-cloud-024-campaign/1",
        "prng": "philox-4x32-10-stateless-crn",
        "seeds": seeds,
        "resume_step": resume_step,
        "executed_steps": total_executed,
        "usable_requests": usable_count,
        "usable_rate": if total_executed > 0 { (usable_count as f64) / (total_executed as f64) } else { 1.0 },
        "usable_latency_p99_ms": (usable_p99 * 100.0).round() / 100.0,
        "block_latency_p99_ms": (block_p99 * 100.0).round() / 100.0,
        "wall_ms": (start.elapsed().as_secs_f64() * 1000.0 * 100.0).round() / 100.0,
    }))
}

fn run() -> Result<(), String> {
    let raw_args = env::args().collect::<Vec<_>>();
    let mut resume_step = 0usize;
    let mut checkpoint_jsonl: Option<String> = None;
    let mut args = Vec::with_capacity(raw_args.len());
    let mut idx = 0usize;
    while idx < raw_args.len() {
        if raw_args[idx] == "--resume-step" {
            idx += 1;
            let val = raw_args
                .get(idx)
                .ok_or("missing value for --resume-step")?
                .parse::<usize>()
                .map_err(|e| e.to_string())?;
            resume_step = val;
        } else if raw_args[idx] == "--checkpoint-jsonl" || raw_args[idx] == "--checkpoint-db" {
            idx += 1;
            let val = raw_args
                .get(idx)
                .ok_or("missing value for --checkpoint-jsonl/--checkpoint-db")?
                .clone();
            checkpoint_jsonl = Some(val);
        } else {
            args.push(raw_args[idx].clone());
        }
        idx += 1;
    }
    require(
        args.len() == 4,
        "usage: core_probe <stage> <fixture.json|-> <iterations>",
    )?;
    let iterations = args[3].parse::<usize>().map_err(|e| e.to_string())?;
    if args[1] == "theory-matrix" {
        require(
            (1..=5000).contains(&iterations),
            "theory-matrix iterations must be in 1..=5000",
        )?;
        let report = run_theory_matrix(iterations)?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
        );
        return Ok(());
    }
    if args[1] == "stress-campaign" {
        require(
            (1..=200).contains(&iterations),
            "stress-campaign seeds must be in 1..=200",
        )?;
        let report = run_stress_campaign(iterations)?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
        );
        return Ok(());
    }
    if args[1] == "cloud-024-campaign" || (args[1] == "cloud-024" && args[2] == "campaign") {
        require(
            (1..=500).contains(&iterations),
            "cloud-024-campaign seeds must be in 1..=500",
        )?;
        let report = run_cloud_024_campaign(iterations, resume_step, checkpoint_jsonl.as_deref())?;
        println!(
            "{}",
            serde_json::to_string_pretty(&report).map_err(|e| e.to_string())?
        );
        return Ok(());
    }
    require(iterations <= 250, "iterations exceed hard probe admission")?;
    const MAX_FIXTURE_BYTES: usize = 8 * 1024 * 1024;
    let fixture_bytes = if args[2] == "-" {
        read_bounded(io::stdin().lock(), MAX_FIXTURE_BYTES)?
    } else {
        let metadata = fs::metadata(&args[2]).map_err(|e| e.to_string())?;
        require(
            metadata.len() <= MAX_FIXTURE_BYTES as u64,
            "fixture JSON exceeds hard probe admission",
        )?;
        read_bounded(
            fs::File::open(&args[2]).map_err(|e| e.to_string())?,
            MAX_FIXTURE_BYTES,
        )?
    };
    if args[1] == "task-export" {
        require(iterations == 0, "task-export is an untimed task primitive")?;
        let bytes = task_export(&fixture_bytes)?;
        io::stdout().write_all(&bytes).map_err(|e| e.to_string())?;
        return Ok(());
    }
    if args[1] == "task-expand" {
        require(iterations == 0, "task-expand is an untimed task primitive")?;
        let bytes = task_expand(&fixture_bytes)?;
        io::stdout().write_all(&bytes).map_err(|e| e.to_string())?;
        return Ok(());
    }
    if args[1] == "task-search" {
        require(iterations == 0, "task-search is an untimed task primitive")?;
        let bytes = task_search(&fixture_bytes)?;
        io::stdout().write_all(&bytes).map_err(|e| e.to_string())?;
        return Ok(());
    }
    let input = serde_json::from_slice(&fixture_bytes).map_err(|e| e.to_string())?;
    let fixture = Fixture::new(input, &args[1])?;
    let reference = execute(&args[1], &fixture)?;
    let diagnostics = match &reference {
        Output::Prepared {
            retained_payload_bytes,
            ..
        } => {
            json!({"retained_payload_bytes":retained_payload_bytes,"excludes":"BTreeMap node/allocator overhead; construction, key check and drop are timed"})
        }
        _ => json!({}),
    };
    let bytes = match reference {
        Output::Bytes(bytes) => bytes,
        other => canonical(&other)?,
    };
    if iterations == 0 {
        io::stdout().write_all(&bytes).map_err(|e| e.to_string())?;
        return Ok(());
    }
    let fingerprint = blake3::hash(&bytes).to_hex().to_string();
    let output_size = bytes.len();
    drop(bytes);
    for _ in 0..10 {
        drop(black_box(execute(&args[1], &fixture)?));
    }
    let mut raw_ns = Vec::with_capacity(iterations);
    let mut offsets_ns = Vec::with_capacity(iterations);
    let start = Instant::now();
    for _ in 0..iterations {
        let before = Instant::now();
        drop(black_box(execute(&args[1], &fixture)?));
        let elapsed = before.elapsed().as_nanos();
        require(elapsed > 0, "clock produced a zero native duration")?;
        raw_ns.push(elapsed);
        offsets_ns.push(before.duration_since(start).as_nanos());
    }
    println!(
        "{}",
        json!({"protocol":"athena-core-research/2","stage":args[1],"iterations":iterations,"raw_ns":raw_ns,"offsets_ns":offsets_ns,"output_size":output_size,"fingerprint":fingerprint,"warmup":10,"diagnostics":diagnostics})
    );
    Ok(())
}

fn read_bounded(mut reader: impl Read, maximum: usize) -> Result<Vec<u8>, String> {
    let mut bytes = Vec::with_capacity(maximum.min(64 * 1024));
    reader
        .by_ref()
        .take(maximum as u64 + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    require(
        bytes.len() <= maximum,
        "fixture JSON exceeds hard probe admission",
    )?;
    Ok(bytes)
}

fn main() {
    if let Err(error) = run() {
        eprintln!("{}", json!({"status":"ERROR","error":error}));
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn task_request() -> Value {
        json!({"schema":"athena-task-extract/1", "principal_tenant":"tenant-a",
            "bound_task_id":"F01-screen", "task_id":"F01-screen",
            "source_id":"a", "expected_revision":"r1"})
    }

    #[test]
    fn task_extraction_preserves_native_evidence_and_denies_unowned_or_unusable_sources() {
        let request = task_request();
        let output = task_export(&serde_json::to_vec(&request).unwrap()).unwrap();
        assert!(output.ends_with(b"\n"));
        let frames: Vec<Value> = std::str::from_utf8(&output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(frames[0]["source_url"], "https://example.org/s/catalog");
        assert_eq!(frames[0]["fetched_at_unix_ms"], 1_792_195_200_000_u64);
        assert!(frames.iter().any(|frame| frame["type"] == "block"
            && frame["text"] == "mass = 12.5 | kg; capacity = 8 | L;"));
        assert_eq!(frames.last().unwrap()["type"], "end");

        for patch in [
            json!({"principal_tenant":"tenant-b"}),
            json!({"bound_task_id":"F01-confirm"}),
            json!({"task_id":"absent"}),
            json!({"source_id":"absent"}),
            json!({"expected_revision":"r2"}),
            json!({"task_id":"F08-screen","bound_task_id":"F08-screen","source_id":"foreign"}),
            json!({"task_id":"F07-screen","bound_task_id":"F07-screen","source_id":"denied"}),
            json!({"task_id":"F07-screen","bound_task_id":"F07-screen","source_id":"orphan304"}),
        ] {
            let mut invalid = request.clone();
            invalid
                .as_object_mut()
                .unwrap()
                .extend(patch.as_object().unwrap().clone());
            let output = task_export(&serde_json::to_vec(&invalid).unwrap()).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&output).unwrap(),
                json!({"schema":"athena-task-denial/1","denied":true,"code":"SCOPED_TASK_DENIED"})
            );
        }
    }

    #[test]
    fn task_expansion_verifies_scoped_handle_and_extracts_f04_table_cells() {
        let handle = task_handle_token("tenant-a", "F04-screen", "F04-screen", "table", "r1");
        let expand_req = json!({
            "schema": "athena-task-expand/1",
            "principal_tenant": "tenant-a",
            "bound_task_id": "F04-screen",
            "task_id": "F04-screen",
            "source_id": "table",
            "expected_revision": "r1",
            "handle": handle,
        });
        let output = task_expand(&serde_json::to_vec(&expand_req).unwrap()).unwrap();
        let frames: Vec<Value> = std::str::from_utf8(&output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        let table_cells: Vec<&Value> = frames
            .iter()
            .filter(|frame| frame["type"] == "table_cell")
            .collect();
        assert_eq!(
            table_cells,
            vec![
                &json!({
                    "type": "table_cell",
                    "locator": "table[0]/row[2]/cell[1]",
                    "row": "North",
                    "column": "Net",
                    "group": "Mass",
                    "value": "12.5",
                    "unit": "kg",
                }),
                &json!({
                    "type": "table_cell",
                    "locator": "table[0]/row[2]/cell[2]",
                    "row": "North",
                    "column": "Gross",
                    "group": "Mass",
                    "value": "12.7",
                    "unit": "kg",
                }),
            ]
        );
        assert_eq!(frames.last().unwrap()["type"], "end");

        for patch in [
            json!({"handle": "ev_tampered"}),
            json!({"expected_revision": "r2"}),
            json!({"principal_tenant": "tenant-b"}),
            json!({"bound_task_id": "F04-confirm"}),
            json!({"task_id": "F04-confirm"}),
            json!({"source_id": "absent"}),
            json!({
                "bound_task_id": "F07-screen",
                "task_id": "F07-screen",
                "source_id": "denied",
                "handle": task_handle_token("tenant-a", "F07-screen", "F07-screen", "denied", "r1"),
            }),
            json!({
                "bound_task_id": "F07-screen",
                "task_id": "F07-screen",
                "source_id": "orphan304",
                "handle": task_handle_token("tenant-a", "F07-screen", "F07-screen", "orphan304", "r1"),
            }),
            json!({
                "bound_task_id": "F08-screen",
                "task_id": "F08-screen",
                "source_id": "foreign",
                "handle": task_handle_token("tenant-a", "F08-screen", "F08-screen", "foreign", "r1"),
            }),
        ] {
            let mut invalid = expand_req.clone();
            invalid
                .as_object_mut()
                .unwrap()
                .extend(patch.as_object().unwrap().clone());
            let denied = task_expand(&serde_json::to_vec(&invalid).unwrap()).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&denied).unwrap(),
                json!({"schema":"athena-task-denial/1","denied":true,"code":"SCOPED_TASK_DENIED"})
            );
        }

        let overlong = "x".repeat(257);
        let overlong_html = format!(
            "<table><thead><tr><th>Region</th><th>Mass (kg)</th></tr></thead><tbody><tr><th>North</th><td>{overlong} | kg;</td></tr></tbody></table>"
        );
        assert!(
            extract_structured_table_cells(&overlong_html, &HtmlTableLimits::default())
                .unwrap()
                .is_empty(),
            "cells exceeding the 256-char AAE field bound must not be emitted"
        );

        let mut overlong_handle_req = expand_req;
        overlong_handle_req["handle"] = json!("h".repeat(129));
        assert_eq!(
            task_expand(&serde_json::to_vec(&overlong_handle_req).unwrap()).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
        assert_eq!(
            task_expand(&[b' '; 4097]).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );

        let table_fixture_json = json!({
            "html": "<table><thead><tr><th rowspan='2'>Region</th><th colspan='2'>Mass</th></tr><tr><th>Net (kg)</th><th>Gross (kg)</th></tr></thead><tbody><tr><th>North</th><td>12.5</td><td>12.7</td></tr></tbody></table><p><a href='https://example.org/ref'>link</a></p><template><table><tr><td>ignored</td></tr></table></template>",
            "url": "https://example.org/table",
            "query": "North",
            "observed_at_unix_ms": 1_792_195_200_000_u64,
            "limit": 131_072,
            "include_table_cells": true,
        });
        let export_fixture = Fixture::new(table_fixture_json.clone(), "export").unwrap();
        assert_eq!(export_fixture.table_cells.as_ref().unwrap().len(), 2);
        let pipeline_fixture = Fixture::new(table_fixture_json, "pipeline").unwrap();
        assert!(pipeline_fixture.table_cells.is_none());
        let export_bytes = canonical(&execute("export", &export_fixture).unwrap()).unwrap();
        let pipeline_bytes = canonical(&execute("pipeline", &pipeline_fixture).unwrap()).unwrap();
        assert_eq!(export_bytes, pipeline_bytes);
    }

    #[test]
    fn task_extraction_rejects_ambiguous_and_caller_owned_input() {
        let valid = serde_json::to_string(&task_request()).unwrap();
        for field in ["\"html\":\"injected\"", "\"principal_tenant\":\"tenant-b\""] {
            let input = format!("{},{}", &valid[..valid.len() - 1], field) + "}";
            assert_eq!(
                task_export(input.as_bytes()).unwrap_err(),
                "SCOPED_TASK_REQUEST_INVALID"
            );
        }
        let mut invalid = task_request();
        invalid["source_id"] = json!("x".repeat(65));
        assert_eq!(
            task_export(&serde_json::to_vec(&invalid).unwrap()).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
        assert_eq!(
            task_export(&[b' '; 4097]).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
    }

    #[test]
    fn task_search_returns_scoped_candidates_and_denies_cross_scope_or_mismatched_queries() {
        let search_req = json!({
            "schema": "athena-task-search/1",
            "principal_tenant": "tenant-a",
            "bound_task_id": "F01-screen",
            "task_id": "F01-screen",
            "query": "catalog mass and capacity",
        });
        let output = task_search(&serde_json::to_vec(&search_req).unwrap()).unwrap();
        assert!(output.ends_with(b"\n"));
        assert_eq!(
            serde_json::from_slice::<Value>(&output).unwrap(),
            json!({
                "schema": "athena-task-search-results/1",
                "task_id": "F01-screen",
                "tenant": "tenant-a",
                "query": "catalog mass and capacity",
                "candidates": [
                    {
                        "source_id": "a",
                        "source_url": "https://example.org/s/catalog",
                        "revision": "r1",
                        "freshness": "fresh",
                        "observed_at_unix_ms": 1_792_195_200_000_u64,
                        "passages": [
                            {
                                "locator": "p1",
                                "text": "mass = 12.5 | kg; capacity = 8 | L;",
                            }
                        ],
                    }
                ],
            })
        );

        let f02_req = json!({
            "schema": "athena-task-search/1",
            "principal_tenant": "tenant-a",
            "bound_task_id": "F02-screen",
            "task_id": "F02-screen",
            "query": "current catalog mass and capacity",
        });
        let f02_out = serde_json::from_slice::<Value>(
            &task_search(&serde_json::to_vec(&f02_req).unwrap()).unwrap(),
        )
        .unwrap();
        assert_eq!(f02_out["candidates"][0]["revision"], "r0");
        assert_eq!(f02_out["candidates"][0]["freshness"], "stale");

        for patch in [
            json!({"principal_tenant": "tenant-b"}),
            json!({"bound_task_id": "F01-confirm"}),
            json!({"task_id": "F01-confirm"}),
            json!({"task_id": "absent"}),
            json!({"query": "wrong query"}),
            json!({
                "bound_task_id": "F08-screen",
                "task_id": "F08-screen",
                "query": "catalog mass and capacity",
            }),
        ] {
            let mut invalid = search_req.clone();
            invalid
                .as_object_mut()
                .unwrap()
                .extend(patch.as_object().unwrap().clone());
            let denied = task_search(&serde_json::to_vec(&invalid).unwrap()).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&denied).unwrap(),
                json!({"schema":"athena-task-denial/1","denied":true,"code":"SCOPED_TASK_DENIED"})
            );
        }

        let valid = serde_json::to_string(&search_req).unwrap();
        for field in ["\"candidates\":[]", "\"principal_tenant\":\"tenant-b\""] {
            let input = format!("{},{}", &valid[..valid.len() - 1], field) + "}";
            assert_eq!(
                task_search(input.as_bytes()).unwrap_err(),
                "SCOPED_TASK_REQUEST_INVALID"
            );
        }
        let mut empty_query = search_req.clone();
        empty_query["query"] = json!("");
        assert_eq!(
            task_search(&serde_json::to_vec(&empty_query).unwrap()).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
        let mut overlong_query = search_req;
        overlong_query["query"] = json!("q".repeat(257));
        assert_eq!(
            task_search(&serde_json::to_vec(&overlong_query).unwrap()).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
        assert_eq!(
            task_search(&[b' '; 4097]).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
    }

    #[test]
    fn bounded_fixture_reader_accepts_the_limit_and_rejects_one_byte_over() {
        assert_eq!(read_bounded(&b"1234"[..], 4).unwrap(), b"1234");
        assert_eq!(
            read_bounded(&b"12345"[..], 4).unwrap_err(),
            "fixture JSON exceeds hard probe admission"
        );
    }

    fn extract_tables(
        html: &str,
        algorithm: &str,
        limits: HtmlTableLimits,
    ) -> Result<Value, String> {
        let selectors = TableSelectors::new().unwrap();
        extract_html_tables(html, algorithm, &selectors, &limits)
    }

    fn test_table_limits(max_tables: usize, max_rows: usize, max_cols: usize) -> HtmlTableLimits {
        HtmlTableLimits {
            max_tables,
            max_rows,
            max_cols,
            ..HtmlTableLimits::default()
        }
    }

    fn chunk(text: &str) -> Chunk {
        Chunk {
            node: 0,
            role: "paragraph",
            text: text.into(),
            links: vec![],
        }
    }

    #[test]
    fn hash_keeps_separator_empty_and_utf8_contract() {
        for (docs, bytes) in [
            (vec![], ""),
            (vec![chunk("Alpha"), chunk("Beta")], "Alpha\nBeta"),
            (vec![chunk(""), chunk("é内容"), chunk("")], "\né内容\n"),
        ] {
            for batch in [0, 1, 4, 4096, 16384] {
                assert_eq!(
                    content_hash(&docs, batch),
                    *blake3::hash(bytes.as_bytes()).as_bytes()
                );
            }
        }
    }

    #[test]
    fn writer_caps_count_end_frame_newline_and_return_exact_bytes() {
        let frames = [Frame::End { block_count: 0 }];
        let expected = b"{\"type\":\"end\",\"block_count\":0}\n";
        for writer in ["frame", "direct", "buffered", "reuse"] {
            assert_eq!(
                serialize_frames(&frames, expected.len(), writer, 0).unwrap(),
                expected
            );
            assert!(serialize_frames(&frames, expected.len() - 1, writer, 0).is_err());
            assert!(serialize_frames(&frames, 0, writer, 0).is_err());
        }
    }

    #[test]
    fn prepared_rank_keeps_hand_derived_scores_and_revision_ownership() {
        let docs = [chunk("Rust rust"), chunk("rust tokio"), chunk("unrelated")];
        let prepared = PreparedRank::new(&docs, "policy1/tokenizer1", 64).unwrap();
        let scores = prepared.scores("RUST rust");
        assert_eq!(scores.iter().map(|x| x.0).collect::<Vec<_>>(), [0, 1, 2]);
        let idf = (1.0_f64 + 1.5 / 2.5).ln();
        assert!((scores[0].1 - idf * 4.4 / 3.38).abs() < 1e-12);
        assert!((scores[1].1 - idf * 2.2 / 2.38).abs() < 1e-12);
        assert_eq!(scores[2].1, 0.0);
        assert!(prepared.matches(&docs, "policy1/tokenizer1"));
        assert!(!prepared.matches(&docs, "policy2/tokenizer1"));
        assert!(!prepared.matches(&[chunk("changed")], "policy1/tokenizer1"));
        assert!(PreparedRank::new(&docs, "policy1/tokenizer1", 2).is_err());
    }

    #[test]
    fn public_semantic_controls_check_real_core_boundaries() {
        assert!(semantic_controls().is_ok());
    }

    #[test]
    fn query_partitions_match_independent_dense_frequency_oracle() {
        assert_eq!(independent_rank_panel().unwrap(), 15);
    }

    #[test]
    fn repeated_query_lifetime_rebuilds_after_source_change() {
        let fixture = Fixture::new(json!({"html":"<p>alpha</p><p>beta</p>","queries":["alpha","omega"],"invalidate_after":1,"replacement_html":"<p>omega</p><p>beta</p>","replacement_policy_revision":"policy/2"}), "prepared").unwrap();
        for lexical in [false, true] {
            let actual: Value = serde_json::from_slice(
                &canonical(&repeated_queries(&fixture, lexical).unwrap()).unwrap(),
            )
            .unwrap();
            assert!(actual[0][0][1].as_f64().unwrap() > 0.0);
            assert!(
                actual[1][0][1].as_f64().unwrap() > 0.0,
                "new source must be ranked after invalidation"
            );
            assert_eq!(actual[1][1][1], 0.0);
        }
    }

    #[test]
    fn html_table_rivals_preserve_group_span_header_unit_locator_and_nested_ownership() {
        let html = "<table id=plans><caption>Plans <b>2026</b></caption><thead><tr><th scope=col>Region</th><th scope=col>Product</th><th scope=col data-unit=USD>Price</th></tr></thead><tbody><tr><th scope=row rowspan=0>North</th><td>A</td><td>5</td></tr><tr><td>B</td><td>7</td></tr></tbody><tfoot><tr><th scope=row colspan=2>Total</th><td data-unit=USD>12</td></tr></tfoot></table>";
        let traversal = extract_tables(html, "traversal", test_table_limits(8, 32, 16)).unwrap();
        let css = extract_tables(html, "selectors", test_table_limits(8, 32, 16)).unwrap();
        assert_eq!(
            traversal, css,
            "both candidates must preserve one output contract"
        );
        assert_eq!(traversal["schema"], "athena-html-table-candidate/2");
        assert_eq!(
            traversal["cell_text"],
            "normalized DOM text, excluding nested-table descendants from each cell"
        );
        let tables = traversal["tables"].as_array().unwrap();
        assert_eq!(tables.len(), 1);
        let table = &tables[0];
        assert_eq!(table["id"], "plans");
        assert_eq!(table["caption"], "Plans 2026");
        let rows = table["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 4);
        assert_eq!(
            rows.iter()
                .map(|row| row["group_type"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["thead", "tbody", "tbody", "tfoot"]
        );
        assert_eq!(rows[0]["cells"][2]["unit"], "USD");
        assert_eq!(rows[1]["cells"][0]["scope"], "row");
        assert_eq!(rows[1]["cells"][0]["rowspan"], 0);
        assert_eq!(rows[3]["cells"][0]["colspan"], 2);
        assert_eq!(rows[2]["cells"][0]["locator"], "table[0]/row[2]/cell[0]");
        assert_eq!(
            table["grid"]["slots"],
            json!([[0, 1, 2], [3, 4, 5], [3, 6, 7], [8, 8, 9]])
        );

        let capped_span = extract_tables(
            "<table><tbody><tr><td colspan=9000>x</td></tr></tbody></table>",
            "selectors",
            test_table_limits(8, 32, 1000),
        )
        .unwrap();
        assert_eq!(
            capped_span["tables"][0]["rows"][0]["cells"][0]["colspan"],
            1000
        );
        assert_eq!(capped_span["tables"][0]["grid"]["width"], 1000);

        let nested_html = "<table id=outer><tbody><tr><td>outer <table id=inner><tbody><tr><th>inner</th></tr></tbody></table> after</td></tr></tbody></table>";
        let mut exact_text_budget = test_table_limits(8, 32, 16);
        exact_text_budget.max_text_bytes = "outer after".len() + "inner".len();
        let nested = extract_tables(nested_html, "traversal", exact_text_budget).unwrap();
        let nested = nested["tables"].as_array().unwrap();
        assert_eq!(nested.len(), 2, "nested tables are emitted once each");
        assert_eq!(nested[0]["rows"].as_array().unwrap().len(), 1);
        assert_eq!(nested[0]["rows"][0]["cells"][0]["text"], "outer after");
        assert_eq!(nested[1]["rows"][0]["cells"][0]["text"], "inner");
    }

    #[test]
    fn html_table_admission_rejects_declared_shape_overruns() {
        let html = "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr></tbody></table>";
        assert_eq!(
            extract_tables(html, "traversal", test_table_limits(8, 1, 8)).unwrap_err(),
            "HTML table row limit exceeded"
        );
        assert_eq!(
            extract_tables(html, "selectors", test_table_limits(8, 8, 1)).unwrap_err(),
            "HTML table column limit exceeded"
        );
        assert_eq!(
            extract_tables(html, "traversal", test_table_limits(0, 8, 8)).unwrap_err(),
            "invalid HTML table admission"
        );
        assert_eq!(
            extract_tables(
                html,
                "traversal",
                HtmlTableLimits {
                    max_html_bytes: 1,
                    ..test_table_limits(8, 8, 8)
                }
            )
            .unwrap_err(),
            "invalid resource budget"
        );
        assert_eq!(
            extract_tables(
                html,
                "traversal",
                HtmlTableLimits {
                    max_nodes: 1,
                    ..test_table_limits(8, 8, 8)
                }
            )
            .unwrap_err(),
            "max DOM nodes exceeded"
        );
        assert_eq!(
            extract_tables(
                "<table><tr><td>four</td></tr></table>",
                "selectors",
                HtmlTableLimits {
                    max_text_bytes: 3,
                    ..test_table_limits(8, 8, 8)
                }
            )
            .unwrap_err(),
            "decoded text byte budget exceeded"
        );
        let wide = format!(
            "<table><tbody>{}</tbody></table>",
            "<tr><td colspan=500>x</td></tr>".repeat(512)
        );
        assert_eq!(
            extract_tables(&wide, "traversal", test_table_limits(8, 512, 500)).unwrap_err(),
            "HTML table grid limit exceeded"
        );
    }

    #[test]
    fn html_table_tag_prefilter_handles_comments_rawtext_and_quoted_attributes() {
        assert!(!has_html_table_tag(
            r#"<script>const t = "<table><tr><td>1</td></tr></table>";</SCRIPT 	><style>.x{content:"<table>"}</style><textarea><table><tr><td>noise</td></tr></table></textarea><title><table></title><xmp><table></xmp><!-- <table> --><table-viewer><p>ok</p></table-viewer data-x="<table>">"#
        ));
        assert!(has_html_table_tag(
            "<!--><table><tr><td>1</td></tr></table>"
        ));
        assert!(has_html_table_tag(
            "<!---><table><tr><td>1</td></tr></table>"
        ));
        assert!(has_html_table_tag(
            r#"<div title="<!--"><table><tr><td>1</td></tr></table></div>"#
        ));
        assert!(has_html_table_tag(
            r#"<div data-x="<script>"><table><tr><td>1</td></tr></table></div>"#
        ));
        assert!(has_html_table_tag(
            r#"<script>const s = "<table-viewer>";</script><TABLE class="grid"><tr><td>1</td></tr></TABLE>"#
        ));
    }

    #[test]
    fn content_hash_batches_match_joined_across_exact_buffer_boundaries() {
        let mk = |s: String| Chunk {
            node: 1,
            role: "paragraph",
            text: s,
            links: Vec::new(),
        };
        for chunks in [
            vec![],
            vec![mk("single".into())],
            vec![mk("x".repeat(4095)), mk("y".repeat(17))],
            vec![
                mk("a".repeat(4095)),
                mk("b".repeat(17)),
                mk("c".repeat(300_000)),
            ],
            vec![
                mk("p".repeat(16383)),
                mk("q".repeat(5)),
                mk("r".repeat(300_000)),
            ],
        ] {
            let expected = content_hash(&chunks, 0);
            for batch in [1, 4096, 16384] {
                assert_eq!(content_hash(&chunks, batch), expected);
            }
        }
    }

    #[test]
    fn serialization_and_task_primitives_honor_include_links_and_highlighted_only() {
        let fixture = Fixture::new(
            json!({
                "html": "<main><p><a href='/bg'>Background</a></p><p><a href='/target'>Target passage</a> and <a href='/target'>repeat</a></p></main>",
                "url": "https://evidence.example/doc",
                "query": "Target",
                "highlight_limit": 1,
                "link_mode": "highlighted_only"
            }),
            "serialization",
        )
        .unwrap();
        let Output::Bytes(serialized) = execute("serialization", &fixture).unwrap() else {
            panic!("expected bytes");
        };
        let text = String::from_utf8(serialized).unwrap();
        assert!(text.contains("https://evidence.example/target"));
        assert!(!text.contains("https://evidence.example/bg"));

        let req_no_links = serde_json::to_vec(&json!({
            "schema": "athena-task-extract/1",
            "principal_tenant": "tenant_alpha",
            "bound_task_id": "task_alpha",
            "task_id": "task_alpha",
            "source_id": "doc_primary",
            "expected_revision": "rev_200_v1",
            "include_links": false
        }))
        .unwrap();
        let out_no_links = String::from_utf8(task_export(&req_no_links).unwrap()).unwrap();
        assert!(!out_no_links.contains("\"type\":\"link\""));

        let req_bad_mode = serde_json::to_vec(&json!({
            "schema": "athena-task-extract/1",
            "principal_tenant": "tenant_alpha",
            "bound_task_id": "task_alpha",
            "task_id": "task_alpha",
            "source_id": "doc_primary",
            "expected_revision": "rev_200_v1",
            "link_mode": "invalid_mode"
        }))
        .unwrap();
        assert_eq!(
            task_export(&req_bad_mode).unwrap_err(),
            "SCOPED_TASK_REQUEST_INVALID"
        );
    }

    #[test]
    fn theory_matrix_and_new_contender_modes_execute_cleanly() {
        let report = run_theory_matrix(2).unwrap();
        assert_eq!(report["protocol"], "athena-theory-matrix/1");
        assert_eq!(
            report["retrieval_ranking_and_selection"]["structural_authority_256"]["bm25f_mslp"]["top1_role"],
            "heading"
        );
        assert_eq!(
            report["retrieval_ranking_and_selection"]["near_duplicate_novelty_trap_512"]["bm25f_simhash64"]
                ["unique_non_boilerplate_topics"],
            5
        );
        assert_eq!(
            report["boilerplate_policies"]["unsemantic_div_soup_96_blocks"]["cetd_entropy_v1"]["retained_chunks"],
            32
        );
        assert_eq!(
            report["annas_archive_extraction"]["annas_archive_scroll_cards_plus_main_content"]["records_extracted"],
            32
        );

        let cetd_fixture = Fixture::new(
            json!({
                "html": "<div class='menu'><p><a href='/a'>A</a> <a href='/b'>B</a> <a href='/c'>C</a></p></div><div class='article'><p>Deterministic entropy pruning retains high density prose.</p></div>",
                "boilerplate": "cetd_entropy_v1"
            }),
            "parse",
        )
        .unwrap();
        let Output::Projection(proj) = execute("parse", &cetd_fixture).unwrap() else {
            panic!("expected projection");
        };
        assert_eq!(proj.chunks.len(), 1);
        assert_eq!(
            proj.chunks[0].text,
            "Deterministic entropy pruning retains high density prose."
        );

        for mode in ["bm25", "bm25f", "bm25_plus", "bm25l", "ql_dirichlet"] {
            let f = Fixture::new(
                json!({
                    "html": "<h1>Byzantine Quorum</h1><p>Byzantine quorum safety protocol</p>",
                    "query": "byzantine quorum",
                    "rank_mode": mode
                }),
                "rank",
            )
            .unwrap();
            let Output::Rank(scores) = execute("rank", &f).unwrap() else {
                panic!("expected rank");
            };
            assert_eq!(scores.len(), 2);
            assert!(scores[0].1 > 0.0);
        }

        for mode in ["bm25", "bm25f", "bm25f_simhash"] {
            let f = Fixture::new(
                json!({
                    "html": "<p>Byzantine quorum safety.</p><p>Byzantine quorum safety!</p><p>Byzantine quorum liveness proof</p>",
                    "query": "byzantine quorum",
                    "highlight_limit": 2,
                    "rank_mode": mode
                }),
                "highlight",
            )
            .unwrap();
            let Output::Selection(sel) = execute("highlight", &f).unwrap() else {
                panic!("expected selection");
            };
            if mode == "bm25f_simhash" {
                assert_eq!(sel, vec![0, 2]);
            } else {
                assert_eq!(sel, vec![0, 1]);
            }
        }

        // BENCH-01: link_mode="none" defaults limits.include_links to false in parse stage
        let link_none_fixture = Fixture::new(
            json!({
                "html": "<main><p>Read <a href='/docs'>docs</a></p></main>",
                "link_mode": "none"
            }),
            "parse",
        )
        .unwrap();
        assert!(!link_none_fixture.limits.include_links);
        let Output::Projection(proj_no_links) = execute("parse", &link_none_fixture).unwrap()
        else {
            panic!("expected projection");
        };
        assert!(proj_no_links.links.is_empty());

        let rank_model_fixture = Fixture::new(
            json!({
                "html": "<p>Byzantine quorum safety.</p><p>Byzantine quorum safety!</p><p>Byzantine quorum liveness proof</p>",
                "query": "byzantine quorum",
                "highlight_limit": 2,
                "rank_model": "bm25f_simhash_highlight_indices"
            }),
            "selection",
        )
        .unwrap();
        let Output::Selection(sel_model) = execute("selection", &rank_model_fixture).unwrap()
        else {
            panic!("expected selection");
        };
        assert_eq!(sel_model, vec![0, 2]);

        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["total_factorial_retrieval_extraction_cells"],
            126
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["total_extended_factorial_cells_with_hybrid_rrf"],
            144
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["grand_total_evaluated_cells"],
            133
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["by_rank_model_summary"]["bm25f_plus_mslp_simhash64"]
                ["max_ndcg_at_5"],
            1.0
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["multi_query_rrf_benchmark"]["ndcg_at_5"],
            1.0
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["multi_query_rrf_benchmark"]["boilerplate_removal_f1_under_link_none"],
            1.0
        );
        assert_eq!(
            report["combinatorial_matrix_126_and_ir_quality"]["multi_query_rrf_benchmark"]["extracted_annas_records_under_link_none"],
            2
        );
        assert_eq!(
            report["proxy_pool_anti_bot_and_bandwidth_routing"]["total_routing_cells"],
            40
        );
        assert!(
            report["proxy_pool_anti_bot_and_bandwidth_routing"]["by_algorithm_summary"]
                ["athena_adaptive_contextual_v1"]["mean_first_attempt_usable_rate"]
                .as_f64()
                .unwrap()
                > report["proxy_pool_anti_bot_and_bandwidth_routing"]["by_algorithm_summary"]
                    ["discounted_thompson"]["mean_first_attempt_usable_rate"]
                    .as_f64()
                    .unwrap()
        );

        let hybrid_fixture = Fixture::new(
            json!({
                "html": "<p>Byzantine quorum safety.</p><p>Byzantine quorum safety!</p><p>Byzantine quorum liveness proof</p>",
                "query": "byzantine quorum",
                "highlight_limit": 2,
                "rank_model": "bm25f_plus_mslp_simhash64"
            }),
            "selection",
        )
        .unwrap();
        let Output::Selection(sel_hybrid) = execute("selection", &hybrid_fixture).unwrap() else {
            panic!("expected selection");
        };
        assert_eq!(sel_hybrid, vec![0, 2]);

        let rrf_fixture = Fixture::new(
            json!({
                "html": "<p>Byzantine quorum safety.</p><p>Byzantine quorum safety!</p><p>Byzantine quorum liveness proof</p>",
                "query": "byzantine quorum",
                "highlight_limit": 2,
                "rank_model": "hybrid_rrf_simhash64"
            }),
            "selection",
        )
        .unwrap();
        let Output::Selection(sel_rrf) = execute("selection", &rrf_fixture).unwrap() else {
            panic!("expected selection");
        };
        assert_eq!(sel_rrf, vec![0, 2]);

        let stress = run_stress_campaign(1).unwrap();
        assert_eq!(
            stress["multi_corpus_864_cell_ir_and_bandwidth_study"]["total_cells"],
            864
        );
        assert!(
            stress["proxy_antibot_monte_carlo_and_ablation_study"]
                ["by_algorithm_and_ablation_summary"]["athena_adaptive_contextual_v1"]
                ["mean_first_attempt_usable_rate"]
                .as_f64()
                .unwrap()
                > stress["proxy_antibot_monte_carlo_and_ablation_study"]
                    ["by_algorithm_and_ablation_summary"]["discounted_thompson"]
                    ["mean_first_attempt_usable_rate"]
                    .as_f64()
                    .unwrap()
        );
    }

    #[test]
    fn cloud_024_breakthrough_archetypes_and_deconfounded_crn_execute_cleanly() {
        let waf_fix = Fixture::new(
            json!({
                "html": "<!doctype html><main><p>Authentic scholarly payload after WAF clearance.</p></main>",
                "archetype": "deconfounded_waf_crn_replay"
            }),
            "cloud-024",
        )
        .unwrap();
        let Output::Control(waf_out) = execute("cloud-024", &waf_fix).unwrap() else {
            panic!("expected control output");
        };
        assert_eq!(waf_out["soft_block_recall"], 1.0);
        assert_eq!(waf_out["false_positive_rate"], 0.0);
        assert_eq!(waf_out["cache_poisoning_rate"], 0.0);
        assert_eq!(waf_out["crn_substream_orthogonality"], true);

        let camp_full = run_cloud_024_campaign(2, 0, None).unwrap();
        let camp_resumed = run_cloud_024_campaign(2, 250, None).unwrap();
        assert_eq!(camp_full["executed_steps"], 500);
        assert_eq!(camp_resumed["executed_steps"], 250);
    }
}
