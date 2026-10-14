//! Bounded logical grid builder. Preserves overlaps as errors and handles
//! rowspan=0 by extending to the end of its row group. Unlike flat Markdown,
//! the value at each logical slot references a single source cell.
use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Cell {
    pub text: String,
    pub rowspan: usize,  // zero means remaining rows in the group
    pub colspan: usize,
    pub is_header: bool,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row { pub group: usize, pub cells: Vec<Cell> }
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Grid { pub cells: Vec<Cell>, pub slots: Vec<Vec<Option<usize>>>, pub width: usize }
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum GridError { TooLarge, InvalidGroupOrder, Overlap }
impl fmt::Display for GridError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result { write!(f, "{self:?}") }
}
impl std::error::Error for GridError {}

pub fn expand(rows: &[Row], max_rows: usize, max_cols: usize) -> Result<Grid, GridError> {
    if rows.len() > max_rows || max_cols == 0 { return Err(GridError::TooLarge); }
    // Groups must appear in contiguous, non-decreasing order.
    if rows.windows(2).any(|pair| pair[0].group > pair[1].group) {
        return Err(GridError::InvalidGroupOrder);
    }
    let mut slots = vec![vec![None; max_cols]; rows.len()];
    let mut cells = Vec::new();
    let mut width = 0;
    for (r, row) in rows.iter().enumerate() {
        let group_end = (r + 1..rows.len()).find(|&i| rows[i].group != row.group)
            .unwrap_or(rows.len());
        let mut c = 0usize;
        for cell in &row.cells {
            while c < max_cols && slots[r][c].is_some() { c += 1; }
            let dc = cell.colspan.max(1).min(1000);
            let dr = if cell.rowspan == 0 { group_end - r }
                     else { cell.rowspan.min(65_534).min(group_end - r) };
            let end_col = c.checked_add(dc).ok_or(GridError::TooLarge)?;
            if end_col > max_cols { return Err(GridError::TooLarge); }
            let index = cells.len();
            for line in slots.iter_mut().take(r + dr).skip(r) {
                for spot in &mut line[c..end_col] {
                    if spot.is_some() { return Err(GridError::Overlap); }
                    *spot = Some(index);
                }
            }
            cells.push(cell.clone());
            width = width.max(end_col);
            c = end_col;
        }
    }
    for row in &mut slots { row.truncate(width); }
    Ok(Grid { cells, slots, width })
}
#[cfg(test)]
mod tests {
    use super::*;
    fn cell(text: &str, rs: usize, cs: usize) -> Cell {
        Cell { text: text.to_owned(), rowspan: rs, colspan: cs, is_header: false }
    }
    #[test]
    fn row_and_col_spans() {
        let rows = [
            Row { group: 0, cells: vec![cell("A",2,2),cell("B",1,1)] },
            Row { group: 0, cells: vec![cell("C",1,1)] },
        ];
        let g = expand(&rows, 100, 10).unwrap();
        assert_eq!(g.width, 3);
        assert_eq!(g.slots[1], vec![Some(0),Some(0),Some(2)]);
    }
    #[test]
    fn zero_rowspan_stops_at_group_end() {
        let rows = [Row { group:0, cells:vec![cell("A",0,1)] },
                    Row { group:0, cells:vec![cell("B",1,1)] },
                    Row { group:1, cells:vec![cell("C",1,1)] }];
        let g = expand(&rows, 10, 10).unwrap();
        assert_eq!(g.slots[1][0], Some(0));
        assert_eq!(g.slots[2][0], Some(2));
    }
}
