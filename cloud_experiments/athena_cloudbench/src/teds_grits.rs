#![forbid(unsafe_code)]

use crate::lcs_uax29::{exact_token_lcs_len, uax29_word_tokens};
use serde::{Deserialize, Serialize};

pub const DEFAULT_MAX_TEDS_NODES: usize = 4096;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TableTreeNode {
    pub tag: String,
    pub rowspan: u32,
    pub colspan: u32,
    pub text: String,
    pub children: Vec<TableTreeNode>,
}

impl TableTreeNode {
    pub fn new(tag: impl Into<String>) -> Self {
        Self {
            tag: tag.into().to_ascii_lowercase(),
            rowspan: 1,
            colspan: 1,
            text: String::new(),
            children: Vec::new(),
        }
    }

    pub fn cell(
        tag: impl Into<String>,
        rowspan: u32,
        colspan: u32,
        text: impl Into<String>,
    ) -> Self {
        Self {
            tag: tag.into().to_ascii_lowercase(),
            rowspan: rowspan.max(1),
            colspan: colspan.max(1),
            text: normalize_ws(&text.into()),
            children: Vec::new(),
        }
    }

    pub fn with_children(mut self, children: Vec<TableTreeNode>) -> Self {
        self.children = children;
        self
    }

    pub fn node_count(&self) -> usize {
        1 + self.children.iter().map(Self::node_count).sum::<usize>()
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct TedsMetrics {
    pub pred_nodes: usize,
    pub gold_nodes: usize,
    pub node_budget_exceeded: bool,
    pub s_teds: f64,
    pub teds: f64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GridCellSpec {
    pub row: usize,
    pub col: usize,
    pub rowspan: usize,
    pub colspan: usize,
    pub is_header: bool,
    pub text: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct GridSlot {
    pub top_offset: i32,
    pub left_offset: i32,
    pub rowspan: i32,
    pub colspan: i32,
    pub text: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TableSlotGrid {
    pub rows: usize,
    pub cols: usize,
    pub slots: Vec<GridSlot>,
}

impl TableSlotGrid {
    pub fn from_cells(rows: usize, cols: usize, cells: &[GridCellSpec]) -> Self {
        let mut slots = vec![
            GridSlot {
                top_offset: 0,
                left_offset: 0,
                rowspan: 1,
                colspan: 1,
                text: String::new(),
            };
            rows.saturating_mul(cols)
        ];
        for c in cells {
            let rs = c.rowspan.max(1);
            let cs = c.colspan.max(1);
            let norm_txt = normalize_ws(&c.text);
            for dr in 0..rs {
                let r = c.row + dr;
                if r >= rows {
                    break;
                }
                for dc in 0..cs {
                    let col = c.col + dc;
                    if col >= cols {
                        break;
                    }
                    slots[r * cols + col] = GridSlot {
                        top_offset: -(dr as i32),
                        left_offset: -(dc as i32),
                        rowspan: rs as i32,
                        colspan: cs as i32,
                        text: norm_txt.clone(),
                    };
                }
            }
        }
        Self { rows, cols, slots }
    }

    #[inline]
    pub fn slot(&self, r: usize, c: usize) -> &GridSlot {
        &self.slots[r * self.cols + c]
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct GritsMetrics {
    pub grits_top: f64,
    pub grits_cont: f64,
    pub grits_top_precision: f64,
    pub grits_top_recall: f64,
    pub grits_cont_precision: f64,
    pub grits_cont_recall: f64,
}

fn normalize_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

struct PostorderTree {
    tags: Vec<String>,
    rowspans: Vec<u32>,
    colspans: Vec<u32>,
    texts: Vec<Vec<char>>,
    leftmost: Vec<usize>, // 1-based postorder index of leftmost leaf
    keyroots: Vec<usize>, // 1-based postorder indices sorted ascending
}

impl PostorderTree {
    fn build(root: &TableTreeNode) -> Self {
        let n = root.node_count();
        let mut tree = Self {
            tags: vec![String::new(); n + 1],
            rowspans: vec![1; n + 1],
            colspans: vec![1; n + 1],
            texts: vec![Vec::new(); n + 1],
            leftmost: vec![0; n + 1],
            keyroots: Vec::new(),
        };
        let mut counter = 0usize;
        Self::traverse(root, &mut counter, &mut tree);

        // Compute LR_keyroots: for each distinct leftmost[i], keep the largest postorder i
        let mut max_for_left = vec![0usize; n + 1];
        for i in 1..=n {
            let l = tree.leftmost[i];
            if i > max_for_left[l] {
                max_for_left[l] = i;
            }
        }
        let mut kr: Vec<usize> = max_for_left.into_iter().filter(|&k| k > 0).collect();
        kr.sort_unstable();
        kr.dedup();
        tree.keyroots = kr;
        tree
    }

    fn traverse(node: &TableTreeNode, counter: &mut usize, out: &mut Self) -> usize {
        let mut first_leaf = 0usize;
        for child in &node.children {
            let child_left = Self::traverse(child, counter, out);
            if first_leaf == 0 {
                first_leaf = child_left;
            }
        }
        *counter += 1;
        let idx = *counter;
        out.tags[idx] = node.tag.clone();
        out.rowspans[idx] = node.rowspan;
        out.colspans[idx] = node.colspan;
        out.texts[idx] = node.text.chars().collect();
        let left = if first_leaf == 0 { idx } else { first_leaf };
        out.leftmost[idx] = left;
        left
    }

    #[inline]
    fn len(&self) -> usize {
        self.tags.len() - 1
    }
}

fn normalized_char_distance(a: &[char], b: &[char]) -> f64 {
    if a.is_empty() && b.is_empty() {
        return 0.0;
    }
    let max_len = a.len().max(b.len());
    if a.is_empty() || b.is_empty() {
        return 1.0;
    }
    let (short, long_seq) = if a.len() <= b.len() { (a, b) } else { (b, a) };
    let mut dp: Vec<usize> = (0..=short.len()).collect();
    for (i, &lc) in long_seq.iter().enumerate() {
        let mut prev = dp[0];
        dp[0] = i + 1;
        for (j, &sc) in short.iter().enumerate() {
            let cur = dp[j + 1];
            let sub = prev + usize::from(lc != sc);
            let ins = dp[j] + 1;
            let del = cur + 1;
            dp[j + 1] = sub.min(ins).min(del);
            prev = cur;
        }
    }
    (dp[short.len()] as f64) / (max_len as f64)
}

fn zhang_shasha_distance(t1: &PostorderTree, t2: &PostorderTree, include_text: bool) -> f64 {
    let n1 = t1.len();
    let n2 = t2.len();
    if n1 == 0 {
        return n2 as f64;
    }
    if n2 == 0 {
        return n1 as f64;
    }
    let cols = n2 + 1;
    let mut td = vec![0.0f64; (n1 + 1) * cols];
    let mut fd = vec![0.0f64; (n1 + 1) * cols];

    for &x in &t1.keyroots {
        let lx = t1.leftmost[x];
        let rows_f = x - lx + 2;
        for &y in &t2.keyroots {
            let ly = t2.leftmost[y];
            let cols_f = y - ly + 2;
            fd[0] = 0.0;
            for di in 1..rows_f {
                fd[di * cols_f] = fd[(di - 1) * cols_f] + 1.0;
            }
            for dj in 1..cols_f {
                fd[dj] = fd[dj - 1] + 1.0;
            }
            for di in 1..rows_f {
                let i = lx + di - 1;
                for dj in 1..cols_f {
                    let j = ly + dj - 1;
                    let ins = fd[di * cols_f + (dj - 1)] + 1.0;
                    let del = fd[(di - 1) * cols_f + dj] + 1.0;
                    let same_struct = t1.tags[i] == t2.tags[j]
                        && t1.rowspans[i] == t2.rowspans[j]
                        && t1.colspans[i] == t2.colspans[j];
                    let sub_cost = if !same_struct {
                        1.0
                    } else if include_text && matches!(t1.tags[i].as_str(), "td" | "th") {
                        normalized_char_distance(&t1.texts[i], &t2.texts[j])
                    } else {
                        0.0
                    };
                    let val = if t1.leftmost[i] == lx && t2.leftmost[j] == ly {
                        let rep = fd[(di - 1) * cols_f + (dj - 1)] + sub_cost;
                        let best = ins.min(del).min(rep);
                        td[i * cols + j] = best;
                        best
                    } else {
                        let p_di = t1.leftmost[i] - lx;
                        let p_dj = t2.leftmost[j] - ly;
                        let sub_tree = fd[p_di * cols_f + p_dj] + td[i * cols + j];
                        ins.min(del).min(sub_tree)
                    };
                    fd[di * cols_f + dj] = val;
                }
            }
        }
    }
    td[n1 * cols + n2]
}

/// Computes bounded Zhang-Shasha `S-TEDS` (structural tag+span skeleton) and `TEDS` (structure + cell content).
pub fn compute_bounded_teds(
    pred: &TableTreeNode,
    gold: &TableTreeNode,
    max_nodes: usize,
) -> TedsMetrics {
    let pred_nodes = pred.node_count();
    let gold_nodes = gold.node_count();
    let budget = max_nodes.max(1);
    if pred_nodes > budget || gold_nodes > budget {
        return TedsMetrics {
            pred_nodes,
            gold_nodes,
            node_budget_exceeded: true,
            s_teds: 0.0,
            teds: 0.0,
        };
    }
    let denom = pred_nodes.max(gold_nodes).max(1) as f64;
    let pt = PostorderTree::build(pred);
    let gt = PostorderTree::build(gold);
    let s_dist = zhang_shasha_distance(&pt, &gt, false);
    let full_dist = zhang_shasha_distance(&pt, &gt, true);
    TedsMetrics {
        pred_nodes,
        gold_nodes,
        node_budget_exceeded: false,
        s_teds: (1.0 - s_dist / denom).clamp(0.0, 1.0),
        teds: (1.0 - full_dist / denom).clamp(0.0, 1.0),
    }
}

#[inline]
fn slot_topology_iou(a: &GridSlot, b: &GridSlot) -> f64 {
    let ay0 = a.top_offset;
    let ax0 = a.left_offset;
    let ay1 = ay0 + a.rowspan.max(1);
    let ax1 = ax0 + a.colspan.max(1);

    let by0 = b.top_offset;
    let bx0 = b.left_offset;
    let by1 = by0 + b.rowspan.max(1);
    let bx1 = bx0 + b.colspan.max(1);

    let iy0 = ay0.max(by0);
    let ix0 = ax0.max(bx0);
    let iy1 = ay1.min(by1);
    let ix1 = ax1.min(bx1);
    if iy1 <= iy0 || ix1 <= ix0 {
        return 0.0;
    }
    let inter = ((iy1 - iy0) as f64) * ((ix1 - ix0) as f64);
    let area_a = ((ay1 - ay0) as f64) * ((ax1 - ax0) as f64);
    let area_b = ((by1 - by0) as f64) * ((bx1 - bx0) as f64);
    let union = area_a + area_b - inter;
    if union <= 0.0 { 0.0 } else { inter / union }
}

fn slot_content_lcs_f1(a: &GridSlot, b: &GridSlot) -> f64 {
    let ta = uax29_word_tokens(&a.text);
    let tb = uax29_word_tokens(&b.text);
    if ta.is_empty() && tb.is_empty() {
        return 1.0;
    }
    if ta.is_empty() || tb.is_empty() {
        return 0.0;
    }
    let lcs = exact_token_lcs_len(&ta, &tb);
    (2.0 * lcs as f64) / ((ta.len() + tb.len()) as f64)
}

fn weighted_1d_lcs_with_alignment(
    n: usize,
    m: usize,
    mut weight_fn: impl FnMut(usize, usize) -> f64,
) -> (f64, Vec<(usize, usize)>) {
    if n == 0 || m == 0 {
        return (0.0, Vec::new());
    }
    let cols = m + 1;
    let mut dp = vec![0.0f64; (n + 1) * cols];
    let mut step = vec![0u8; (n + 1) * cols]; // 1 = diag, 2 = up, 3 = left
    for i in 1..=n {
        for j in 1..=m {
            let w = weight_fn(i - 1, j - 1);
            let diag = dp[(i - 1) * cols + (j - 1)] + w;
            let up = dp[(i - 1) * cols + j];
            let left = dp[i * cols + (j - 1)];
            if diag >= up && diag >= left {
                dp[i * cols + j] = diag;
                step[i * cols + j] = 1;
            } else if up >= left {
                dp[i * cols + j] = up;
                step[i * cols + j] = 2;
            } else {
                dp[i * cols + j] = left;
                step[i * cols + j] = 3;
            }
        }
    }
    let mut pairs = Vec::new();
    let (mut i, mut j) = (n, m);
    while i > 0 && j > 0 {
        match step[i * cols + j] {
            1 => {
                pairs.push((i - 1, j - 1));
                i -= 1;
                j -= 1;
            }
            2 => i -= 1,
            _ => j -= 1,
        }
    }
    pairs.reverse();
    (dp[n * cols + m], pairs)
}

fn factored_2d_lcs_sum(r1: usize, c1: usize, r2: usize, c2: usize, sim_matrix: &[f64]) -> f64 {
    if r1 == 0 || c1 == 0 || r2 == 0 || c2 == 0 {
        return 0.0;
    }
    let at = |i1: usize, j1: usize, i2: usize, j2: usize| -> f64 {
        sim_matrix[(((i1 * c1 + j1) * r2) + i2) * c2 + j2]
    };

    // Pass 1: initial column pairing via row-wise 1D LCS, then row pairing conditioned on matched columns, refined once.
    let (_, mut matched_cols) = weighted_1d_lcs_with_alignment(c1, c2, |j1, j2| {
        weighted_1d_lcs_with_alignment(r1, r2, |i1, i2| at(i1, j1, i2, j2)).0
    });
    let mut best_sum = 0.0f64;
    for _ in 0..2 {
        let (row_sum, matched_rows) = weighted_1d_lcs_with_alignment(r1, r2, |i1, i2| {
            matched_cols
                .iter()
                .map(|&(j1, j2)| at(i1, j1, i2, j2))
                .sum::<f64>()
        });
        best_sum = best_sum.max(row_sum);
        let (col_sum, next_cols) = weighted_1d_lcs_with_alignment(c1, c2, |j1, j2| {
            matched_rows
                .iter()
                .map(|&(i1, i2)| at(i1, j1, i2, j2))
                .sum::<f64>()
        });
        best_sum = best_sum.max(col_sum);
        matched_cols = next_cols;
    }
    best_sum
}

/// Computes Smock et al. (CVPR 2023) `GriTS_Top` (2D relative-span IoU) and `GriTS_Cont` (2D UAX #29 Token-LCS) over WHATWG slot grids.
pub fn compute_grits(pred: &TableSlotGrid, gold: &TableSlotGrid) -> GritsMetrics {
    let area_p = pred.rows.saturating_mul(pred.cols);
    let area_g = gold.rows.saturating_mul(gold.cols);
    if area_p == 0 && area_g == 0 {
        return GritsMetrics {
            grits_top: 1.0,
            grits_cont: 1.0,
            grits_top_precision: 1.0,
            grits_top_recall: 1.0,
            grits_cont_precision: 1.0,
            grits_cont_recall: 1.0,
        };
    }
    if area_p == 0 || area_g == 0 {
        return GritsMetrics {
            grits_top: 0.0,
            grits_cont: 0.0,
            grits_top_precision: 0.0,
            grits_top_recall: 0.0,
            grits_cont_precision: 0.0,
            grits_cont_recall: 0.0,
        };
    }

    let total = area_p * area_g;
    let mut top_sim = vec![0.0f64; total];
    let mut cont_sim = vec![0.0f64; total];
    for i1 in 0..pred.rows {
        for j1 in 0..pred.cols {
            let sp = pred.slot(i1, j1);
            let p_idx = (i1 * pred.cols + j1) * area_g;
            for i2 in 0..gold.rows {
                for j2 in 0..gold.cols {
                    let sg = gold.slot(i2, j2);
                    let flat = p_idx + (i2 * gold.cols + j2);
                    top_sim[flat] = slot_topology_iou(sp, sg);
                    cont_sim[flat] = slot_content_lcs_f1(sp, sg);
                }
            }
        }
    }

    let top_sum = factored_2d_lcs_sum(pred.rows, pred.cols, gold.rows, gold.cols, &top_sim);
    let cont_sum = factored_2d_lcs_sum(pred.rows, pred.cols, gold.rows, gold.cols, &cont_sim);

    let top_prec = (top_sum / area_p as f64).clamp(0.0, 1.0);
    let top_rec = (top_sum / area_g as f64).clamp(0.0, 1.0);
    let grits_top = if top_prec + top_rec > 0.0 {
        (2.0 * top_prec * top_rec) / (top_prec + top_rec)
    } else {
        0.0
    };

    let cont_prec = (cont_sum / area_p as f64).clamp(0.0, 1.0);
    let cont_rec = (cont_sum / area_g as f64).clamp(0.0, 1.0);
    let grits_cont = if cont_prec + cont_rec > 0.0 {
        (2.0 * cont_prec * cont_rec) / (cont_prec + cont_rec)
    } else {
        0.0
    };

    GritsMetrics {
        grits_top,
        grits_cont,
        grits_top_precision: top_prec,
        grits_top_recall: top_rec,
        grits_cont_precision: cont_prec,
        grits_cont_recall: cont_rec,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_gold_tree_and_grid() -> (TableTreeNode, TableSlotGrid) {
        let tree = TableTreeNode::new("table").with_children(vec![
            TableTreeNode::new("thead").with_children(vec![
                TableTreeNode::new("tr").with_children(vec![TableTreeNode::cell(
                    "th",
                    1,
                    2,
                    "Revenue USD",
                )]),
            ]),
            TableTreeNode::new("tbody").with_children(vec![
                TableTreeNode::new("tr").with_children(vec![
                    TableTreeNode::cell("td", 1, 1, "Q1 120"),
                    TableTreeNode::cell("td", 1, 1, "Q2 145"),
                ]),
            ]),
        ]);
        let grid = TableSlotGrid::from_cells(
            2,
            2,
            &[
                GridCellSpec {
                    row: 0,
                    col: 0,
                    rowspan: 1,
                    colspan: 2,
                    is_header: true,
                    text: "Revenue USD".into(),
                },
                GridCellSpec {
                    row: 1,
                    col: 0,
                    rowspan: 1,
                    colspan: 1,
                    is_header: false,
                    text: "Q1 120".into(),
                },
                GridCellSpec {
                    row: 1,
                    col: 1,
                    rowspan: 1,
                    colspan: 1,
                    is_header: false,
                    text: "Q2 145".into(),
                },
            ],
        );
        (tree, grid)
    }

    #[test]
    fn identical_table_scores_exact_one_and_span_or_order_mutations_penalize_teds_and_grits() {
        let (gold_tree, gold_grid) = sample_gold_tree_and_grid();
        let exact_t = compute_bounded_teds(&gold_tree, &gold_tree, DEFAULT_MAX_TEDS_NODES);
        assert!(!exact_t.node_budget_exceeded);
        assert!((exact_t.s_teds - 1.0).abs() < 1e-12);
        assert!((exact_t.teds - 1.0).abs() < 1e-12);

        let exact_g = compute_grits(&gold_grid, &gold_grid);
        assert!((exact_g.grits_top - 1.0).abs() < 1e-12);
        assert!((exact_g.grits_cont - 1.0).abs() < 1e-12);

        // Mutate colspan 2 -> 1 (shifts topology while keeping text)
        let mut span_broken_tree = gold_tree.clone();
        span_broken_tree.children[0].children[0].children[0].colspan = 1;
        let span_t = compute_bounded_teds(&span_broken_tree, &gold_tree, DEFAULT_MAX_TEDS_NODES);
        assert!(span_t.s_teds < 1.0 && span_t.teds < 1.0);

        let span_broken_grid = TableSlotGrid::from_cells(
            2,
            2,
            &[
                GridCellSpec {
                    row: 0,
                    col: 0,
                    rowspan: 1,
                    colspan: 1,
                    is_header: true,
                    text: "Revenue USD".into(),
                },
                GridCellSpec {
                    row: 1,
                    col: 0,
                    rowspan: 1,
                    colspan: 1,
                    is_header: false,
                    text: "Q1 120".into(),
                },
                GridCellSpec {
                    row: 1,
                    col: 1,
                    rowspan: 1,
                    colspan: 1,
                    is_header: false,
                    text: "Q2 145".into(),
                },
            ],
        );
        let span_g = compute_grits(&span_broken_grid, &gold_grid);
        assert!(
            span_g.grits_top < 0.85,
            "colspan shift must drop GriTS_Top: {span_g:?}"
        );

        // Swap Q1 and Q2 columns: structure S-TEDS/GriTS_Top remain 1.0, TEDS & GriTS_Cont drop!
        let mut swapped_tree = gold_tree.clone();
        swapped_tree.children[1].children[0].children.reverse();
        let swap_t = compute_bounded_teds(&swapped_tree, &gold_tree, DEFAULT_MAX_TEDS_NODES);
        assert!((swap_t.s_teds - 1.0).abs() < 1e-12);
        assert!(
            swap_t.teds < 0.90,
            "swapped cell order must drop ordered TEDS: {swap_t:?}"
        );

        // Exceeding node budget fails closed cleanly without OOM
        let over_budget = compute_bounded_teds(&gold_tree, &gold_tree, 3);
        assert!(over_budget.node_budget_exceeded);
        assert_eq!(over_budget.teds, 0.0);
    }
}
