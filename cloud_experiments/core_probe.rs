//! Native research boundaries; canonical outputs are produced outside timers.
#![forbid(unsafe_code)]
use athena_core_v2::{
    aae::{
        LinkMode, export_jsonl_with_limit, export_jsonl_with_link_mode_and_prepared,
        export_jsonl_with_options,
    },
    arena::Tag,
    network::{ContentEncodingMode, decode_content_encoding},
    projection::{
        BoilerplatePolicy, Chunk, CommentUnwrapPolicy, ParseLimits, Projection, ResearchNode,
        ResearchNodeKind, extract_annas_archive_records, parse_html,
        parse_html_document_with_node_limit, parse_html_with_limits, project_research_nodes,
    },
    rank::{
        PreparedRank as CorePreparedRank, bm25, bm25_plus, bm25f, bm25f_highlight_indices,
        bm25f_simhash_highlight_indices, bm25l, highlight_indices, ql_dirichlet,
    },
    table::{Cell, Grid, GridError, Row, expand, expand_with_slot_limit},
};
use scraper::{ElementRef, Html, Selector};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    env, fs,
    hint::black_box,
    io::{self, Read, Write},
    sync::Arc,
    time::Instant,
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
            "parser" | "parse" | "projection" | "pipeline" | "table" | "html-table" | "controls"
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
                Some("bm25_plus" | "bm25+" | "bm25-plus") => {
                    bm25_plus(chunks, query, 1.2, 0.75, 1.0)
                }
                Some("bm25l") => bm25l(chunks, query, 1.2, 0.75, 0.5),
                Some("ql_dirichlet" | "dirichlet" | "ql-dirichlet") => {
                    ql_dirichlet(chunks, query, 1000.0)
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
            "bm25l_long_to_short_ratio": score_of(&s_bm25l, 0) / score_of(&s_bm25l, 1),
            "ql_dirichlet_long_to_short_ratio": score_of(&s_ql, 0) / score_of(&s_ql, 1),
            "bm25_timing": time_samples(iterations, warmup, || {
                black_box(bm25(black_box(&skewed_chunks), q_skew, 1.2, 0.75));
            }),
            "bm25_plus_timing": time_samples(iterations, warmup, || {
                black_box(bm25_plus(black_box(&skewed_chunks), q_skew, 1.2, 0.75, 1.0));
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

fn run() -> Result<(), String> {
    let args = env::args().collect::<Vec<_>>();
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
    }
}
