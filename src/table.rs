//! Bounded logical grid builder. Preserves overlaps as errors and handles
//! rowspan=0 by extending to the end of its row group. Unlike flat Markdown,
//! the value at each logical slot references a single source cell.
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cell {
    pub text: String,
    pub rowspan: usize, // zero means remaining rows in the group
    pub colspan: usize,
    pub is_header: bool,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    pub group: usize,
    pub cells: Vec<Cell>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Grid {
    pub cells: Vec<Cell>,
    pub slots: Vec<Vec<Option<usize>>>,
    pub width: usize,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GridError {
    TooLarge,
    TooManySlots,
    InvalidGroupOrder,
    Overlap,
}
impl fmt::Display for GridError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for GridError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slot {
    pub cell_id: u32,
    pub origin_row: u16,
    pub origin_col: u16,
    pub is_header: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GroundedTableCell {
    pub cell_id: u32,
    pub row_idx: usize,
    pub col_idx: usize,
    pub rowspan: usize,
    pub colspan: usize,
    pub is_header: bool,
    pub text: String,
    pub col_headers: Vec<String>,
    pub row_headers: Vec<String>,
    pub grounded_context: String,
}

/// WHATWG §4.9.12 tolerant 2D table grid builder (`M2`, `D11`).
/// Normalizes out-of-order `(thead, tfoot, tbody)` row groups stably, clamps hanging
/// `rowspan`/`colspan` to group and table bounds, and resolves slot collisions deterministically.
pub fn expand_tolerant(rows: &[Row], max_rows: usize, max_cols: usize) -> Result<Grid, GridError> {
    let max_slots = max_rows.saturating_mul(max_cols);
    expand_tolerant_with_slot_limit(rows, max_rows, max_cols, max_slots)
}

pub fn expand_tolerant_with_slot_limit(
    rows: &[Row],
    max_rows: usize,
    max_cols: usize,
    max_slots: usize,
) -> Result<Grid, GridError> {
    if rows.len() > max_rows || max_cols == 0 {
        return Err(GridError::TooLarge);
    }
    let mut normalized_rows: Vec<Row> = rows.to_vec();
    if normalized_rows
        .windows(2)
        .any(|pair| pair[0].group > pair[1].group)
    {
        normalized_rows.sort_by_key(|row| row.group);
    }

    let mut slots: Vec<Vec<Option<usize>>> = vec![Vec::new(); normalized_rows.len()];
    let mut cells = Vec::new();
    let mut width = 0usize;
    let mut group_end = 0usize;
    let mut initialized_slots = 0usize;

    for (r, row) in normalized_rows.iter().enumerate() {
        if r == group_end {
            group_end = (r + 1..normalized_rows.len())
                .find(|&i| normalized_rows[i].group != row.group)
                .unwrap_or(normalized_rows.len());
        }
        let mut c = 0usize;
        for cell in &row.cells {
            while c < slots[r].len() && slots[r][c].is_some() {
                c += 1;
            }
            if c >= max_cols {
                break;
            }
            let max_rem_cols = max_cols - c;
            let dc = cell.colspan.clamp(1, 1000).min(max_rem_cols);
            let group_rem_rows = (group_end - r).max(1);
            let dr = if cell.rowspan == 0 {
                group_rem_rows
            } else {
                cell.rowspan.clamp(1, 65_534).min(group_rem_rows)
            };
            let end_col = c + dc;
            let index = cells.len();
            for line in slots.iter_mut().take(r + dr).skip(r) {
                if line.len() < end_col {
                    initialized_slots = initialized_slots
                        .checked_add(end_col - line.len())
                        .ok_or(GridError::TooManySlots)?;
                    if initialized_slots > max_slots {
                        return Err(GridError::TooManySlots);
                    }
                    let target_cap = end_col
                        .checked_next_power_of_two()
                        .unwrap_or(max_cols)
                        .min(max_cols);
                    if line.capacity() < end_col {
                        line.reserve_exact(target_cap.saturating_sub(line.len()));
                    }
                    line.resize(end_col, None);
                }
                for spot in &mut line[c..end_col] {
                    if spot.is_none() {
                        *spot = Some(index);
                    }
                }
            }
            cells.push(Cell {
                text: cell.text.clone(),
                rowspan: dr,
                colspan: dc,
                is_header: cell.is_header,
            });
            width = width.max(end_col);
            c = end_col;
        }
    }

    for row in &mut slots {
        if row.len() < width {
            initialized_slots = initialized_slots
                .checked_add(width - row.len())
                .ok_or(GridError::TooManySlots)?;
            if initialized_slots > max_slots {
                return Err(GridError::TooManySlots);
            }
            let target_cap = width
                .checked_next_power_of_two()
                .unwrap_or(max_cols)
                .min(max_cols);
            if row.capacity() < width {
                row.reserve_exact(target_cap.saturating_sub(row.len()));
            }
            row.resize(width, None);
        }
    }

    Ok(Grid {
        cells,
        slots,
        width,
    })
}

/// Resolves 2D column/row header lineage for every cell in `Grid` (`D11`).
pub fn ground_grid_cells(grid: &Grid) -> Vec<GroundedTableCell> {
    let n_cells = grid.cells.len();
    if n_cells == 0 {
        return Vec::new();
    }
    let mut origins: Vec<Option<(usize, usize, usize, usize)>> = vec![None; n_cells];
    for (r, row) in grid.slots.iter().enumerate() {
        for (c, slot) in row.iter().enumerate() {
            if let Some(id) = *slot
                && id < n_cells
            {
                match &mut origins[id] {
                    None => origins[id] = Some((r, c, r + 1, c + 1)),
                    Some((r0, c0, r1, c1)) => {
                        *r0 = (*r0).min(r);
                        *c0 = (*c0).min(c);
                        *r1 = (*r1).max(r + 1);
                        *c1 = (*c1).max(c + 1);
                    }
                }
            }
        }
    }

    let mut out = Vec::with_capacity(n_cells);
    for (id, cell) in grid.cells.iter().enumerate() {
        let (r0, c0, r1, c1) = origins[id].unwrap_or((0, 0, 1, 1));
        let rowspan = r1.saturating_sub(r0).max(1);
        let colspan = c1.saturating_sub(c0).max(1);

        let mut col_headers = Vec::new();
        for hr in 0..r0 {
            if let Some(hrow) = grid.slots.get(hr) {
                for slot in hrow.iter().take(c1).skip(c0) {
                    if let Some(hid) = *slot
                        && hid != id
                        && hid < n_cells
                        && grid.cells[hid].is_header
                    {
                        let htxt = grid.cells[hid].text.trim();
                        if !htxt.is_empty() && !col_headers.iter().any(|s: &String| s == htxt) {
                            col_headers.push(htxt.to_owned());
                        }
                    }
                }
            }
        }

        let mut row_headers = Vec::new();
        let mut row_bindings = Vec::new();
        for rr in r0..r1.min(grid.slots.len()) {
            let hrow = &grid.slots[rr];
            for (hc, slot) in hrow.iter().enumerate().take(c0) {
                if let Some(hid) = *slot
                    && hid != id
                    && hid < n_cells
                {
                    let hcell = &grid.cells[hid];
                    let htxt = hcell.text.trim();
                    if !htxt.is_empty()
                        && (hcell.is_header || hc == 0)
                        && !row_headers.iter().any(|s: &String| s == htxt)
                    {
                        row_headers.push(htxt.to_owned());
                        let col_axis_hdr = grid
                            .slots
                            .first()
                            .and_then(|top_row| top_row.get(hc))
                            .and_then(|&s| s)
                            .filter(|&top_id| {
                                top_id != hid && top_id < n_cells && grid.cells[top_id].is_header
                            })
                            .map(|top_id| grid.cells[top_id].text.trim())
                            .filter(|s| !s.is_empty());
                        if let Some(axis) = col_axis_hdr {
                            row_bindings.push(format!("{axis} = {htxt}"));
                        } else {
                            row_bindings.push(htxt.to_owned());
                        }
                    }
                }
            }
        }

        let clean_text = cell.text.trim();
        let grounded_context =
            if cell.is_header || (col_headers.is_empty() && row_bindings.is_empty()) {
                clean_text.to_owned()
            } else if !row_bindings.is_empty() && !col_headers.is_empty() {
                format!(
                    "[{} | {}]: {}",
                    row_bindings.join(" / "),
                    col_headers.join(" / "),
                    clean_text
                )
            } else if !col_headers.is_empty() {
                format!("[{}]: {}", col_headers.join(" / "), clean_text)
            } else {
                format!("[{}]: {}", row_bindings.join(" / "), clean_text)
            };

        out.push(GroundedTableCell {
            cell_id: id as u32,
            row_idx: r0,
            col_idx: c0,
            rowspan,
            colspan,
            is_header: cell.is_header,
            text: cell.text.clone(),
            col_headers,
            row_headers,
            grounded_context,
        });
    }
    out
}

fn resize_row(row: &mut Vec<Option<usize>>, width: usize, max_cols: usize) {
    if row.capacity() < width {
        let capacity = width
            .checked_next_power_of_two()
            .unwrap_or(max_cols)
            .min(max_cols);
        row.reserve_exact(capacity - row.len());
    }
    row.resize(width, None);
}

pub fn expand(rows: &[Row], max_rows: usize, max_cols: usize) -> Result<Grid, GridError> {
    let max_slots = max_rows.saturating_mul(max_cols);
    expand_with_slot_limit(rows, max_rows, max_cols, max_slots)
}

/// Expand a logical table while bounding the total number of initialized grid slots.
/// The slot budget is enforced before each row grows, including final rectangular padding.
pub fn expand_with_slot_limit(
    rows: &[Row],
    max_rows: usize,
    max_cols: usize,
    max_slots: usize,
) -> Result<Grid, GridError> {
    if rows.len() > max_rows || max_cols == 0 {
        return Err(GridError::TooLarge);
    }
    // Groups must appear in contiguous, non-decreasing order.
    if rows.windows(2).any(|pair| pair[0].group > pair[1].group) {
        return Err(GridError::InvalidGroupOrder);
    }
    let mut slots: Vec<Vec<Option<usize>>> = vec![Vec::new(); rows.len()];
    let mut cells = Vec::new();
    let mut width = 0;
    let mut group_end = 0;
    let mut initialized_slots = 0usize;
    for (r, row) in rows.iter().enumerate() {
        if r == group_end {
            group_end = (r + 1..rows.len())
                .find(|&i| rows[i].group != row.group)
                .unwrap_or(rows.len());
        }
        let mut c = 0usize;
        for cell in &row.cells {
            while c < slots[r].len() && slots[r][c].is_some() {
                c += 1;
            }
            let dc = cell.colspan.clamp(1, 1000);
            let dr = if cell.rowspan == 0 {
                group_end - r
            } else {
                cell.rowspan.min(65_534).min(group_end - r)
            };
            let end_col = c.checked_add(dc).ok_or(GridError::TooLarge)?;
            if end_col > max_cols {
                return Err(GridError::TooLarge);
            }
            let index = cells.len();
            for line in slots.iter_mut().take(r + dr).skip(r) {
                if line.len() < end_col {
                    initialized_slots = initialized_slots
                        .checked_add(end_col - line.len())
                        .ok_or(GridError::TooManySlots)?;
                    if initialized_slots > max_slots {
                        return Err(GridError::TooManySlots);
                    }
                    resize_row(line, end_col, max_cols);
                }
                for spot in &mut line[c..end_col] {
                    if spot.is_some() {
                        return Err(GridError::Overlap);
                    }
                    *spot = Some(index);
                }
            }
            cells.push(cell.clone());
            width = width.max(end_col);
            c = end_col;
        }
    }
    for row in &mut slots {
        if row.len() < width {
            initialized_slots = initialized_slots
                .checked_add(width - row.len())
                .ok_or(GridError::TooManySlots)?;
            if initialized_slots > max_slots {
                return Err(GridError::TooManySlots);
            }
            resize_row(row, width, max_cols);
        }
    }
    Ok(Grid {
        cells,
        slots,
        width,
    })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn cell(text: &str, rs: usize, cs: usize) -> Cell {
        Cell {
            text: text.to_owned(),
            rowspan: rs,
            colspan: cs,
            is_header: false,
        }
    }
    #[test]
    fn sparse_grid_does_not_reserve_maximum_columns_per_row() {
        let rows = vec![
            Row {
                group: 0,
                cells: vec![cell("x", 1, 1)]
            };
            128
        ];
        let grid = expand(&rows, 128, 1024).unwrap();
        assert_eq!(grid.width, 1);
        assert!(grid.slots.iter().all(|r| r.capacity() <= 8));
    }
    #[test]
    fn allocation_growth_respects_column_budget() {
        for max_cols in [1, 5, 600] {
            let rows = [Row {
                group: 0,
                cells: vec![cell("x", 1, 1); max_cols],
            }];
            let grid = expand(&rows, 1, max_cols).unwrap();
            assert_eq!(grid.width, max_cols);
            assert!(grid.slots[0].capacity() <= max_cols);
        }
    }
    #[test]
    fn slot_budget_accepts_exact_grid_and_rejects_one_slot_below() {
        let rows = [
            Row {
                group: 0,
                cells: vec![cell("A", 2, 2)],
            },
            Row {
                group: 0,
                cells: vec![],
            },
        ];
        let exact = expand_with_slot_limit(&rows, 2, 2, 4).unwrap();
        assert_eq!(exact.width, 2);
        assert_eq!(exact.slots, vec![vec![Some(0); 2], vec![Some(0); 2]]);
        assert_eq!(
            expand_with_slot_limit(&rows, 2, 2, 3),
            Err(GridError::TooManySlots)
        );
    }
    #[test]
    fn overlaps_are_errors_and_group_order_is_validated() {
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
        assert_eq!(expand(&overlap, 2, 3), Err(GridError::Overlap));
        let reversed = [
            Row {
                group: 2,
                cells: vec![],
            },
            Row {
                group: 1,
                cells: vec![],
            },
        ];
        assert_eq!(expand(&reversed, 2, 3), Err(GridError::InvalidGroupOrder));
    }
    #[test]
    fn row_and_col_spans() {
        let rows = [
            Row {
                group: 0,
                cells: vec![cell("A", 2, 2), cell("B", 1, 1)],
            },
            Row {
                group: 0,
                cells: vec![cell("C", 1, 1)],
            },
        ];
        let g = expand(&rows, 100, 10).unwrap();
        assert_eq!(g.width, 3);
        assert_eq!(g.slots[1], vec![Some(0), Some(0), Some(2)]);
    }
    #[test]
    fn zero_rowspan_stops_at_group_end() {
        let rows = [
            Row {
                group: 0,
                cells: vec![cell("A", 0, 1)],
            },
            Row {
                group: 0,
                cells: vec![cell("B", 1, 1)],
            },
            Row {
                group: 1,
                cells: vec![cell("C", 1, 1)],
            },
        ];
        let g = expand(&rows, 10, 10).unwrap();
        assert_eq!(g.slots[1][0], Some(0));
        assert_eq!(g.slots[2][0], Some(2));
    }
}
