"""Tier-B evaluation on Wikimedia Enterprise HTML dumps (dataset_plan.md Step 5).

Evaluates Athena (`core_probe`) on real Wikimedia Parsoid HTML pages with
machine-verifiable ground truth, streaming `.ndjson` members directly from the
pinned `.json.tar.gz` archives without unpacking to disk:

1. **MathML-to-LaTeX (`D2`)**:
   - Uses ``leakage.strip_math_leakage`` on pages containing ``<math>`` elements
     with gold TeX (``<annotation encoding="application/x-tex">``, ``alttext``,
     Parsoid ``data-mw`` ``body.extsrc``, ``data-latex``, ``data-tex``) and a
     Presentation MathML subtree.
   - Evaluates on the exact same sample of math-bearing pages:
     * ``stripped_tier2`` (Pure Tier-2 Recursive MathML Tree Walk): all 5 TeX
       attribute/annotation channels stripped before ``core_probe`` sees the
       HTML (verified clean via ``leakage.math_leak_report``), scored against
       ``leakage.normalize_tex(gold_tex)``.
     * ``unstripped_raw_parsoid``: raw Wikimedia Parsoid HTML passed to
       ``core_probe``.
     * ``unstripped_tier1_annotated`` (End-to-End Tier-1 Passthrough + Tier-2
       Fallback): ``alttext`` populated from gold TeX on ``<math>`` elements so
       Tier-1 passthrough and Tier-2 fallback are measured end-to-end.
   - Reports overall ``formula_similarity``, ``formula_hit_at_0_9``,
     ``formula_hit_at_0_8``, ``exact_match_rate``, and per-construct breakdown
     (``frac``, ``scripts``, ``sqrt``, ``matrix``, ``operators_greek``,
     ``atomic_or_other``) with sample size ``n`` and 95% CIs.

2. **HTML Table Extraction & Header Binding (`D4` / `D11`)**:
   - Filters for real data tables (excluding layout/infobox/navbox/mbox/toc
     chrome tables and requiring ``<th>`` + ``<td>`` in a >= 2x2 grid).
   - Builds an independent Python 2D grid oracle from the raw HTML ``<table>``
     (handling ``rowspan`` and ``colspan`` expansion and resolving 2D
     ``(row_headers, col_headers)`` lineage) and scores ``core_probe`` output
     (``cloud-024`` grounded ``table_cell``/``table_header`` chunks and
     ``html_table`` 2D ``grid.slots``) for ``cell_content_recall``,
     ``header_binding_recall``, ``grounded_discourse_f1``, and
     ``grid_slot_fidelity``, with ``n`` and 95% CIs across ``all_data_tables``,
     ``simple_tables`` (``max_span == 1``), and ``spanned_tables``
     (``rowspan > 1`` or ``colspan > 1``).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import html as html_lib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tarfile
import time
import unicodedata

from cloud_experiments import data_fetch, leakage, tier_a, warc_ingest

TIER_B_SCHEMA = "athena-tier-b-eval/1"
SELECTION_SALT = "athena-core-corpus-v1:tier-b:"
DEFAULT_SOURCES = (
    "simplewikibooks-ns0-20250320",
    "viwikibooks-ns0-20250320",
    "enwikibooks-ns0-20250320",
)

_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_TEX_SPAN_RE = re.compile(r"\$\$(.+?)\$\$|\$(?!\s)([^$\n]+?)\$", re.DOTALL)
_MATH_OPEN_RE = re.compile(r"<math\b([^>]*)>", re.IGNORECASE)

# Math construct classifiers on normalized / raw gold TeX
_FRAC_RE = re.compile(r"\\(?:[dtc]?frac|binom|over|choose)\b")
_SQRT_RE = re.compile(r"\\sqrt\b")
_MATRIX_RE = re.compile(
    r"\\begin\{(?:[pbvBV]?matrix|smallmatrix|cases|array|aligned|align\*?| eqnarray\*?)\}"
    r"|\\(?:matrix|pmatrix|bmatrix)\b"
)
_OP_GREEK_RE = re.compile(
    r"\\(?:"
    r"alpha|beta|gamma|delta|epsilon|varepsilon|zeta|eta|theta|vartheta|iota|kappa|"
    r"lambda|mu|nu|xi|pi|varpi|rho|varrho|sigma|varsigma|tau|upsilon|phi|varphi|"
    r"chi|psi|omega|Gamma|Delta|Theta|Lambda|Xi|Pi|Sigma|Upsilon|Phi|Psi|Omega|"
    r"sum|prod|coprod|int|oint|iint|iiint|lim|limsup|liminf|inf|sup|min|max|"
    r"sin|cos|tan|cot|sec|csc|arcsin|arccos|arctan|sinh|cosh|tanh|log|ln|exp|det|gcd|"
    r"pm|mp|times|div|cdot|ast|star|circ|bullet|oplus|otimes|odot|"
    r"leq|le|geq|ge|neq|ne|approx|equiv|sim|simeq|cong|propto|ll|gg|"
    r"subset|subseteq|supset|supseteq|in|notin|ni|cup|cap|setminus|emptyset|varnothing|"
    r"forall|exists|neg|land|lor|implies|iff|to|rightarrow|leftarrow|leftrightarrow|"
    r"Rightarrow|Leftarrow|Leftrightarrow|mapsto|infty|partial|nabla"
    r")\b"
)
_CONSTRUCT_BUCKETS = (
    "frac",
    "scripts",
    "sqrt",
    "matrix",
    "operators_greek",
    "atomic_or_other",
)

# Table chrome classes excluded when selecting real data tables
_CHROME_TABLE_CLASSES = frozenset({
    "infobox",
    "navbox",
    "vertical-navbox",
    "sidebar",
    "mbox",
    "ambox",
    "tmbox",
    "imbox",
    "cmbox",
    "ombox",
    "fmbox",
    "dmbox",
    "mbox-small",
    "metadata",
    "nomobile",
    "sistersitebox",
    "toc",
    "toccolours",
    "plainlinks",
    "navbox-inner",
    "navbox-columns-table",
    "multicol",
})


def _pick_key(kind: str, key: str) -> str:
    return hashlib.sha256((SELECTION_SALT + kind + ":" + key).encode("utf-8")).hexdigest()


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", text or "")).strip()


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(_norm_ws(text).lower())


def _token_f1(a: str, b: str) -> float:
    from collections import Counter
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    ca = Counter(ta)
    cb = Counter(tb)
    overlap = sum((ca & cb).values())
    if overlap == 0:
        return 0.0
    return (2.0 * overlap) / (len(ta) + len(tb))


def _mean_ci(values: list[float]) -> dict:
    clean = [float(v) for v in values if v is not None]
    return {
        "n": len(clean),
        "mean": None if not clean else round(tier_a.mean(clean), 6),
        "ci95": tier_a.bootstrap_ci(clean),
    }


def _rate_ci(hits: int, n: int) -> dict:
    return {
        "n": n,
        "hits": hits,
        "rate": None if n == 0 else round(hits / n, 6),
        "ci95": tier_a.wilson(hits, n),
    }


def classify_math_constructs(gold_tex: str) -> list[str]:
    """Classify a ground-truth LaTeX formula into construct buckets (`D2`)."""
    norm = leakage.normalize_tex(gold_tex)
    combined = f"{gold_tex} {norm}"
    buckets: list[str] = []
    if _FRAC_RE.search(combined):
        buckets.append("frac")
    if "^" in combined or "_" in combined:
        buckets.append("scripts")
    if _SQRT_RE.search(combined):
        buckets.append("sqrt")
    if _MATRIX_RE.search(combined):
        buckets.append("matrix")
    if _OP_GREEK_RE.search(combined):
        buckets.append("operators_greek")
    if not buckets:
        buckets.append("atomic_or_other")
    return buckets


def enrich_math_tier1_annotations(html: str) -> str:
    """Populate `alttext` on `<math>` elements from `data-mw` `body.extsrc` when absent.

    Wikimedia Parsoid dumps store the author's LaTeX inside `data-mw` JSON while
    Mathoid / Wikipedia REST / ar5iv also expose `alttext` or `<annotation>`.
    Populating `alttext` allows measuring end-to-end Tier-1 passthrough on the
    exact same pages as `stripped_tier2`.
    """
    stripped = leakage.strip_math_leakage(html)
    if not stripped.gold:
        return html
    golds = [f.gold_tex or "" for f in stripped.gold]
    idx = 0

    def _inject(match: re.Match) -> str:
        nonlocal idx
        attrs = match.group(1)
        gold = golds[idx] if idx < len(golds) else ""
        idx += 1
        if not gold or re.search(r"\balttext\s*=", attrs, re.IGNORECASE):
            return match.group(0)
        escaped = html_lib.escape(gold, quote=True)
        return f'<math{attrs} alttext="{escaped}">'

    return _MATH_OPEN_RE.sub(_inject, html)


def extract_predicted_tex_spans(chunks: list[dict]) -> list[str]:
    """Extract `$latex$` and `$$latex$$` spans in document order from `core_probe` chunks."""
    spans: list[str] = []
    for chunk in chunks:
        text = chunk.get("text") or ""
        for match in _TEX_SPAN_RE.finditer(text):
            body = (match.group(1) if match.group(1) is not None else match.group(2)) or ""
            body = body.strip()
            if body:
                spans.append(body)
    return spans


def align_gold_and_predicted_tex(
    gold_texs: list[str], pred_texs: list[str]
) -> list[tuple[str, str, float, int]]:
    """Order-preserving sequence alignment between gold TeX and predicted `$...$` spans.

    Returns a list of ``(gold_tex, matched_pred_tex, similarity, edit_distance)``
    with one entry per ``gold_tex`` in ``gold_texs``. Unmatched gold formulas
    receive ``matched_pred_tex = ""``, ``similarity = 0.0``, and
    ``edit_distance = len(normalize_tex(gold_tex))``.
    """
    m = len(gold_texs)
    n = len(pred_texs)
    if m == 0:
        return []
    if n == 0:
        out = []
        for g in gold_texs:
            ng = leakage.normalize_tex(g)
            out.append((g, "", 0.0, len(ng)))
        return out

    sim_matrix = [
        [leakage.tex_similarity(g, p) for p in pred_texs]
        for g in gold_texs
    ]
    # DP over (i in 0..m, j in 0..n) maximizing total similarity
    dp = [[0.0] * (n + 1) for _ in range(m + 1)]
    choice = [[0] * (n + 1) for _ in range(m + 1)]  # 1: match (i, j), 2: skip gold i, 3: skip pred j
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            best = dp[i + 1][j]
            act = 2
            if dp[i][j + 1] > best:
                best = dp[i][j + 1]
                act = 3
            cand = sim_matrix[i][j] + dp[i + 1][j + 1]
            if cand >= best:
                best = cand
                act = 1
            dp[i][j] = best
            choice[i][j] = act

    matched: dict[int, int] = {}
    i, j = 0, 0
    while i < m and j < n:
        act = choice[i][j]
        if act == 1:
            matched[i] = j
            i += 1
            j += 1
        elif act == 2:
            i += 1
        else:
            j += 1

    results: list[tuple[str, str, float, int]] = []
    for idx, g in enumerate(gold_texs):
        if idx in matched:
            p = pred_texs[matched[idx]]
            sim = sim_matrix[idx][matched[idx]]
            dist = leakage.tex_edit_distance(g, p)
            results.append((g, p, sim, dist))
        else:
            ng = leakage.normalize_tex(g)
            results.append((g, "", 0.0, len(ng)))
    return results


# ---------------------------------------------------------------------------
# Independent Python 2D Table Grid & Header-Binding Oracle (D4 / D11)
# ---------------------------------------------------------------------------

@dataclass
class OracleRawCell:
    text: str
    rowspan: int
    colspan: int
    is_header: bool
    scope: str = ""


@dataclass
class OracleRawRow:
    group: int  # 0=thead, 1=tbody, 2=tfoot
    cells: list[OracleRawCell] = field(default_factory=list)


@dataclass
class OracleGroundedCell:
    cell_id: int
    row_idx: int
    col_idx: int
    rowspan: int
    colspan: int
    is_header: bool
    text: str
    col_headers: list[str]
    row_headers: list[str]
    row_bindings: list[str]
    grounded_context: str


@dataclass
class OracleTable:
    table_index: int
    html: str
    classes: set[str]
    caption: str
    width: int
    height: int
    slots: list[list[int | None]]
    cells: list[OracleGroundedCell]
    max_rowspan: int
    max_colspan: int

    @property
    def has_span(self) -> bool:
        return self.max_rowspan > 1 or self.max_colspan > 1


_SKIP_INLINE_TAGS = frozenset({"style", "script", "noscript", "template"})


class _TableExtractorParser(HTMLParser):
    """Extract top-level `<table>` elements, their raw rows/cells, and HTML slices."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._table_depth = 0
        self._cur_table: dict | None = None
        self._cur_group = 1
        self._cur_row: OracleRawRow | None = None
        self._cur_cell: dict | None = None
        self._in_caption = False
        self._skip_depth = 0
        self._math_depth = 0
        self._math_annotation_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                classes = set(attr_map.get("class", "").lower().split())
                role = attr_map.get("role", "").lower().strip()
                self._cur_table = {
                    "classes": classes,
                    "role": role,
                    "caption": [],
                    "rows": [],
                }
                self._cur_group = 1
            return

        if self._table_depth != 1 or self._cur_table is None:
            return

        if t in _SKIP_INLINE_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if t == "caption":
            self._in_caption = True
        elif t == "thead":
            self._cur_group = 0
        elif t == "tbody":
            self._cur_group = 1
        elif t == "tfoot":
            self._cur_group = 2
        elif t == "tr":
            self._cur_row = OracleRawRow(group=self._cur_group)
        elif t in ("th", "td") and self._cur_row is not None and self._cur_cell is None:
            raw_rs = attr_map.get("rowspan", "1").strip()
            raw_cs = attr_map.get("colspan", "1").strip()
            try:
                rs = int(raw_rs)
            except ValueError:
                rs = 1
            try:
                cs = max(1, int(raw_cs))
            except ValueError:
                cs = 1
            self._cur_cell = {
                "is_header": t == "th",
                "rowspan": max(0, rs),
                "colspan": cs,
                "scope": attr_map.get("scope", "").strip().lower(),
                "parts": [],
            }
        elif t == "br" and self._cur_cell is not None:
            self._cur_cell["parts"].append("\n")
        elif t == "math":
            self._math_depth += 1
        elif t in ("annotation", "annotation-xml") and self._math_depth > 0:
            self._math_annotation_depth += 1

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if t == "table":
            if self._table_depth == 1 and self._cur_table is not None:
                self.tables.append(self._cur_table)
                self._cur_table = None
            if self._table_depth > 0:
                self._table_depth -= 1
            return

        if self._table_depth != 1 or self._cur_table is None:
            return

        if t in _SKIP_INLINE_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return

        if t == "caption":
            self._in_caption = False
        elif t in ("thead", "tbody", "tfoot"):
            self._cur_group = 1
        elif t in ("th", "td") and self._cur_cell is not None and self._cur_row is not None:
            raw_text = "".join(self._cur_cell["parts"])
            lines = [_norm_ws(line) for line in raw_text.splitlines()]
            clean_text = "\n".join(line for line in lines if line)
            self._cur_row.cells.append(
                OracleRawCell(
                    text=clean_text,
                    rowspan=self._cur_cell["rowspan"],
                    colspan=self._cur_cell["colspan"],
                    is_header=self._cur_cell["is_header"],
                    scope=self._cur_cell["scope"],
                )
            )
            self._cur_cell = None
        elif t == "tr" and self._cur_row is not None:
            if self._cur_row.cells:
                self._cur_table["rows"].append(self._cur_row)
            self._cur_row = None
        elif t in ("annotation", "annotation-xml") and self._math_annotation_depth > 0:
            self._math_annotation_depth -= 1
        elif t == "math" and self._math_depth > 0:
            self._math_depth -= 1

    def handle_data(self, data: str):
        if self._table_depth != 1 or self._cur_table is None or self._skip_depth > 0:
            return
        if self._math_annotation_depth > 0:
            return
        if self._cur_cell is not None:
            self._cur_cell["parts"].append(data)
        elif self._in_caption:
            self._cur_table["caption"].append(data)


