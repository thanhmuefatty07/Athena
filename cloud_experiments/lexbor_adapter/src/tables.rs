use athena_core_v2::{
    projection::{ResearchNode, ResearchNodeKind},
    table::{Cell, Grid, GridError, Row, expand_with_slot_limit},
};

const MAX_HTML_BYTES: usize = 4 * 1024 * 1024;
const MAX_PARSER_NODES: usize = 250_000;
const MAX_TEXT_BYTES: usize = 4 * 1024 * 1024;
const MAX_TABLES: usize = 64;
const MAX_ROWS: usize = 16_384;
const MAX_CELLS: usize = 50_000;
const MAX_GRID_SLOTS: usize = 250_000;

#[derive(Debug, Clone, Copy)]
pub struct HtmlTableLimits {
    pub max_tables: usize,
    pub max_rows: usize,
    pub max_cols: usize,
    pub max_html_bytes: usize,
    pub max_nodes: usize,
    pub max_text_bytes: usize,
}

impl Default for HtmlTableLimits {
    fn default() -> Self {
        Self {
            max_tables: MAX_TABLES,
            max_rows: 1000,
            max_cols: 1000,
            max_html_bytes: MAX_HTML_BYTES,
            max_nodes: MAX_PARSER_NODES,
            max_text_bytes: MAX_TEXT_BYTES,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HtmlTable {
    pub id: Option<String>,
    pub caption: Option<String>,
    pub rows: Vec<HtmlTableRow>,
    pub grid: Grid,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HtmlTableRow {
    pub row_index: usize,
    pub group: usize,
    pub group_type: String,
    pub cells: Vec<HtmlTableCell>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HtmlTableCell {
    pub cell_index: usize,
    pub tag: String,
    pub text: String,
    pub rowspan: usize,
    pub colspan: usize,
    pub scope: Option<String>,
    pub headers: Vec<String>,
    pub unit: Option<String>,
    pub abbr: Option<String>,
    pub id: Option<String>,
    pub locator: String,
}

#[derive(Debug, Clone, Copy)]
struct Group {
    table: usize,
    index: usize,
    name: &'static str,
}

struct RowInput {
    row_index: usize,
    group: usize,
    group_type: &'static str,
    cells: Vec<usize>,
}

fn element_named(node: &ResearchNode, name: &str) -> bool {
    node.kind == ResearchNodeKind::Element && node.name == name
}

fn group_name(node: &ResearchNode) -> Option<&'static str> {
    match node.name.as_str() {
        "thead" => Some("thead"),
        "tbody" => Some("tbody"),
        "tfoot" => Some("tfoot"),
        _ => None,
    }
}

fn span_attribute(node: &ResearchNode, name: &str) -> usize {
    let parsed = node
        .attr(name)
        .and_then(|value| value.trim().parse::<usize>().ok());
    match (name, parsed) {
        ("rowspan", Some(value)) => value.min(65_534),
        ("colspan", Some(value)) if value > 0 => value.min(1_000),
        _ => 1,
    }
}

fn normalized_descendant_text(nodes: &[ResearchNode], depths: &[usize], root: usize) -> String {
    let mut normalized = String::new();
    for index in root + 1..nodes.len() {
        if depths[index] <= depths[root] {
            break;
        }
        if nodes[index].kind == ResearchNodeKind::Text {
            for token in nodes[index].text.split_whitespace() {
                if !normalized.is_empty() {
                    normalized.push(' ');
                }
                normalized.push_str(token);
            }
        }
    }
    normalized
}

fn grid_error(error: GridError) -> String {
    match error {
        GridError::TooLarge => "HTML table column limit exceeded".into(),
        GridError::TooManySlots => "HTML table grid limit exceeded".into(),
        GridError::InvalidGroupOrder => "HTML table row group order invalid".into(),
        GridError::Overlap => "HTML table cell spans overlap".into(),
    }
}

/// Extract the same explicit HTML table fields and logical grid used by the
/// core research probe. Text includes nested-table descendants, matching the
/// existing scraper consumer's declared behavior.
pub fn extract_tables(
    html_len: usize,
    nodes: &[ResearchNode],
    limits: HtmlTableLimits,
) -> Result<Vec<HtmlTable>, String> {
    if limits.max_html_bytes == 0
        || limits.max_html_bytes > MAX_HTML_BYTES
        || html_len > limits.max_html_bytes
        || limits.max_nodes == 0
        || limits.max_nodes > MAX_PARSER_NODES
        || nodes
            .len()
            .checked_add(1)
            .is_none_or(|count| count > limits.max_nodes)
        || limits.max_text_bytes == 0
        || limits.max_text_bytes > MAX_TEXT_BYTES
    {
        return Err("invalid resource budget".into());
    }
    if limits.max_tables == 0
        || limits.max_tables > MAX_TABLES
        || limits.max_rows == 0
        || limits.max_rows > 2048
        || limits.max_cols == 0
        || limits.max_cols > 4096
        || limits
            .max_rows
            .checked_mul(limits.max_cols)
            .is_none_or(|slots| slots > 1_000_000)
    {
        return Err("invalid HTML table admission".into());
    }

    let node_count = nodes.len();
    let mut depths = vec![0usize; node_count];
    let mut table_by_node = vec![None; node_count];
    let mut table_nodes = Vec::new();
    for (index, node) in nodes.iter().enumerate() {
        let parent = match node.parent {
            Some(parent) if parent < index => Some(parent),
            Some(_) => return Err("invalid HTML table parser tree".into()),
            None => None,
        };
        depths[index] = parent.map_or(0, |parent| depths[parent].saturating_add(1));
        if element_named(node, "table") {
            if table_nodes.len() >= limits.max_tables {
                return Err("HTML table count limit exceeded".into());
            }
            table_by_node[index] = Some(table_nodes.len());
            table_nodes.push(index);
        }
    }

    let table_count = table_nodes.len();
    let mut nearest_table = vec![None; node_count];
    for (index, node) in nodes.iter().enumerate() {
        nearest_table[index] =
            table_by_node[index].or_else(|| node.parent.and_then(|parent| nearest_table[parent]));
    }

    let mut group_counts = vec![0usize; table_count];
    let mut direct_groups = vec![None; node_count];
    for (index, node) in nodes.iter().enumerate() {
        let Some(name) = group_name(node) else {
            continue;
        };
        let Some(table) = node.parent.and_then(|parent| table_by_node[parent]) else {
            continue;
        };
        direct_groups[index] = Some(Group {
            table,
            index: group_counts[table],
            name,
        });
        group_counts[table] += 1;
    }

    let mut nearest_group = vec![None; node_count];
    for (index, node) in nodes.iter().enumerate() {
        if table_by_node[index].is_some() {
            continue;
        }
        if let Some(group) = direct_groups[index] {
            nearest_group[index] = Some(group);
            continue;
        }
        nearest_group[index] = node
            .parent
            .and_then(|parent| nearest_group[parent])
            .filter(|group| Some(group.table) == nearest_table[index]);
    }

    let mut rows = Vec::<RowInput>::new();
    let mut rows_by_table = vec![Vec::<usize>::new(); table_count];
    let mut row_by_node = vec![None; node_count];
    let mut total_rows = 0usize;
    for (index, node) in nodes.iter().enumerate() {
        if !element_named(node, "tr") {
            continue;
        }
        let Some(table) = nearest_table[index] else {
            continue;
        };
        let table_rows = &mut rows_by_table[table];
        if table_rows.len() >= limits.max_rows {
            return Err("HTML table row limit exceeded".into());
        }
        total_rows = total_rows
            .checked_add(1)
            .ok_or("HTML table row limit exceeded")?;
        if total_rows > MAX_ROWS {
            return Err("HTML table row limit exceeded".into());
        }
        let group = nearest_group[index].filter(|group| group.table == table);
        let row_index = table_rows.len();
        row_by_node[index] = Some(rows.len());
        table_rows.push(rows.len());
        rows.push(RowInput {
            row_index,
            group: group.map_or(group_counts[table], |group| group.index),
            group_type: group.map_or("table", |group| group.name),
            cells: Vec::new(),
        });
    }

    let mut total_cells = 0usize;
    for (index, node) in nodes.iter().enumerate() {
        if !(element_named(node, "th") || element_named(node, "td")) {
            continue;
        }
        let Some(row) = node.parent.and_then(|parent| row_by_node[parent]) else {
            continue;
        };
        total_cells = total_cells
            .checked_add(1)
            .ok_or("HTML table cell limit exceeded")?;
        if rows[row].cells.len() >= limits.max_cols || total_cells > MAX_CELLS {
            return Err("HTML table column limit exceeded".into());
        }
        rows[row].cells.push(index);
    }

    let mut captions = vec![None; table_count];
    for (index, node) in nodes.iter().enumerate() {
        if !element_named(node, "caption") {
            continue;
        }
        if let Some(table) = node.parent.and_then(|parent| table_by_node[parent])
            && captions[table].is_none()
        {
            captions[table] = Some(index);
        }
    }

    let mut total_grid_slots = 0usize;
    let mut total_text_bytes = 0usize;
    let mut output = Vec::with_capacity(table_count);
    for table in 0..table_count {
        let mut grid_rows = Vec::with_capacity(rows_by_table[table].len());
        let mut output_rows = Vec::with_capacity(rows_by_table[table].len());
        for &row_id in &rows_by_table[table] {
            let row = &rows[row_id];
            let mut grid_cells = Vec::with_capacity(row.cells.len());
            let mut output_cells = Vec::with_capacity(row.cells.len());
            for &cell_node in &row.cells {
                let cell = &nodes[cell_node];
                let tag = cell.name.as_str();
                let text = normalized_descendant_text(nodes, &depths, cell_node);
                total_text_bytes = total_text_bytes
                    .checked_add(text.len())
                    .ok_or("decoded text byte budget exceeded")?;
                if total_text_bytes > limits.max_text_bytes {
                    return Err("decoded text byte budget exceeded".into());
                }
                let headers = cell
                    .attr("headers")
                    .unwrap_or("")
                    .split_ascii_whitespace()
                    .map(str::to_owned)
                    .collect::<Vec<_>>();
                output_cells.push(HtmlTableCell {
                    cell_index: output_cells.len(),
                    tag: tag.to_owned(),
                    text: text.clone(),
                    rowspan: span_attribute(cell, "rowspan"),
                    colspan: span_attribute(cell, "colspan"),
                    scope: cell.attr("scope").map(str::to_owned),
                    headers,
                    unit: cell.attr("data-unit").map(str::to_owned),
                    abbr: cell.attr("abbr").map(str::to_owned),
                    id: cell.attr("id").map(str::to_owned),
                    locator: format!(
                        "table[{table}]/row[{}]/cell[{}]",
                        row.row_index,
                        output_cells.len()
                    ),
                });
                grid_cells.push(Cell {
                    text,
                    rowspan: span_attribute(cell, "rowspan"),
                    colspan: span_attribute(cell, "colspan"),
                    is_header: tag == "th",
                });
            }
            grid_rows.push(Row {
                group: row.group,
                cells: grid_cells,
            });
            output_rows.push(HtmlTableRow {
                row_index: row.row_index,
                group: row.group,
                group_type: row.group_type.to_owned(),
                cells: output_cells,
            });
        }
        let remaining_grid_slots = MAX_GRID_SLOTS
            .checked_sub(total_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        let grid = expand_with_slot_limit(
            &grid_rows,
            limits.max_rows,
            limits.max_cols,
            remaining_grid_slots,
        )
        .map_err(grid_error)?;
        let table_grid_slots = grid
            .slots
            .len()
            .checked_mul(grid.width)
            .ok_or("HTML table grid limit exceeded")?;
        total_grid_slots = total_grid_slots
            .checked_add(table_grid_slots)
            .ok_or("HTML table grid limit exceeded")?;
        if total_grid_slots > MAX_GRID_SLOTS {
            return Err("HTML table grid limit exceeded".into());
        }

        let table_node = &nodes[table_nodes[table]];
        let caption = captions[table]
            .map(|caption_node| normalized_descendant_text(nodes, &depths, caption_node));
        if let Some(value) = &caption {
            total_text_bytes = total_text_bytes
                .checked_add(value.len())
                .ok_or("decoded text byte budget exceeded")?;
            if total_text_bytes > limits.max_text_bytes {
                return Err("decoded text byte budget exceeded".into());
            }
        }
        output.push(HtmlTable {
            id: table_node.attr("id").map(str::to_owned),
            caption,
            rows: output_rows,
            grid,
        });
    }
    Ok(output)
}
