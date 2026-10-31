//! Native research boundaries; canonical outputs are produced outside timers.
#![forbid(unsafe_code)]
use athena_core_v2::{
    aae::export_jsonl_with_limit,
    arena::Tag,
    projection::{
        Chunk, ParseLimits, Projection, ResearchNode, ResearchNodeKind, parse_html,
        parse_html_document_with_node_limit, parse_html_with_limits, project_research_nodes,
    },
    rank::{bm25, highlight_indices},
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
    lengths: Vec<usize>,
    postings: BTreeMap<String, Vec<(usize, usize)>>,
    retained_payload_bytes: usize,
}
impl PreparedRank {
    fn new(chunks: &[Chunk], revision: &str, max_tokens: usize) -> Result<Self, String> {
        let mut lengths = Vec::with_capacity(chunks.len());
        let mut postings = BTreeMap::<String, Vec<(usize, usize)>>::new();
        let mut tokens = 0usize;
        let mut normalized_bytes = 0usize;
        for (index, chunk) in chunks.iter().enumerate() {
            let mut length = 0;
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
            lengths.push(length);
        }
        let retained_payload_bytes = lengths.capacity() * std::mem::size_of::<usize>()
            + postings
                .iter()
                .map(|(term, hits)| {
                    term.capacity() + hits.capacity() * std::mem::size_of::<(usize, usize)>()
                })
                .sum::<usize>();
        Ok(Self {
            key: source_key(chunks, revision),
            lengths,
            postings,
            retained_payload_bytes,
        })
    }
    fn scores(&self, query: &str) -> Vec<(usize, f64)> {
        let mut terms = words(query).map(lowercase).collect::<Vec<_>>();
        terms.sort_unstable();
        terms.dedup();
        let n = self.lengths.len();
        if n == 0 {
            return vec![];
        }
        let total_len: usize = self.lengths.iter().sum();
        let avg_len = (total_len as f64 / n as f64).max(1.0);
        let k1 = 1.2;
        let b = 0.75;
        let mut scores = (0..n).map(|i| (i, 0.0)).collect::<Vec<_>>();
        for term in terms {
            if let Some(matches) = self.postings.get(term.as_ref()) {
                let df = matches.len() as f64;
                let idf = (1.0 + (n as f64 - df + 0.5) / (df + 0.5)).ln();
                for &(i, frequency) in matches {
                    let freq = frequency as f64;
                    let norm = k1 * (1.0 - b + b * self.lengths[i] as f64 / avg_len);
                    scores[i].1 += idf * freq * (k1 + 1.0) / (freq + norm);
                }
            }
        }
        scores.sort_by(|(a, sa), (b, sb)| sb.total_cmp(sa).then(a.cmp(b)));
        scores
    }
    fn matches(&self, chunks: &[Chunk], revision: &str) -> bool {
        self.key == source_key(chunks, revision)
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
    let mut hash = blake3::Hasher::new();
    if batch == 1 {
        for (i, chunk) in chunks.iter().enumerate() {
            if i != 0 {
                hash.update(b"\n");
            }
            hash.update(chunk.text.as_bytes());
        }
    } else {
        let mut scratch = Vec::with_capacity(batch);
        for (i, chunk) in chunks.iter().enumerate() {
            let separator = if i == 0 {
                b"".as_slice()
            } else {
                b"\n".as_slice()
            };
            for mut bytes in [separator, chunk.text.as_bytes()] {
                if scratch.is_empty() && bytes.len() >= batch {
                    hash.update(bytes);
                    continue;
                }
                while !bytes.is_empty() {
                    let take = bytes.len().min(batch - scratch.len());
                    scratch.extend_from_slice(&bytes[..take]);
                    bytes = &bytes[take..];
                    if scratch.len() == batch {
                        hash.update(&scratch);
                        scratch.clear();
                    }
                }
            }
        }
        hash.update(&scratch);
    }
    *hash.finalize().as_bytes()
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
) -> Vec<Frame> {
    let highlights = query
        .map(|q| highlight_indices(&page.chunks, q, limit))
        .unwrap_or_default();
    let mut selected = vec![false; page.chunks.len()];
    for &index in &highlights {
        selected[index] = true;
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
    for index in highlights
        .into_iter()
        .chain((0..page.chunks.len()).filter(|&i| !selected[i]))
    {
        let chunk = &page.chunks[index];
        frames.push(Frame::Block {
            id: blake3::Hash::from(ids[index]).to_hex().to_string(),
            role: chunk.role,
            text: chunk.text.clone(),
            links: chunk.links.clone(),
            highlight: selected[index],
        });
    }
    for (index, url) in page.links.iter().enumerate() {
        frames.push(Frame::Link {
            id: index as u32,
            url: url.clone(),
        });
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

fn row_group<'a>(row: ElementRef<'a>, table: ElementRef<'a>) -> (usize, String) {
    let groups = table
        .child_elements()
        .filter(|child| matches!(child.value().name(), "thead" | "tbody" | "tfoot"))
        .collect::<Vec<_>>();
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
    tables: &[ElementRef<'a>],
    table_index: usize,
) -> Vec<(ElementRef<'a>, usize, String)> {
    let mut stack = table.child_elements().collect::<Vec<_>>();
    stack.reverse();
    let mut rows = Vec::new();
    while let Some(element) = stack.pop() {
        match element.value().name() {
            "table" => continue,
            "tr" => {
                if nearest_table_index(element, tables) == Some(table_index) {
                    let (group, group_type) = row_group(element, table);
                    rows.push((element, group, group_type));
                }
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
    document: &'a Html,
    tables: &[ElementRef<'a>],
    selectors: &TableSelectors,
) -> Vec<Vec<(ElementRef<'a>, usize, String)>> {
    let mut result = vec![Vec::new(); tables.len()];
    if tables.is_empty() {
        return result;
    }
    let mut table_groups: Vec<Option<Vec<ElementRef<'a>>>> = vec![None; tables.len()];
    for row in document.select(&selectors.row) {
        let mut group_candidate = None;
        let mut table_index = None;
        let mut parent = row.parent().and_then(ElementRef::wrap);
        while let Some(candidate) = parent {
            let name = candidate.value().name();
            if name == "table" {
                table_index = tables.iter().position(|table| *table == candidate);
                break;
            }
            if group_candidate.is_none() && matches!(name, "thead" | "tbody" | "tfoot") {
                group_candidate = Some((candidate, name));
            }
            parent = candidate.parent().and_then(ElementRef::wrap);
        }
        if let Some(index) = table_index {
            let groups = table_groups[index].get_or_insert_with(|| {
                tables[index]
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
        require(
            prepared.matches(&initial.chunks, &input.revision),
            "prepared source identity mismatch",
        )?;
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
        if let Some(prepared) = &state
            && source_key(&initial.chunks, &input.revision)
                != source_key(&replacement.chunks, &revision)
        {
            require(
                !prepared.matches(&replacement.chunks, &revision),
                "stale prepared source was accepted",
            )?;
        }
        drop(state.take());
        state = if lexical {
            Some(PreparedRank::new(
                &replacement.chunks,
                &revision,
                input.max_tokens,
            )?)
        } else {
            None
        };
        if let Some(prepared) = &state {
            require(
                prepared.matches(&replacement.chunks, &revision),
                "replacement prepared identity mismatch",
            )?;
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
            make_frames(
                page.as_ref().ok_or("serialization page missing")?,
                &url,
                observed,
                input["query"].as_str(),
                highlight_limit,
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
        let mut bytes = export_jsonl_with_limit(
            page,
            &input.url,
            input.observed,
            input.input["query"].as_str(),
            input.highlight_limit,
            input.limit,
        )
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
        "rank" => Ok(Output::Rank(bm25(
            &page()?.chunks,
            input.input["query"].as_str().unwrap_or(""),
            1.2,
            0.75,
        ))),
        "selection" | "highlight" => Ok(Output::Selection(highlight_indices(
            &page()?.chunks,
            input.input["query"].as_str().unwrap_or(""),
            input.highlight_limit,
        ))),
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

fn has_html_table_tag(html: &str) -> bool {
    html.as_bytes()
        .windows(6)
        .any(|window| window.eq_ignore_ascii_case(b"<table"))
}

fn is_skipped_projection_subtree(name: &str) -> bool {
    matches!(
        name,
        "script" | "style" | "template" | "noscript" | "svg" | "canvas" | "iframe"
    )
}

fn scraper_projection_nodes(document: &Html) -> Vec<ResearchNode> {
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
                    let attributes = if el.name() == "a" {
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
    let nodes = scraper_projection_nodes(&document);
    let projection = project_research_nodes(html.len(), &nodes, url, limits, parse_warnings)
        .map_err(str::to_string)?;
    let cells = extract_structured_table_cells_from_document(&document, table_limits)?;
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
            "include_table_cells":true
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
            .all(|value| !value.is_empty() && value.len() <= 64),
        "SCOPED_TASK_REQUEST_INVALID",
    )?;
    task_authorize_and_export(
        &request.principal_tenant,
        &request.bound_task_id,
        &request.task_id,
        &request.source_id,
        &request.expected_revision,
        None,
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
            && request.handle.len() <= 128,
        "SCOPED_TASK_REQUEST_INVALID",
    )?;
    task_authorize_and_export(
        &request.principal_tenant,
        &request.bound_task_id,
        &request.task_id,
        &request.source_id,
        &request.expected_revision,
        Some(&request.handle),
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

fn run() -> Result<(), String> {
    let args = env::args().collect::<Vec<_>>();
    require(
        args.len() == 4,
        "usage: core_probe <stage> <fixture.json|-> <iterations>",
    )?;
    let iterations = args[3].parse::<usize>().map_err(|e| e.to_string())?;
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
}