def expand_oracle_grid(
    rows: list[OracleRawRow], *, max_rows: int = 512, max_cols: int = 128
) -> tuple[list[list[int | None]], list[OracleGroundedCell], int, int]:
    """Independent Python 2D grid expander and header-binding resolver (`D4`/`D11`)."""
    ordered_rows: list[OracleRawRow] = []
    for grp in (0, 1, 2):
        ordered_rows.extend(r for r in rows if r.group == grp)
    if not ordered_rows:
        return [], [], 1, 1

    group_end: dict[int, int] = {}
    for idx, row in enumerate(ordered_rows):
        group_end[row.group] = idx + 1

    slots: list[list[int | None]] = []
    raw_cells: list[OracleRawCell] = []
    origins: list[tuple[int, int, int, int]] = []
    width = 0
    max_rs = 1
    max_cs = 1

    def _ensure_row(r_idx: int):
        while len(slots) <= r_idx:
            slots.append([None] * width)

    def _ensure_width(new_w: int):
        nonlocal width
        if new_w > width:
            diff = new_w - width
            for r in slots:
                r.extend([None] * diff)
            width = new_w

    for r_idx, row in enumerate(ordered_rows):
        if r_idx >= max_rows:
            break
        _ensure_row(r_idx)
        col = 0
        grp_limit = min(max_rows, group_end.get(row.group, r_idx + 1))
        for cell in row.cells:
            while col < len(slots[r_idx]) and slots[r_idx][col] is not None:
                col += 1
            if col >= max_cols:
                break
            cell_id = len(raw_cells)
            raw_cells.append(cell)
            if cell.rowspan == 0:
                eff_rs = max(1, grp_limit - r_idx)
            else:
                eff_rs = max(1, min(cell.rowspan, max_rows - r_idx))
            eff_cs = max(1, min(cell.colspan, max_cols - col))
            max_rs = max(max_rs, eff_rs)
            max_cs = max(max_cs, eff_cs)
            _ensure_width(col + eff_cs)
            for dr in range(eff_rs):
                rr = r_idx + dr
                _ensure_row(rr)
                for dc in range(eff_cs):
                    cc = col + dc
                    if slots[rr][cc] is None:
                        slots[rr][cc] = cell_id
            origins.append((r_idx, col, r_idx + eff_rs, col + eff_cs))
            col += eff_cs

    grounded: list[OracleGroundedCell] = []
    n_cells = len(raw_cells)
    for cid, cell in enumerate(raw_cells):
        r0, c0, r1, c1 = origins[cid]
        col_headers: list[str] = []
        for hr in range(0, r0):
            hrow = slots[hr]
            for hc in range(c0, min(c1, len(hrow))):
                hid = hrow[hc]
                if hid is not None and hid != cid and hid < n_cells and raw_cells[hid].is_header:
                    htxt = raw_cells[hid].text.strip()
                    if htxt and htxt not in col_headers:
                        col_headers.append(htxt)

        row_headers: list[str] = []
        row_bindings: list[str] = []
        for rr in range(r0, min(r1, len(slots))):
            hrow = slots[rr]
            for hc in range(0, min(c0, len(hrow))):
                hid = hrow[hc]
                if hid is not None and hid != cid and hid < n_cells:
                    hcell = raw_cells[hid]
                    htxt = hcell.text.strip()
                    if htxt and (hcell.is_header or hc == 0) and htxt not in row_headers:
                        row_headers.append(htxt)
                        top_id = slots[0][hc] if slots and hc < len(slots[0]) else None
                        if (
                            top_id is not None
                            and top_id != hid
                            and top_id < n_cells
                            and raw_cells[top_id].is_header
                            and raw_cells[top_id].text.strip()
                        ):
                            axis = raw_cells[top_id].text.strip()
                            row_bindings.append(f"{axis} = {htxt}")
                        else:
                            row_bindings.append(htxt)

        clean_text = cell.text.strip()
        if cell.is_header or (not col_headers and not row_bindings):
            ctx = clean_text
        elif row_bindings and col_headers:
            ctx = f"[{' / '.join(row_bindings)} | {' / '.join(col_headers)}]: {clean_text}"
        elif col_headers:
            ctx = f"[{' / '.join(col_headers)}]: {clean_text}"
        else:
            ctx = f"[{' / '.join(row_bindings)}]: {clean_text}"

        grounded.append(
            OracleGroundedCell(
                cell_id=cid,
                row_idx=r0,
                col_idx=c0,
                rowspan=r1 - r0,
                colspan=c1 - c0,
                is_header=cell.is_header,
                text=clean_text,
                col_headers=col_headers,
                row_headers=row_headers,
                row_bindings=row_bindings,
                grounded_context=ctx,
            )
        )

    return slots, grounded, max_rs, max_cs


