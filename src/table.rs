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
    InvalidGroupOrder,
    Overlap,
}
impl fmt::Display for GridError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for GridError {}

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
        resize_row(row, width, max_cols);
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
