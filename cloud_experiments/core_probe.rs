//! Native research boundaries; canonical outputs are produced outside timers.
#![forbid(unsafe_code)]
use athena_core_v2::{
    aae::export_jsonl_with_limit,
    arena::Tag,
    projection::{
        Chunk, ParseLimits, Projection, parse_html, parse_html_document_with_node_limit,
        parse_html_with_limits,
    },
    rank::{bm25, highlight_indices},
    table::{Cell, Grid, GridError, Row, expand, expand_with_slot_limit},
};
use scraper::{ElementRef, Html, Selector};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    env, fs,
    hint::black_box,
    io::{self, Write},
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
            let mut counts = BTreeMap::<String, usize>::new();
            let mut length = 0;
            for word in words(&chunk.text) {
                tokens += 1;
                if tokens > max_tokens {
                    return Err("prepared token budget exceeded".into());
                }
                let normalized = word.to_lowercase();
                normalized_bytes = normalized_bytes
                    .checked_add(normalized.len())
                    .ok_or("prepared byte budget exceeded")?;
                if normalized_bytes > 32 * 1024 * 1024 {
                    return Err("prepared byte budget exceeded".into());
                }
                *counts.entry(normalized).or_default() += 1;
                length += 1;
            }
            lengths.push(length);
            for (term, count) in counts {
                postings.entry(term).or_default().push((index, count));
            }
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
        let mut terms = words(query).map(str::to_lowercase).collect::<Vec<_>>();
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
        let norms = self
            .lengths
            .iter()
            .map(|&length| k1 * (1.0 - b + b * length as f64 / avg_len))
            .collect::<Vec<_>>();
        let mut scores = (0..n).map(|i| (i, 0.0)).collect::<Vec<_>>();
        for term in terms {
            if let Some(matches) = self.postings.get(&term) {
                let df = matches.len() as f64;
                let idf = (1.0 + (n as f64 - df + 0.5) / (df + 0.5)).ln();
                for &(i, frequency) in matches {
                    let freq = frequency as f64;
                    scores[i].1 += idf * freq * (k1 + 1.0) / (freq + norms[i]);
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
    for row in document.select(&selectors.row) {
        if let Some(index) = nearest_table_index(row, tables) {
            let (group, group_type) = row_group(row, tables[index]);
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

#[derive(Clone)]
struct HtmlCellMeta {
    tag: String,
    id: Option<String>,
    headers: Vec<String>,
    scope: Option<String>,
    locator: String,
}

fn derive_header_associations(
    grid: &Grid,
    cells: &[HtmlCellMeta],
) -> Result<Vec<Vec<String>>, String> {
    if grid.cells.len() != cells.len() {
        return Err("HTML table cell metadata does not match logical grid".into());
    }
    let mut ids = BTreeMap::new();
    for (index, cell) in cells.iter().enumerate() {
        if let Some(id) = &cell.id
            && ids.insert(id.clone(), index).is_some()
        {
            return Err("duplicate table header id".into());
        }
    }

    let mut coordinates = vec![Vec::new(); cells.len()];
    for (row, slots) in grid.slots.iter().enumerate() {
        for (column, index) in slots
            .iter()
            .enumerate()
            .filter_map(|(column, index)| index.map(|value| (column, value)))
        {
            coordinates[index].push((row, column));
        }
    }
    let mut bounds = Vec::with_capacity(cells.len());
    for points in &coordinates {
        let Some(&(first_row, first_column)) = points.first() else {
            return Err("table cell is absent from logical grid".into());
        };
        let (last_row, last_column) = points.iter().copied().fold(
            (first_row, first_column),
            |(last_row, last_column), (row, column)| (last_row.max(row), last_column.max(column)),
        );
        bounds.push((first_row, first_column, last_row, last_column));
    }

    let mut column_headers = vec![Vec::new(); grid.width];
    let mut row_headers = vec![Vec::new(); grid.slots.len()];
    for (index, cell) in cells.iter().enumerate() {
        if cell.tag != "th" {
            continue;
        }
        let scope = cell.scope.as_deref().unwrap_or("");
        match scope {
            "col" | "colgroup" => {
                for &(_, column) in &coordinates[index] {
                    column_headers[column].push(index);
                }
            }
            "row" | "rowgroup" => {
                for &(row, _) in &coordinates[index] {
                    row_headers[row].push(index);
                }
            }
            _ => continue,
        }
    }

    let mut result = vec![Vec::new(); cells.len()];
    for (index, cell) in cells.iter().enumerate() {
        if cell.tag != "td" {
            continue;
        }
        if !cell.headers.is_empty() {
            let mut associated = Vec::with_capacity(cell.headers.len());
            for id in &cell.headers {
                let Some(&header_index) = ids.get(id) else {
                    return Err("explicit table header reference is missing".into());
                };
                if cells[header_index].tag != "th" {
                    return Err("explicit table header reference is not a header".into());
                }
                associated.push(id.clone());
            }
            result[index] = associated;
            continue;
        }
        let (first_row, first_column, _, _) = bounds[index];
        let mut candidates = BTreeMap::<usize, (u8, usize, usize)>::new();
        for &header_index in column_headers
            .iter()
            .skip(first_column)
            .take(bounds[index].3 - first_column + 1)
            .flatten()
        {
            let (header_row, header_column, last_row, _) = bounds[header_index];
            if last_row < first_row {
                candidates
                    .entry(header_index)
                    .or_insert((0, header_row, header_column));
            }
        }
        for &header_index in row_headers
            .iter()
            .skip(first_row)
            .take(bounds[index].2 - first_row + 1)
            .flatten()
        {
            let (header_row, header_column, _, last_column) = bounds[header_index];
            if last_column < first_column {
                candidates
                    .entry(header_index)
                    .or_insert((1, header_row, header_column));
            }
        }
        let mut ordered = candidates.into_iter().collect::<Vec<_>>();
        ordered.sort_by_key(|(header_index, (kind, row, column))| {
            (*kind, *row, *column, *header_index)
        });
        result[index] = ordered
            .into_iter()
            .map(|(header_index, _)| {
                cells[header_index]
                    .id
                    .clone()
                    .unwrap_or_else(|| cells[header_index].locator.clone())
            })
            .collect();
    }
    Ok(result)
}

#[cfg(test)]
fn extract_html_tables(
    html: &str,
    algorithm: &str,
    selectors: &TableSelectors,
    limits: &HtmlTableLimits,
) -> Result<Value, String> {
    extract_html_tables_with_headers(html, algorithm, "explicit", selectors, limits)
}

fn extract_html_tables_with_headers(
    html: &str,
    algorithm: &str,
    header_mode: &str,
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
        || !matches!(header_mode, "explicit" | "scope")
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
    let selected_rows = if algorithm == "selectors" {
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
        let rows = selected_rows.as_ref().map_or_else(
            || rows_by_traversal(table, &tables, table_index),
            |all| all[table_index].clone(),
        );
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
        let mut cell_metadata = Vec::new();
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
                let text = normalized_cell_text(cell);
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
                cell_metadata.push(HtmlCellMeta {
                    tag: tag.to_owned(),
                    id: cell.attr("id").map(str::to_owned),
                    headers: header_ids.iter().map(|value| (*value).to_owned()).collect(),
                    scope: cell.attr("scope").map(str::to_owned),
                    locator: locator.clone(),
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
        if header_mode == "scope" {
            let associations = derive_header_associations(&grid, &cell_metadata)?;
            let mut cell_index = 0;
            for row in &mut output_rows {
                for cell in row["cells"]
                    .as_array_mut()
                    .ok_or("HTML table output cell list missing")?
                {
                    cell["associated_headers"] = json!(associations[cell_index]);
                    cell_index += 1;
                }
            }
        }
        output_tables.push(json!({
            "table_index":table_index, "id":table.attr("id"), "caption":caption,
            "rows":output_rows, "grid":{"width":grid.width,"slots":slots}
        }));
    }
    Ok(json!({
        "schema":"athena-html-table-candidate/1",
            "parse_warnings":document.errors.len(), "header_associations":if header_mode == "scope" { "scope-derived-v1" } else { "explicit-attributes-only" },
        "cell_text":"normalized DOM text, including nested-table descendant text", "tables":output_tables
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
        let mut page = if matches!(
            stage,
            "parser" | "parse" | "projection" | "pipeline" | "table" | "html-table" | "controls"
        ) {
            None
        } else {
            Some(parse_html_with_limits(&html, &url, limits).map_err(str::to_string)?)
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
    let export = |page: &Projection| {
        export_jsonl_with_limit(
            page,
            &input.url,
            input.observed,
            input.input["query"].as_str(),
            input.highlight_limit,
            input.limit,
        )
        .map(String::into_bytes)
        .map_err(|e| e.to_string())
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
        "export" => export(page()?).map(Output::Bytes),
        "pipeline" => export(&parse()?).map(Output::Bytes),
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
        "html-table" => extract_html_tables_with_headers(
            &input.html,
            option_env!("ATHENA_RESEARCH_TABLE_EXTRACTOR").unwrap_or("traversal"),
            option_env!("ATHENA_RESEARCH_TABLE_HEADERS").unwrap_or("explicit"),
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

fn run() -> Result<(), String> {
    let args = env::args().collect::<Vec<_>>();
    require(
        args.len() == 4,
        "usage: core_probe <stage> <fixture.json> <iterations>",
    )?;
    let iterations = args[3].parse::<usize>().map_err(|e| e.to_string())?;
    require(iterations <= 250, "iterations exceed hard probe admission")?;
    let metadata = fs::metadata(&args[2]).map_err(|e| e.to_string())?;
    require(
        metadata.len() <= 8 * 1024 * 1024,
        "fixture JSON exceeds hard probe admission",
    )?;
    let input = serde_json::from_slice(&fs::read(&args[2]).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
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

fn main() {
    if let Err(error) = run() {
        eprintln!("{}", json!({"status":"ERROR","error":error}));
        std::process::exit(2);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn extract_tables(
        html: &str,
        algorithm: &str,
        limits: HtmlTableLimits,
    ) -> Result<Value, String> {
        let selectors = TableSelectors::new().unwrap();
        extract_html_tables(html, algorithm, &selectors, &limits)
    }

    fn extract_tables_with_headers(
        html: &str,
        algorithm: &str,
        header_mode: &str,
        limits: HtmlTableLimits,
    ) -> Result<Value, String> {
        let selectors = TableSelectors::new().unwrap();
        extract_html_tables_with_headers(html, algorithm, header_mode, &selectors, &limits)
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

        let nested = "<table id=outer><tbody><tr><td>outer <table id=inner><tbody><tr><th>inner</th></tr></tbody></table></td></tr></tbody></table>";
        let nested = extract_tables(nested, "traversal", test_table_limits(8, 32, 16)).unwrap();
        let nested = nested["tables"].as_array().unwrap();
        assert_eq!(nested.len(), 2, "nested tables are emitted once each");
        assert_eq!(nested[0]["rows"].as_array().unwrap().len(), 1);
        assert_eq!(nested[0]["rows"][0]["cells"][0]["text"], "outer inner");
        assert_eq!(nested[1]["rows"][0]["cells"][0]["text"], "inner");
    }

    #[test]
    fn scope_header_mode_derives_column_and_row_headers_from_the_logical_grid() {
        let html = "<table id=metrics><thead><tr><th id=region scope=col>Region</th><th id=amount scope=col>Amount</th></tr></thead><tbody><tr><th id=east scope=row>East</th><td>1200</td></tr><tr><th id=west scope=row>West</th><td>900</td></tr></tbody></table>";
        let output =
            extract_tables_with_headers(html, "traversal", "scope", test_table_limits(8, 32, 16))
                .unwrap();
        assert_eq!(output["header_associations"], "scope-derived-v1");
        let rows = output["tables"][0]["rows"].as_array().unwrap();
        assert_eq!(
            rows[1]["cells"][1]["associated_headers"],
            json!(["amount", "east"])
        );
        assert_eq!(
            rows[2]["cells"][1]["associated_headers"],
            json!(["amount", "west"])
        );
    }

    #[test]
    fn scope_header_mode_rejects_duplicate_header_ids() {
        let html = "<table><tr><th id=h scope=col>One</th><th id=h scope=col>Two</th></tr><tr><td>1</td></tr></table>";
        let error =
            extract_tables_with_headers(html, "traversal", "scope", test_table_limits(8, 32, 16))
                .unwrap_err();
        assert_eq!(error, "duplicate table header id");
    }

    #[test]
    fn scope_header_mode_rejects_missing_explicit_header_reference() {
        let html = "<table><tr><th id=known scope=col>Known</th></tr><tr><td headers=missing>1</td></tr></table>";
        let error =
            extract_tables_with_headers(html, "traversal", "scope", test_table_limits(8, 32, 16))
                .unwrap_err();
        assert_eq!(error, "explicit table header reference is missing");
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