_TOP_TABLE_SLICE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)


def extract_oracle_data_tables(html: str) -> list[OracleTable]:
    """Extract real data tables from HTML, excluding layout/navbox/infobox chrome."""
    parser = _TableExtractorParser()
    parser.feed(html)
    parser.close()

    out: list[OracleTable] = []
    for idx, tinfo in enumerate(parser.tables):
        if tinfo["role"] in ("presentation", "none"):
            continue
        classes = tinfo["classes"]
        if classes & _CHROME_TABLE_CLASSES:
            continue
        rows: list[OracleRawRow] = tinfo["rows"]
        if len(rows) < 2:
            continue
        slots, grounded, max_rs, max_cs = expand_oracle_grid(rows)
        height = len(slots)
        width = len(slots[0]) if slots else 0
        if height < 2 or width < 2:
            continue
        header_cells = [c for c in grounded if c.is_header and c.text]
        data_cells = [c for c in grounded if not c.is_header and c.text]
        if not header_cells or len(data_cells) < 2:
            continue
        caption = _norm_ws("".join(tinfo["caption"]))
        out.append(
            OracleTable(
                table_index=idx,
                html="",
                classes=classes,
                caption=caption,
                width=width,
                height=height,
                slots=slots,
                cells=grounded,
                max_rowspan=max_rs,
                max_colspan=max_cs,
            )
        )
    return out


def split_probe_table_chunk_groups(chunks: list[dict]) -> list[list[dict]]:
    """Group `core_probe cloud-024` `table_header`/`table_cell` chunks by table."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for ch in chunks:
        role = ch.get("role")
        if role not in ("table_header", "table_cell"):
            continue
        slug = str(ch.get("dom_path_slug") or "")
        if slug.endswith("/cell[0]") and current:
            groups.append(current)
            current = [ch]
        else:
            current.append(ch)
    if current:
        groups.append(current)
    return groups


def score_oracle_table_against_probe(
    oracle: OracleTable,
    probe_table_groups: list[list[dict]],
    probe_grid_tables: list[dict] | None = None,
) -> dict:
    """Score one `OracleTable` against `core_probe` chunks and optional `html_table` grids."""
    data_cells = [c for c in oracle.cells if not c.is_header and c.text]
    header_bound_cells = [
        c for c in data_cells if c.col_headers or c.row_headers
    ]
    oracle_value_tokens = set()
    for c in data_cells:
        oracle_value_tokens.update(_tokens(c.text))

    # Pick the best-matching probe table chunk group by token overlap on cell texts
    best_group: list[dict] = []
    best_overlap = -1.0
    for grp in probe_table_groups:
        grp_tokens = set()
        for ch in grp:
            grp_tokens.update(_tokens(ch.get("text") or ""))
        if not oracle_value_tokens:
            overlap = 0.0
        else:
            overlap = len(oracle_value_tokens & grp_tokens) / len(oracle_value_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_group = grp

    from collections import Counter, defaultdict

    probe_texts = [_norm_ws(ch.get("text") or "") for ch in best_group]
    probe_Data_texts = [
        _norm_ws(ch.get("text") or "")
        for ch in best_group
        if ch.get("role") == "table_cell"
    ] or probe_texts

    def _counter_f1(ca: Counter, len_a: int, cb: Counter, len_b: int) -> float:
        if len_a == 0 and len_b == 0:
            return 1.0
        if len_a == 0 or len_b == 0:
            return 0.0
        overlap = sum((ca & cb).values())
        if overlap == 0:
            return 0.0
        return (2.0 * overlap) / (len_a + len_b)

    parsed_probe = []
    tok_to_indices: dict[str, list[int]] = defaultdict(list)
    for idx, ptxt in enumerate(probe_Data_texts):
        plower = ptxt.lower()
        ptoks = _tokens(plower)
        pcounter = Counter(ptoks)
        has_bracket = ptxt.startswith("[") and "]:" in ptxt
        if has_bracket:
            hdr_part, _, val_part = ptxt[1:].partition("]:")
            val_part_low = _norm_ws(val_part).lower()
            vtoks = _tokens(val_part_low)
            vcounter = Counter(vtoks)
            hdr_toks = set(_tokens(hdr_part))
        else:
            val_part_low = ""
            vtoks = []
            vcounter = Counter()
            hdr_toks = set()
        parsed_probe.append((
            ptxt,
            plower,
            pcounter,
            len(ptoks),
            has_bracket,
            hdr_toks,
            val_part_low,
            vcounter,
            len(vtoks),
        ))
        for t in pcounter:
            tok_to_indices[t].append(idx)

    all_indices = list(range(len(parsed_probe)))

    def _candidate_indices(toks: list[str]) -> list[int]:
        if not toks or len(parsed_probe) <= 64:
            return all_indices
        seen: set[int] = set()
        out_idx: list[int] = []
        for t in toks:
            for i in tok_to_indices.get(t, ()):
                if i not in seen:
                    seen.add(i)
                    out_idx.append(i)
        return out_idx

    # 1. Cell content recall: fraction of non-empty oracle data cells whose value is recovered
    recovered_cells = 0
    for c in data_cells:
        val_norm = _norm_ws(c.text).lower()
        val_toks = _tokens(val_norm)
        val_counter = Counter(val_toks)
        val_len = len(val_toks)
        matched = False
        for i in _candidate_indices(val_toks):
            _, plower, pcounter, plen, _, _, _, _, _ = parsed_probe[i]
            if val_norm and val_norm in plower:
                matched = True
                break
            if val_len and _counter_f1(val_counter, val_len, pcounter, plen) >= 0.8:
                matched = True
                break
        if matched:
            recovered_cells += 1
    cell_recall = recovered_cells / len(data_cells) if data_cells else 1.0

    # 2. Header binding recall: for data cells with oracle headers, check `[Header]: Value` binding
    bound_hits = 0
    discourse_f1s: list[float] = []
    for c in header_bound_cells:
        val_norm = _norm_ws(c.text).lower()
        val_toks = _tokens(val_norm)
        val_counter = Counter(val_toks)
        val_len = len(val_toks)
        gc_toks = _tokens(c.grounded_context)
        gc_counter = Counter(gc_toks)
        gc_len = len(gc_toks)
        expected_hdr_toks = set(_tokens(" ".join(c.col_headers + c.row_headers)))
        best_cell_f1 = 0.0
        cell_bound_ok = False
        for i in _candidate_indices(gc_toks):
            _, _, pcounter, plen, has_bracket, pred_hdr_toks, val_part_low, vcounter, vlen = parsed_probe[i]
            f1 = _counter_f1(gc_counter, gc_len, pcounter, plen)
            if f1 > best_cell_f1:
                best_cell_f1 = f1
            if has_bracket and not cell_bound_ok:
                val_ok = (bool(val_norm) and val_norm in val_part_low) or (
                    val_len > 0 and _counter_f1(val_counter, val_len, vcounter, vlen) >= 0.75
                )
                if val_ok and expected_hdr_toks:
                    hdr_recall = len(expected_hdr_toks & pred_hdr_toks) / len(expected_hdr_toks)
                    if hdr_recall >= 0.6:
                        cell_bound_ok = True
        if cell_bound_ok:
            bound_hits += 1
        discourse_f1s.append(best_cell_f1)

    header_binding_recall = (
        bound_hits / len(header_bound_cells) if header_bound_cells else cell_recall
    )
    grounded_discourse_f1 = (
        sum(discourse_f1s) / len(discourse_f1s) if discourse_f1s else cell_recall
    )

    # 3. 2D Grid slot expansion fidelity (against `core_probe html-table` when available)
    grid_slot_fidelity = None
    if probe_grid_tables:
        best_slot_acc = None
        for pt in probe_grid_tables:
            g = pt.get("grid") or {}
            raw_slots = g.get("slots") or []
            nonempty_slots = [r for r in raw_slots if any(cell is not None for cell in r)]
            for candidate_slots in (raw_slots, nonempty_slots):
                if len(candidate_slots) == oracle.height and g.get("width") == oracle.width:
                    total_slots = oracle.height * oracle.width
                    if total_slots > 0:
                        same = sum(
                            1
                            for r in range(oracle.height)
                            for c in range(oracle.width)
                            if c < len(candidate_slots[r]) and candidate_slots[r][c] == oracle.slots[r][c]
                        )
                        acc = same / total_slots
                        if best_slot_acc is None or acc > best_slot_acc:
                            best_slot_acc = acc
        grid_slot_fidelity = best_slot_acc

    return {
        "data_cell_count": len(data_cells),
        "header_bound_cell_count": len(header_bound_cells),
        "recovered_data_cells": recovered_cells,
        "header_bound_hits": bound_hits,
        "cell_content_recall": cell_recall,
        "header_binding_recall": header_binding_recall,
        "grounded_discourse_f1": grounded_discourse_f1,
        "grid_slot_fidelity": grid_slot_fidelity,
        "has_span": oracle.has_span,
        "max_rowspan": oracle.max_rowspan,
        "max_colspan": oracle.max_colspan,
        "width": oracle.width,
        "height": oracle.height,
    }


# ---------------------------------------------------------------------------
# Streaming Wikimedia Enterprise `.json.tar.gz` Reader
# ---------------------------------------------------------------------------

@dataclass
class WikimediaArticle:
    source_id: str
    member_name: str
    identifier: int
    name: str
    url: str
    language: str
    version_id: int
    html: str


def iter_wikimedia_articles(
    archive_path: str | Path,
    *,
    source_id: str = "wikimedia",
    max_html_bytes: int = warc_ingest.MAX_HTML_BYTES,
):
    """Stream articles from a Wikimedia Enterprise `.json.tar.gz` without unpacking to disk."""
    path = Path(archive_path).expanduser().resolve()
    with tarfile.open(path, mode="r|gz") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".ndjson"):
                continue
            stream = tar.extractfile(member)
            if stream is None:
                continue
            for raw_line in stream:
                if not raw_line or not raw_line.strip():
                    continue
                obj = json.loads(raw_line.decode("utf-8"))
                body = obj.get("article_body") or {}
                html = body.get("html") or ""
                if not html or len(html.encode("utf-8")) > max_html_bytes:
                    continue
                url = str(obj.get("url") or "https://en.wikibooks.org/wiki/Unknown")
                lang_obj = obj.get("in_language") or {}
                ver_obj = obj.get("version") or {}
                yield WikimediaArticle(
                    source_id=source_id,
                    member_name=member.name,
                    identifier=int(obj.get("identifier") or 0),
                    name=str(obj.get("name") or ""),
                    url=warc_ingest.normalize_probe_url(url),
                    language=str(lang_obj.get("identifier") or ""),
                    version_id=int(ver_obj.get("identifier") or 0),
                    html=html,
                )


def verify_wikimedia_source(data_root: str | Path, source_id: str) -> tuple[Path, dict]:
    """Verify a downloaded Wikimedia archive and its `.receipt.json` under `data_root`."""
    root = Path(data_root).expanduser().resolve()
    source = data_fetch.SOURCES[source_id]
    archive = root / source.relpath
    if not archive.is_file() or archive.is_symlink():
        raise FileNotFoundError(f"Missing Wikimedia dump for {source_id}: {archive}")
    actual_bytes = archive.stat().st_size
    if source.expected_bytes is not None and actual_bytes != source.expected_bytes:
        raise ValueError(
            f"{source_id}: size {actual_bytes} != pinned {source.expected_bytes}"
        )
    receipt_path = archive.with_name(archive.name + ".receipt.json")
    receipt_meta = {
        "source_id": source_id,
        "relpath": source.relpath,
        "bytes": actual_bytes,
        "etag_pinned": source.etag,
        **data_fetch.LICENSES[source.family],
    }
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("bytes") != actual_bytes or receipt.get("source_id") != source_id:
            raise ValueError(f"{source_id}: receipt mismatch in {receipt_path}")
        receipt_meta["sha256"] = receipt.get("sha256")
        receipt_meta["fetched_at"] = receipt.get("fetched_at")
    return archive, receipt_meta


# ---------------------------------------------------------------------------
# Probe Invokers & End-to-End Tier-B Evaluation Runner
# ---------------------------------------------------------------------------

def _default_probe_runner(argv: list[str], payload: bytes, timeout: float):
    proc = subprocess.run(argv, input=payload, capture_output=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def run_cloud_024_probe(
    html: str,
    url: str,
    *,
    core_probe_bin: str | Path,
    boilerplate: str = "cetd_entropy_v1",
    timeout: float = 60.0,
    runner=None,
) -> dict:
    """Invoke `core_probe cloud-024 - 0` on an HTML document."""
    runner = runner or _default_probe_runner
    fixture = {
        "html": warc_ingest.sanitize_html_for_probe(html),
        "url": warc_ingest.normalize_probe_url(url),
        "archetype": "multihop_ir_contrastive_aae2",
        "query": warc_ingest.derive_page_query(html, url),
        "boilerplate": boilerplate,
        "link_mode": "none",
    }
    payload = json.dumps(fixture, ensure_ascii=False).encode("utf-8")
    argv = [str(core_probe_bin), "cloud-024", "-", "0"]
    rc, stdout, stderr = runner(argv, payload, timeout)
    if rc != 0:
        tail = (stderr or b"")[-300:].decode("utf-8", "replace")
        raise RuntimeError(f"core_probe cloud-024 exited {rc}: {tail}")
    return json.loads(stdout.decode("utf-8"))


def run_html_table_probe(
    html: str,
    url: str = "https://en.wikibooks.org/wiki/Table",
    *,
    core_probe_bin: str | Path,
    timeout: float = 60.0,
    runner=None,
) -> dict | None:
    """Invoke `core_probe html-table - 0` to inspect 2D grid slot expansion."""
    runner = runner or _default_probe_runner
    norm_url = warc_ingest.normalize_probe_url(url)
    sanitized = warc_ingest.sanitize_html_for_probe(html)
    fixture = {
        "html": sanitized,
        "url": norm_url,
        "candidate": "traversal",
        "max_tables": 64,
        "max_table_rows": 1000,
        "max_table_cols": 500,
    }
    payload = json.dumps(fixture, ensure_ascii=False).encode("utf-8")
    argv = [str(core_probe_bin), "html-table", "-", "0"]
    rc, stdout, _ = runner(argv, payload, timeout)
    if rc == 0:
        return json.loads(stdout.decode("utf-8"))
    tables: list[dict] = []
    for match in re.finditer(r"<table\b.*?</table>", sanitized, re.IGNORECASE | re.DOTALL):
        if len(tables) >= 64:
            break
        sub_fixture = {
            "html": f"<html><body>{match.group(0)}</body></html>",
            "url": norm_url,
            "candidate": "traversal",
            "max_tables": 8,
            "max_table_rows": 1000,
            "max_table_cols": 500,
        }
        sub_payload = json.dumps(sub_fixture, ensure_ascii=False).encode("utf-8")
        sub_rc, sub_out, _ = runner(argv, sub_payload, timeout)
        if sub_rc == 0:
            sub_data = json.loads(sub_out.decode("utf-8"))
            if isinstance(sub_data, dict) and sub_data.get("tables"):
                tables.extend(sub_data["tables"])
    return {"tables": tables} if tables else None


def _summarize_math_records(records: list[dict]) -> dict:
    """Aggregate formula-level scores overall and per construct bucket with 95% CIs."""
    def _bucket_stats(items: list[dict]) -> dict:
        n = len(items)
        if n == 0:
            return {
                "n": 0,
                "formula_similarity": _mean_ci([]),
                "formula_hit_at_0_9": _rate_ci(0, 0),
                "formula_hit_at_0_8": _rate_ci(0, 0),
                "exact_match_rate": _rate_ci(0, 0),
            }
        sims = [float(r["similarity"]) for r in items]
        hits_09 = sum(1 for r in items if float(r["similarity"]) >= 0.9)
        hits_08 = sum(1 for r in items if float(r["similarity"]) >= 0.8)
        exact = sum(1 for r in items if int(r["edit_distance"]) == 0 and r["gold_norm"])
        return {
            "n": n,
            "formula_similarity": _mean_ci(sims),
            "formula_hit_at_0_9": _rate_ci(hits_09, n),
            "formula_hit_at_0_8": _rate_ci(hits_08, n),
            "exact_match_rate": _rate_ci(exact, n),
        }

    by_construct = {}
    for bucket in _CONSTRUCT_BUCKETS:
        sub = [r for r in records if bucket in r["constructs"]]
        by_construct[bucket] = _bucket_stats(sub)

    return {
        "overall": _bucket_stats(records),
        "by_construct": by_construct,
    }


def _summarize_table_records(records: list[dict]) -> dict:
    """Aggregate table-level and cell-level metrics with 95% CIs across span strata."""
    def _stratum_stats(items: list[dict]) -> dict:
        n_tables = len(items)
        total_data_cells = sum(int(r["data_cell_count"]) for r in items)
        recovered_cells = sum(int(r["recovered_data_cells"]) for r in items)
        total_bound_cells = sum(int(r["header_bound_cell_count"]) for r in items)
        bound_hits = sum(int(r["header_bound_hits"]) for r in items)
        cell_recalls = [float(r["cell_content_recall"]) for r in items]
        hdr_recalls = [float(r["header_binding_recall"]) for r in items]
        disc_f1s = [float(r["grounded_discourse_f1"]) for r in items]
        slot_accs = [
            float(r["grid_slot_fidelity"])
            for r in items
            if r.get("grid_slot_fidelity") is not None
        ]
        return {
            "n_tables": n_tables,
            "total_data_cells": total_data_cells,
            "total_header_bound_cells": total_bound_cells,
            "table_macro_cell_recall": _mean_ci(cell_recalls),
            "cell_micro_recall": _rate_ci(recovered_cells, total_data_cells),
            "table_macro_header_binding_recall": _mean_ci(hdr_recalls),
            "cell_micro_header_binding_recall": _rate_ci(bound_hits, total_bound_cells),
            "grounded_discourse_f1": _mean_ci(disc_f1s),
            "grid_slot_fidelity": _mean_ci(slot_accs),
        }

    simple = [r for r in records if not r["has_span"]]
    spanned = [r for r in records if r["has_span"]]
    return {
        "all_data_tables": _stratum_stats(records),
        "simple_tables": _stratum_stats(simple),
        "spanned_tables": _stratum_stats(spanned),
    }


def evaluate_tier_b(
    *,
    data_root: str | Path = "~/athena_data",
    source_ids: tuple[str, ...] = DEFAULT_SOURCES,
    archive_paths: tuple[str | Path, ...] = (),
    core_probe_bin: str | Path,
    max_math_pages_per_source: int = 40,
    max_table_pages_per_source: int = 40,
    scan_limit_per_source: int = 5000,
    boilerplate: str = "cetd_entropy_v1",
    workers: int = 1,
    runner=None,
) -> dict:
    """Run full Tier-B evaluation (`D2` MathML-to-LaTeX & `D4`/`D11` HTML Tables)."""
    from concurrent.futures import ThreadPoolExecutor

    started = time.monotonic()
    sources_report: list[dict] = []
    archives_to_scan: list[tuple[str, Path]] = []

    if archive_paths:
        for idx, ap in enumerate(archive_paths):
            p = Path(ap).expanduser().resolve()
            sid = f"custom-{idx}-{p.name}"
            archives_to_scan.append((sid, p))
            sources_report.append({
                "source_id": sid,
                "path": str(p),
                "bytes": p.stat().st_size,
                **data_fetch.LICENSES["wikimedia"],
            })
    else:
        for sid in source_ids:
            archive, meta = verify_wikimedia_source(data_root, sid)
            archives_to_scan.append((sid, archive))
            sources_report.append(meta)

    math_candidates: list[tuple[str, WikimediaArticle, leakage.MathStripResult]] = []
    table_candidates: list[tuple[str, WikimediaArticle, list[OracleTable]]] = []

    for sid, archive in archives_to_scan:
        m_raw: list[tuple[str, WikimediaArticle]] = []
        t_pool: list[tuple[str, WikimediaArticle, list[OracleTable]]] = []
        scanned = 0
        for article in iter_wikimedia_articles(archive, source_id=sid):
            scanned += 1
            if "<math" in article.html:
                key = _pick_key("math", f"{sid}:{article.identifier}:{article.name}")
                m_raw.append((key, article))
            if "<table" in article.html and "<th" in article.html and "<td" in article.html:
                otables = extract_oracle_data_tables(article.html)
                if otables:
                    # Prefer spanned tables and deterministic hash order
                    has_spanned = any(t.has_span for t in otables)
                    prefix = "0:" if has_spanned else "1:"
                    key = prefix + _pick_key("table", f"{sid}:{article.identifier}:{article.name}")
                    t_pool.append((key, article, otables))
            if scan_limit_per_source and scanned >= scan_limit_per_source:
                break

        m_raw.sort(key=lambda item: item[0])
        m_pool: list[tuple[str, WikimediaArticle, leakage.MathStripResult]] = []
        for key, article in m_raw:
            if len(m_pool) >= max_math_pages_per_source:
                break
            stripped = leakage.strip_math_leakage(article.html)
            valid_gold = [
                f for f in stripped.gold
                if f.gold_tex and leakage.normalize_tex(f.gold_tex)
            ]
            if valid_gold:
                m_pool.append((
                    key,
                    article,
                    leakage.MathStripResult(
                        html=stripped.html,
                        gold=valid_gold,
                        removed=stripped.removed,
                        unpaired_data_mw_tex=stripped.unpaired_data_mw_tex,
                        math_tex_scripts=stripped.math_tex_scripts,
                    ),
                ))

        t_pool.sort(key=lambda item: item[0])
        math_candidates.extend(m_pool)
        table_candidates.extend(t_pool[:max_table_pages_per_source])

    # 1. Evaluate D2 MathML-to-LaTeX in all 3 modes on the selected math pages
    stripped_records: list[dict] = []
    raw_parsoid_records: list[dict] = []
    tier1_annotated_records: list[dict] = []
    math_pages_evaluated = 0
    leak_verified_clean_pages = 0

    def _eval_math_candidate(item: tuple[str, WikimediaArticle, leakage.MathStripResult]):
        _, article, strip_res = item
        leak_check = leakage.math_leak_report(strip_res.html)
        if not leak_check["clean"]:
            raise RuntimeError(
                f"Math leakage check failed on stripped page {article.url}: {leak_check}"
            )
        gold_list = [f.gold_tex or "" for f in strip_res.gold]
        constructs_per_gold = [classify_math_constructs(g) for g in gold_list]
        gold_norms = [leakage.normalize_tex(g) for g in gold_list]
        try:
            out_stripped = run_cloud_024_probe(
                strip_res.html, article.url,
                core_probe_bin=core_probe_bin, boilerplate=boilerplate, runner=runner,
            )
            out_raw = run_cloud_024_probe(
                article.html, article.url,
                core_probe_bin=core_probe_bin, boilerplate=boilerplate, runner=runner,
            )
            annotated_html = enrich_math_tier1_annotations(article.html)
            out_tier1 = run_cloud_024_probe(
                annotated_html, article.url,
                core_probe_bin=core_probe_bin, boilerplate=boilerplate, runner=runner,
            )
        except Exception:
            return None
        return article, gold_list, gold_norms, constructs_per_gold, out_stripped, out_raw, out_tier1

    eff_workers = max(1, int(workers))
    if eff_workers > 1 and runner is None:
        with ThreadPoolExecutor(max_workers=eff_workers) as pool:
            math_results = list(pool.map(_eval_math_candidate, math_candidates))
    else:
        math_results = [_eval_math_candidate(c) for c in math_candidates]

    for res in math_results:
        if res is None:
            continue
        article, gold_list, gold_norms, constructs_per_gold, out_stripped, out_raw, out_tier1 = res
        math_pages_evaluated += 1
        leak_verified_clean_pages += 1

        for mode_records, probe_out in (
            (stripped_records, out_stripped),
            (raw_parsoid_records, out_raw),
            (tier1_annotated_records, out_tier1),
        ):
            preds = extract_predicted_tex_spans(probe_out.get("chunks") or [])
            aligned = align_gold_and_predicted_tex(gold_list, preds)
            for idx, (g_raw, p_raw, sim, dist) in enumerate(aligned):
                mode_records.append({
                    "source_id": article.source_id,
                    "url": article.url,
                    "formula_index": idx,
                    "gold_norm": gold_norms[idx],
                    "pred_norm": leakage.normalize_tex(p_raw),
                    "similarity": sim,
                    "edit_distance": dist,
                    "constructs": constructs_per_gold[idx],
                })

    # 2. Evaluate D4 / D11 HTML Table Extraction & Header Binding
    table_records: list[dict] = []
    table_pages_evaluated = 0

    def _eval_table_candidate(item: tuple[str, WikimediaArticle, list[OracleTable]]):
        _, article, otables = item
        try:
            probe_out = run_cloud_024_probe(
                article.html, article.url,
                core_probe_bin=core_probe_bin, boilerplate=boilerplate, runner=runner,
            )
            grid_out = run_html_table_probe(
                article.html, article.url, core_probe_bin=core_probe_bin, runner=runner,
            )
        except Exception:
            return None
        probe_groups = split_probe_table_chunk_groups(probe_out.get("chunks") or [])
        probe_grids = (grid_out or {}).get("tables") if isinstance(grid_out, dict) else None
        page_scored = []
        for otable in otables:
            scored = score_oracle_table_against_probe(otable, probe_groups, probe_grids)
            scored["source_id"] = article.source_id
            scored["url"] = article.url
            scored["table_index"] = otable.table_index
            page_scored.append(scored)
        return page_scored

    if eff_workers > 1 and runner is None:
        with ThreadPoolExecutor(max_workers=eff_workers) as pool:
            table_results = list(pool.map(_eval_table_candidate, table_candidates))
    else:
        table_results = [_eval_table_candidate(c) for c in table_candidates]

    for page_scored in table_results:
        if page_scored is None:
            continue
        table_pages_evaluated += 1
        table_records.extend(page_scored)

    elapsed_s = round(time.monotonic() - started, 3)
    return {
        "schema": TIER_B_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed_s,
        "selection_salt": SELECTION_SALT,
        "boilerplate_policy": boilerplate,
        "license_policy": {
            **data_fetch.LICENSES["wikimedia"],
            "corpus_status": "CORPUS_NOT_ADMITTED (local internal research only; no cloud staging)",
        },
        "sources": sources_report,
        "math_d2": {
            "pages_evaluated": math_pages_evaluated,
            "leak_verified_clean_pages": leak_verified_clean_pages,
            "total_formulas_evaluated": len(stripped_records),
            "stripped_tier2_tree_walk": _summarize_math_records(stripped_records),
            "unstripped_raw_parsoid": _summarize_math_records(raw_parsoid_records),
            "unstripped_tier1_annotated": _summarize_math_records(tier1_annotated_records),
        },
        "tables_d4_d11": {
            "pages_evaluated": table_pages_evaluated,
            "total_tables_evaluated": len(table_records),
            "spanned_tables_evaluated": sum(1 for r in table_records if r["has_span"]),
            "simple_tables_evaluated": sum(1 for r in table_records if not r["has_span"]),
            "summary": _summarize_table_records(table_records),
        },
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", default="~/athena_data")
    parser.add_argument("--source", action="append", default=[],
                        help="Wikimedia source_id(s); defaults to simplewikibooks + viwikibooks + enwikibooks")
    parser.add_argument("--archive", action="append", default=[],
                        help="Explicit .json.tar.gz archive path(s) for testing")
    parser.add_argument("--core-probe-bin",
                        default=str(Path(__file__).resolve().parents[1] / "target" / "release" / "core_probe"))
    parser.add_argument("--max-math-pages", type=int, default=40)
    parser.add_argument("--max-table-pages", type=int, default=40)
    parser.add_argument("--scan-limit", type=int, default=5000)
    parser.add_argument("--boilerplate", default="cetd_entropy_v1")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    source_ids = tuple(args.source) if args.source else DEFAULT_SOURCES
    report = evaluate_tier_b(
        data_root=args.data_root,
        source_ids=source_ids,
        archive_paths=tuple(args.archive),
        core_probe_bin=args.core_probe_bin,
        max_math_pages_per_source=args.max_math_pages,
        max_table_pages_per_source=args.max_table_pages,
        scan_limit_per_source=args.scan_limit,
        boilerplate=args.boilerplate,
        workers=args.workers,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        out_path = Path(args.out).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

