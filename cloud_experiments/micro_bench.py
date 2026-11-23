#!/usr/bin/env python3
"""Athena-Bench v2: Layer 1 (Micro-Operational Atomic Oracles), Layer 2 (Metamorphic Suite), and Layer 4 (Systems, WAF, Bandit & Cache Heap Audit).

Executes against the unmodified Athena Rust engine via `core_probe evaluate-v2` to establish
the Honest Pre-Fix Baseline across S1..S10.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import glob
import gzip
import html as html_lib
import io
import json
import math
import os
import re
import subprocess
import sys
import tarfile
import time
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from dataclasses import dataclass
from typing import Any

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from cloud_experiments import leakage, tier_a, tier_b, wceb, wceb_runner
from warc_ingest import (
    decode_content,
    iter_gzip_members,
    parse_http_response,
    parse_warc_record,
    sanitize_html_for_probe,
    transcode_to_utf8,
)


@dataclass
class WarcHttpRecord:
    member_offset: int
    target_uri: str
    http_status: int
    http_headers: dict[str, str]
    body: bytes


def decode_html_bytes(body: bytes, http_headers: dict[str, str]) -> tuple[str, str, str, int]:
    ct = http_headers.get("content-type") or http_headers.get("Content-Type") or ""
    tr = transcode_to_utf8(body, ct)
    return tr.text, tr.codec, tr.source, tr.decode_errors


def iter_warc_records(path: str, start_offset: int = 0):
    with open(path, "rb") as stream:
        for member in iter_gzip_members(stream, start_offset=start_offset):
            try:
                rec = parse_warc_record(member)
            except Exception:
                continue
            if rec.type != "response":
                continue
            resp = parse_http_response(rec.block)
            if resp.status is None:
                continue
            payload, _note = decode_content(resp)
            yield WarcHttpRecord(
                member_offset=member.offset,
                target_uri=rec.headers.get("warc-target-uri", ""),
                http_status=resp.status,
                http_headers=resp.headers,
                body=payload,
            )


# ============================================================================
# Persistent Streaming Client for `core_probe evaluate-v2 - 0`
# ============================================================================


class EvaluateV2Client:
    def __init__(self, probe_bin: str) -> None:
        self.probe_bin = probe_bin
        self._proc: subprocess.Popen[str] | None = None
        self._start()

    def _start(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1)
            except Exception:
                pass
        self._proc = subprocess.Popen(
            [self.probe_bin, "evaluate-v2", "-", "0"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def call(self, req: dict[str, Any], timeout_s: float = 15.0) -> dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            self._start()
        assert self._proc is not None and self._proc.stdin is not None and self._proc.stdout is not None
        line = json.dumps(req, ensure_ascii=False) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
            out_line = self._proc.stdout.readline()
            if not out_line:
                rc = self._proc.poll()
                self._start()
                return {"status": "CRASH", "returncode": rc}
            return json.loads(out_line)
        except Exception as exc:
            self._start()
            return {"status": "EXCEPTION", "error": str(exc)}

    def call_isolated(self, req: dict[str, Any], timeout_s: float = 5.0) -> dict[str, Any]:
        """Run a single request in an isolated subprocess with a hard wall-clock timeout."""
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [self.probe_bin, "evaluate-v2", "-", "0"],
                input=(json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_s,
                check=False,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            if proc.returncode != 0:
                return {
                    "status": "CRASH",
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.decode("utf-8", errors="replace")[:500],
                    "elapsed_ms": round(elapsed_ms, 2),
                }
            out_txt = proc.stdout.decode("utf-8", errors="replace").strip()
            if not out_txt:
                return {"status": "EMPTY_STDOUT", "elapsed_ms": round(elapsed_ms, 2)}
            res = json.loads(out_txt.splitlines()[0])
            res["elapsed_ms"] = round(elapsed_ms, 2)
            return res
        except subprocess.TimeoutExpired:
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "status": "TIMEOUT",
                "timeout_s": timeout_s,
                "elapsed_ms": round(elapsed_ms, 2),
            }

    def close(self) -> None:
        if self._proc is not None:
            try:
                if self._proc.stdin:
                    self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=1)
            except Exception:
                pass
            self._proc = None


# ============================================================================
# L1.3 Normalized Math Operator-Tree (AST) Evaluator
# ============================================================================

_MATH_TOKEN_RE = re.compile(r"\\[a-zA-Z]+|\\.|[0-9]+(?:\.[0-9]+)?|[a-zA-Z]|[^\s]")

_CANONICAL_CMD_MAP = {
    r"\tfrac": r"\frac",
    r"\dfrac": r"\frac",
    r"\cfrac": r"\frac",
    r"\dbinom": r"\binom",
    r"\tbinom": r"\binom",
    r"\leqslant": r"\le",
    r"\geqslant": r"\ge",
    r"\leq": r"\le",
    r"\geq": r"\ge",
    r"\neq": r"\ne",
    r"\rightarrow": r"\to",
    r"\longrightarrow": r"\to",
    r"\Longrightarrow": r"\Rightarrow",
    r"\Longleftrightarrow": r"\Leftrightarrow",
    r"\varepsilon": r"\epsilon",
    r"\varphi": r"\phi",
    r"\dots": r"\ldots",
    r"\dotsb": r"\cdots",
    r"\dotsc": r"\ldots",
}

_IGNORED_NOISE_CMDS = {
    r"\displaystyle",
    r"\textstyle",
    r"\scriptstyle",
    r"\scriptscriptstyle",
    r"\left",
    r"\right",
    r"\bigl",
    r"\bigr",
    r"\Bigl",
    r"\Bigr",
    r"\biggl",
    r"\biggr",
    r"\Biggl",
    r"\Biggr",
    r"\!",
    r"\,",
    r"\:",
    r"\;",
    r"\quad",
    r"\qquad",
    r"\limits",
    r"\nolimits",
}

_FONT_WRAPPER_CMDS = {
    r"\mathrm",
    r"\mathbf",
    r"\mathit",
    r"\mathsf",
    r"\mathtt",
    r"\mathcal",
    r"\mathbb",
    r"\mathfrak",
    r"\boldsymbol",
    r"\bm",
    r"\text",
    r"\textrm",
    r"\textbf",
    r"\textit",
    r"\operatorname",
}

_ACCENT_CMDS = {
    r"\hat",
    r"\widehat",
    r"\bar",
    r"\overline",
    r"\vec",
    r"\overrightarrow",
    r"\tilde",
    r"\widetilde",
    r"\dot",
    r"\ddot",
    r"\underline",
}

_ACCENT_CANON = {
    r"\widehat": r"\hat",
    r"\overline": r"\bar",
    r"\overrightarrow": r"\vec",
    r"\widetilde": r"\tilde",
}


def tokenize_latex_math(tex: str) -> list[str]:
    s = tex.strip()
    while s.startswith("$") and s.endswith("$") and len(s) >= 2:
        s = s[1:-1].strip()
    if s.startswith(r"\(") and s.endswith(r"\)"):
        s = s[2:-2].strip()
    if s.startswith(r"\[") and s.endswith(r"\]"):
        s = s[2:-2].strip()
    raw_tokens = _MATH_TOKEN_RE.findall(s)
    out: list[str] = []
    for tok in raw_tokens:
        if tok in _IGNORED_NOISE_CMDS:
            continue
        out.append(_CANONICAL_CMD_MAP.get(tok, tok))
    return out


def parse_math_ast(tex: str) -> Any:
    """Parse LaTeX into a canonical nested tuple AST preserving non-commutative argument order."""
    tokens = tokenize_latex_math(tex)
    pos = 0
    n = len(tokens)

    def peek() -> str | None:
        return tokens[pos] if pos < n else None

    def consume() -> str:
        nonlocal pos
        t = tokens[pos]
        pos += 1
        return t

    def parse_atom() -> Any:
        nonlocal pos
        t = peek()
        if t is None:
            return ("Empty",)
        if t == "{":
            consume()
            inner = parse_expr(stop_tokens={"}"})
            if peek() == "}":
                consume()
            return inner
        if t in ("(", "["):
            open_d = consume()
            close_d = ")" if open_d == "(" else "]"
            inner = parse_expr(stop_tokens={close_d})
            if peek() == close_d:
                consume()
            return ("Delim", open_d, inner, close_d)
        if t in (r"\frac", r"\binom", r"\overset", r"\underset"):
            cmd = consume()
            arg1 = parse_atom()
            arg2 = parse_atom()
            return ("BinCmd", cmd, arg1, arg2)
        if t == r"\sqrt":
            consume()
            if peek() == "[":
                consume()
                idx = parse_expr(stop_tokens={"]"})
                if peek() == "]":
                    consume()
                rad = parse_atom()
                return ("Root", idx, rad)
            rad = parse_atom()
            return ("Sqrt", rad)
        if t in _ACCENT_CMDS:
            cmd = _ACCENT_CANON.get(consume(), t)
            arg = parse_atom()
            return ("Accent", cmd, arg)
        if t in _FONT_WRAPPER_CMDS:
            cmd = consume()
            arg = parse_atom()
            if cmd == r"\operatorname":
                # Canonicalize \operatorname{sin} -> (\sin)
                if isinstance(arg, tuple) and arg[0] == "Seq":
                    letters = "".join(x[1] for x in arg[1:] if isinstance(x, tuple) and x[0] == "Sym")
                    if letters:
                        return ("Sym", "\\" + letters)
                elif isinstance(arg, tuple) and arg[0] == "Sym":
                    return ("Sym", "\\" + arg[1].lstrip("\\"))
            if cmd in (r"\mathrm", r"\text", r"\textrm"):
                return arg
            return ("Font", cmd, arg)
        if t == r"\begin":
            consume()
            env_node = parse_atom()
            env_name = "matrix"
            if isinstance(env_node, tuple):
                flat = _flatten_ast_symbols(env_node)
                if flat:
                    env_name = "".join(flat)
            body = parse_matrix_body()
            return ("Env", env_name, body)
        consume()
        return ("Sym", t)

    def parse_matrix_body() -> tuple[Any, ...]:
        rows: list[tuple[Any, ...]] = []
        cur_row: list[Any] = []
        cur_cell: list[Any] = []
        while pos < n:
            if peek() == r"\end":
                consume()
                _ = parse_atom()
                break
            if peek() == "&":
                consume()
                cur_row.append(_simplify_seq(cur_cell))
                cur_cell = []
            elif peek() == r"\\":
                consume()
                cur_row.append(_simplify_seq(cur_cell))
                rows.append(tuple(cur_row))
                cur_row = []
                cur_cell = []
            else:
                cur_cell.append(parse_scripted())
        if cur_cell or cur_row:
            cur_row.append(_simplify_seq(cur_cell))
            rows.append(tuple(cur_row))
        return tuple(rows)

    def parse_scripted() -> Any:
        base = parse_atom()
        sub_val = None
        sup_val = None
        while peek() in ("_", "^"):
            op = consume()
            arg = parse_atom()
            if op == "_":
                sub_val = arg
            else:
                sup_val = arg
        if sub_val is not None and sup_val is not None:
            return ("SubSup", base, sub_val, sup_val)
        if sub_val is not None:
            return ("Sub", base, sub_val)
        if sup_val is not None:
            return ("Sup", base, sup_val)
        return base

    def parse_expr(stop_tokens: set[str] | None = None) -> Any:
        items: list[Any] = []
        while pos < n:
            t = peek()
            if t is None or (stop_tokens and t in stop_tokens) or t == r"\end":
                break
            items.append(parse_scripted())
        return _simplify_seq(items)

    def _simplify_seq(items: list[Any]) -> Any:
        items = [it for it in items if it != ("Empty",)]
        if not items:
            return ("Empty",)
        if len(items) == 1:
            return items[0]
        # Fold binary relations/operators into ordered BinOp nodes so a - b != b - a
        return ("Seq", *items)

    return parse_expr()


def _flatten_ast_symbols(node: Any) -> list[str]:
    if not isinstance(node, tuple):
        return [str(node)]
    tag = node[0]
    if tag == "Sym":
        return [str(node[1])]
    out: list[str] = []
    for child in node[1:]:
        out.extend(_flatten_ast_symbols(child))
    return out


def ast_preorder_nodes(node: Any) -> list[str]:
    """Serialize AST into ordered structural path tokens sensitive to argument position."""
    out: list[str] = []

    def walk(curr: Any, role_prefix: str) -> None:
        if not isinstance(curr, tuple) or not curr:
            out.append(f"{role_prefix}:{curr}")
            return
        kind = curr[0]
        if kind == "Sym":
            out.append(f"{role_prefix}:Sym({curr[1]})")
        elif kind == "BinCmd":
            cmd, a1, a2 = curr[1], curr[2], curr[3]
            out.append(f"{role_prefix}:BinCmd({cmd})")
            walk(a1, f"{cmd}.arg1")
            walk(a2, f"{cmd}.arg2")
        elif kind == "SubSup":
            out.append(f"{role_prefix}:SubSup")
            walk(curr[1], "base")
            walk(curr[2], "sub")
            walk(curr[3], "sup")
        elif kind == "Sub":
            out.append(f"{role_prefix}:Sub")
            walk(curr[1], "base")
            walk(curr[2], "sub")
        elif kind == "Sup":
            out.append(f"{role_prefix}:Sup")
            walk(curr[1], "base")
            walk(curr[2], "sup")
        elif kind == "Sqrt":
            out.append(f"{role_prefix}:Sqrt")
            walk(curr[1], "rad")
        elif kind == "Root":
            out.append(f"{role_prefix}:Root")
            walk(curr[1], "idx")
            walk(curr[2], "rad")
        elif kind == "Seq":
            out.append(f"{role_prefix}:Seq({len(curr) - 1})")
            for idx, ch in enumerate(curr[1:]):
                walk(ch, f"seq[{idx}]")
        else:
            out.append(f"{role_prefix}:{kind}")
            for idx, ch in enumerate(curr[1:]):
                walk(ch, f"{kind}[{idx}]")

    walk(node, "root")
    return out


def math_ast_similarity(gold_tex: str, cand_tex: str) -> tuple[float, bool]:
    ast_g = parse_math_ast(gold_tex)
    ast_c = parse_math_ast(cand_tex)
    if ast_g == ast_c:
        return 1.0, True
    seq_g = ast_preorder_nodes(ast_g)
    seq_c = ast_preorder_nodes(ast_c)
    if not seq_g and not seq_c:
        return 1.0, True
    if not seq_g or not seq_c:
        return 0.0, False
    sim = difflib.SequenceMatcher(None, seq_g, seq_c).ratio()
    return sim, False


def classify_formula_category(tex: str, raw_html: str = "") -> str:
    t = tex.strip()
    h = raw_html.lower()
    if "katex" in h:
        return "katex_html_trees"
    if "<menclose" in h or any(k in t for k in (r"\boxed", r"\cancel", r"\bcancel", r"\xcancel", r"\enclose")):
        return "menclose_enclosures"
    if (
        r"\begin{" in t
        or r"\matrix" in t
        or r"\pmatrix" in t
        or r"\bmatrix" in t
        or r"\cases" in t
        or "<mtable" in h
    ):
        return "matrices_cases_arrays"
    if any(op in t for op in (r"\sum", r"\prod", r"\int", r"\oint", r"\bigcup", r"\bigcap", r"\lim")):
        return "summations_integrals_limits"
    if r"\sqrt" in t or "<msqrt" in h or "<mroot" in h:
        return "radicals_roots"
    if any(f in t for f in (r"\frac", r"\tfrac", r"\dfrac", r"\cfrac", r"\binom", r"\choose")) or "<mfrac" in h:
        return "fractions_binomials"
    if (
        any(
            fv in t
            for fv in (
                r"\mathbb",
                r"\mathcal",
                r"\mathbf",
                r"\mathrm",
                r"\mathsf",
                r"\mathfrak",
                r"\mathscr",
                r"\boldsymbol",
            )
        )
        or any(
            mv in h
            for mv in (
                'mathvariant="bold"',
                'mathvariant="double-struck"',
                'mathvariant="script"',
                'mathvariant="fraktur"',
                'mathvariant="sans-serif"',
                'mathvariant="monospace"',
            )
        )
    ):
        return "font_variants_mathvariant"
    if any(a in t for a in _ACCENT_CMDS):
        return "accents_hats_bars_vectors"
    if "<mmultiscripts" in h or "^" in t or "_" in t or "<msup" in h or "<msub" in h or "<msubsup" in h:
        return "superscripts_subscripts"
    if any(
        g in t
        for g in (
            r"\sin",
            r"\cos",
            r"\tan",
            r"\cot",
            r"\sec",
            r"\csc",
            r"\arcsin",
            r"\arccos",
            r"\arctan",
            r"\sinh",
            r"\cosh",
            r"\tanh",
            r"\log",
            r"\ln",
            r"\exp",
            r"\det",
            r"\dim",
            r"\ker",
            r"\hom",
            r"\deg",
            r"\gcd",
            r"\max",
            r"\min",
            r"\sup",
            r"\inf",
            "−",
            r"\times",
            r"\div",
            r"\pm",
            r"\mp",
            r"\cdot",
            r"\alpha",
            r"\beta",
            r"\gamma",
            r"\delta",
            r"\theta",
            r"\lambda",
            r"\pi",
            r"\sigma",
            r"\omega",
            r"\le",
            r"\ge",
            r"\ne",
            r"\approx",
            r"\equiv",
            r"\in",
            r"\subset",
            r"\to",
            r"\Rightarrow",
        )
    ):
        return "named_functions_unicode_ops"
    return "simple_inline"



# ============================================================================
# L1.4 Independent WHATWG §4.9.12 2D Table Layout State Machine Oracle
# ============================================================================


_CHROME_TABLE_CLASSES = frozenset(
    {
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
    }
)


class WhatwgTableOracleParser(HTMLParser):
    """Independent reference implementation of WHATWG HTML §4.9.12 table layout,
    including nested tables, unlimited rows, rowspan=0, multi-row thead paths, and row stubs.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.completed_tables: list[dict[str, Any]] = []
        self._table_stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "table":
            classes = frozenset(attr_map.get("class", "").lower().split())
            role = attr_map.get("role", "").strip().lower()
            self._table_stack.append(
                {
                    "group": 1,
                    "group_type": "tbody",
                    "rows": [],
                    "in_row": False,
                    "cur_row_cells": [],
                    "in_cell": False,
                    "cur_cell": None,
                    "depth": len(self._table_stack),
                    "classes": classes,
                    "role": role,
                }
            )
            return
        if not self._table_stack:
            return
        cur = self._table_stack[-1]
        if t in ("thead", "tbody", "tfoot"):
            cur["group_type"] = t
            cur["group"] = 0 if t == "thead" else (2 if t == "tfoot" else 1)
        elif t == "tr":
            cur["in_row"] = True
            cur["cur_row_cells"] = []
        elif t in ("th", "td") and cur["in_row"]:
            try:
                rs = int(attr_map.get("rowspan", "1").strip() or "1")
                if rs < 0:
                    rs = 1
            except ValueError:
                rs = 1
            try:
                cs = int(attr_map.get("colspan", "1").strip() or "1")
                if cs <= 0:
                    cs = 1
            except ValueError:
                cs = 1
            scope = attr_map.get("scope", "").strip().lower()
            cur["in_cell"] = True
            cur["cur_cell"] = {
                "is_header": (t == "th"),
                "scope": scope,
                "rowspan": rs,
                "colspan": min(cs, 1000),
                "buf": [],
            }
        elif t == "br" and cur["in_cell"] and cur["cur_cell"] is not None:
            cur["cur_cell"]["buf"].append(" ")

    def handle_data(self, data: str) -> None:
        if not self._table_stack:
            return
        cur = self._table_stack[-1]
        if cur["in_cell"] and cur["cur_cell"] is not None:
            cur["cur_cell"]["buf"].append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if not self._table_stack:
            return
        cur = self._table_stack[-1]
        if t in ("th", "td") and cur["in_cell"] and cur["cur_cell"] is not None:
            cell = cur["cur_cell"]
            txt = " ".join("".join(cell["buf"]).split())
            cur["cur_row_cells"].append(
                {
                    "text": txt,
                    "is_header": cell["is_header"],
                    "scope": cell["scope"],
                    "rowspan": cell["rowspan"],
                    "colspan": cell["colspan"],
                }
            )
            cur["in_cell"] = False
            cur["cur_cell"] = None
        elif t == "tr" and cur["in_row"]:
            if cur["cur_row_cells"]:
                cur["rows"].append(
                    {
                        "group": cur["group"],
                        "group_type": cur["group_type"],
                        "cells": cur["cur_row_cells"],
                    }
                )
            cur["in_row"] = False
            cur["cur_row_cells"] = []
        elif t in ("thead", "tbody", "tfoot"):
            cur["group"] = 1
            cur["group_type"] = "tbody"
        elif t == "table":
            finished = self._table_stack.pop()
            if finished["rows"]:
                tbl = lay_out_whatwg_table(finished["rows"], finished["depth"])
                tbl["classes"] = finished["classes"]
                tbl["role"] = finished["role"]
                self.completed_tables.append(tbl)


def lay_out_whatwg_table(rows: list[dict[str, Any]], nesting_depth: int = 0) -> dict[str, Any]:
    """Expand rows into a 2D slot matrix per WHATWG §4.9.12 without any 512-row truncation."""
    norm_rows = sorted(rows, key=lambda r: r["group"])
    num_rows = len(norm_rows)
    slots: list[list[int | None]] = [[] for _ in range(num_rows)]
    cells: list[dict[str, Any]] = []
    width = 0
    group_end = 0

    for r_idx, row in enumerate(norm_rows):
        if r_idx == group_end:
            group_end = num_rows
            for k in range(r_idx + 1, num_rows):
                if norm_rows[k]["group"] != row["group"]:
                    group_end = k
                    break
        c_idx = 0
        for cell in row["cells"]:
            while c_idx < len(slots[r_idx]) and slots[r_idx][c_idx] is not None:
                c_idx += 1
            dc = max(1, min(cell["colspan"], 1000))
            group_rem = max(1, group_end - r_idx)
            dr = group_rem if cell["rowspan"] == 0 else max(1, min(cell["rowspan"], group_rem))
            end_col = c_idx + dc
            cell_id = len(cells)
            for rr in range(r_idx, r_idx + dr):
                if len(slots[rr]) < end_col:
                    slots[rr].extend([None] * (end_col - len(slots[rr])))
                for cc in range(c_idx, end_col):
                    if slots[rr][cc] is None:
                        slots[rr][cc] = cell_id
            cells.append(
                {
                    "cell_id": cell_id,
                    "row_idx": r_idx,
                    "col_idx": c_idx,
                    "rowspan": dr,
                    "colspan": dc,
                    "group": row["group"],
                    "is_header": cell["is_header"],
                    "scope": cell.get("scope", ""),
                    "text": cell["text"],
                }
            )
            width = max(width, end_col)
            c_idx = end_col

    for rr in range(num_rows):
        if len(slots[rr]) < width:
            slots[rr].extend([None] * (width - len(slots[rr])))

    # Compute full multi-row column headers (r=0..r0-1) and row stubs (c=0..c0-1)
    for cell in cells:
        cid = cell["cell_id"]
        r0, c0 = cell["row_idx"], cell["col_idx"]
        r1, c1 = r0 + cell["rowspan"], c0 + cell["colspan"]

        col_headers: list[str] = []
        sub_headers_r1plus: list[str] = []
        for hr in range(0, r0):
            for hc in range(c0, min(c1, len(slots[hr]))):
                hid = slots[hr][hc]
                if hid is not None and hid != cid and cells[hid]["is_header"]:
                    htxt = cells[hid]["text"].strip()
                    if htxt and htxt not in col_headers:
                        col_headers.append(htxt)
                        if hr >= 1 and htxt not in sub_headers_r1plus:
                            sub_headers_r1plus.append(htxt)

        row_headers: list[str] = []
        for rr in range(r0, min(r1, num_rows)):
            for hc in range(0, min(c0, len(slots[rr]))):
                hid = slots[rr][hc]
                if hid is not None and hid != cid:
                    hcell = cells[hid]
                    htxt = hcell["text"].strip()
                    if htxt and (hcell["is_header"] or hcell.get("scope") == "row" or hc == 0):
                        if htxt not in row_headers:
                            row_headers.append(htxt)

        cell["col_headers"] = col_headers
        cell["sub_headers_r1plus"] = sub_headers_r1plus
        cell["row_headers"] = row_headers

    return {
        "nesting_depth": nesting_depth,
        "row_count": num_rows,
        "width": width,
        "cells": cells,
        "slots": slots,
    }


def extract_whatwg_oracle_tables(html_str: str) -> list[dict[str, Any]]:
    parser = WhatwgTableOracleParser()
    parser.feed(html_str)
    parser.close()
    return parser.completed_tables


# ============================================================================
# Layer 1 Runner (`L1.1 .. L1.6`)
# ============================================================================


def run_l1_1_charset_and_nfc_oracle(
    client: EvaluateV2Client,
    warc_dir: str,
) -> dict[str, Any]:
    # 1. Deterministic Real WARC Crash / Timeout / Meta-Encoding Regression Suite (16 records)
    warc_records: list[dict[str, Any]] = []
    crash_warc = os.path.join(warc_dir, "CC-MAIN-20261012021225-20261012051225-00920.warc.gz")
    timeout_warc = os.path.join(warc_dir, "CC-MAIN-20261014154306-20261014184306-00432.warc.gz")
    sample_warc = os.path.join(warc_dir, "CC-MAIN-20261015222031-20261016012031-00106.warc.gz")
    if not os.path.exists(sample_warc):
        matches = sorted(glob.glob(os.path.join(warc_dir, "*-00106.warc.gz")))
        if matches:
            sample_warc = matches[0]

    def fetch_single_warc_at_offset(path: str, offset: int, label: str) -> dict[str, Any] | None:
        if not os.path.exists(path):
            return None
        for rec in iter_warc_records(path, start_offset=offset):
            raw_html, enc, src, rep = decode_html_bytes(rec.body, rec.http_headers)
            return {
                "label": label,
                "warc_file": os.path.basename(path),
                "member_offset": rec.member_offset,
                "url": rec.target_uri,
                "raw_bytes": len(rec.body),
                "encoding": enc,
                "encoding_source": src,
                "replacement_chars": rep,
                "raw_html": raw_html,
                "sanitized_html": sanitize_html_for_probe(raw_html),
            }
        return None

    r_crash = fetch_single_warc_at_offset(crash_warc, 11253578, "real_warc_html5ever_meta_charset_panic")
    if r_crash is not None:
        warc_records.append(r_crash)

    r_timeout = fetch_single_warc_at_offset(timeout_warc, 544318512, "real_warc_61527_td_quadratic_table_hang")
    if r_timeout is not None:
        warc_records.append(r_timeout)

    if os.path.exists(sample_warc):
        for rec in iter_warc_records(sample_warc):
            if len(warc_records) >= 16:
                break
            if rec.http_status != 200 or not rec.body:
                continue
            raw_html, enc, src, rep = decode_html_bytes(rec.body, rec.http_headers)
            san = sanitize_html_for_probe(raw_html)
            if san != raw_html or enc != "utf-8" or len(rec.body) > 250_000:
                warc_records.append(
                    {
                        "label": f"warc_00106_{rec.member_offset}_{enc}",
                        "warc_file": os.path.basename(sample_warc),
                        "member_offset": rec.member_offset,
                        "url": rec.target_uri,
                        "raw_bytes": len(rec.body),
                        "encoding": enc,
                        "encoding_source": src,
                        "replacement_chars": rep,
                        "raw_html": raw_html,
                        "sanitized_html": san,
                    }
                )

    warc_eval_rows = []
    unsanitized_panics = 0
    unsanitized_timeouts = 0
    sanitized_panics = 0
    sanitized_timeouts = 0

    for item in warc_records:
        # Test WITHOUT sanitize_html_for_probe (catch_panic=True inside core_probe, plus 3.5s hard timeout)
        res_raw = client.call_isolated(
            {
                "op": "evaluate",
                "doc_id": item["label"] + ":raw",
                "url": item["url"],
                "html": item["raw_html"],
                "parser_mode": "breakthrough",
                "catch_panic": True,
            },
            timeout_s=3.5,
        )
        # Test WITH sanitize_html_for_probe
        res_san = client.call_isolated(
            {
                "op": "evaluate",
                "doc_id": item["label"] + ":sanitized",
                "url": item["url"],
                "html": item["sanitized_html"],
                "parser_mode": "breakthrough",
                "catch_panic": True,
            },
            timeout_s=3.5,
        )
        raw_status = res_raw.get("status", "UNKNOWN")
        san_status = res_san.get("status", "UNKNOWN")
        if raw_status in ("PANIC", "CRASH"):
            unsanitized_panics += 1
        elif raw_status == "TIMEOUT":
            unsanitized_timeouts += 1
        if san_status in ("PANIC", "CRASH"):
            sanitized_panics += 1
        elif san_status == "TIMEOUT":
            sanitized_timeouts += 1

        warc_eval_rows.append(
            {
                "label": item["label"],
                "warc_file": item["warc_file"],
                "member_offset": item["member_offset"],
                "url": item["url"],
                "raw_bytes": item["raw_bytes"],
                "encoding": item["encoding"],
                "meta_was_rewritten_by_sanitizer": item["raw_html"] != item["sanitized_html"],
                "unsanitized_status": raw_status,
                "unsanitized_panic_message": res_raw.get("panic_message"),
                "unsanitized_elapsed_ms": res_raw.get("elapsed_ms"),
                "sanitized_status": san_status,
                "sanitized_elapsed_ms": res_san.get("elapsed_ms"),
            }
        )

    # 2. WHATWG §13.2.3.2 <meta> Prescan & Multi-Encoding Vectors
    prescan_vectors = [
        {
            "id": "commented_meta_trap",
            "desc": "<!-- <meta charset='shift_jis'> --> before <meta charset='utf-8'>",
            "body": "<!-- <meta charset=\"shift_jis\"> --><head><meta charset=\"utf-8\"></head><body><p>Café résumé</p></body>".encode("utf-8"),
            "headers": {"content-type": "text/html"},
            "expected_encoding": "utf-8",
            "expected_text_substring": "Café résumé",
        },
        {
            "id": "reversed_http_equiv_attrs",
            "desc": "<meta content='text/html; charset=windows-1252' http-equiv='Content-Type'>",
            "body": b"<head><meta content=\"text/html; charset=windows-1252\" http-equiv=\"Content-Type\"></head><body><p>Caf\xe9</p></body>",
            "headers": {"content-type": "text/html"},
            "expected_encoding": "windows-1252",
            "expected_text_substring": "Café",
        },
        {
            "id": "utf16le_bom",
            "desc": "UTF-16LE with BOM",
            "body": b"\xff\xfe" + "<p> Tiếng Việt </p>".encode("utf-16-le"),
            "headers": {"content-type": "text/html"},
            "expected_encoding": "utf-16-le",
            "expected_text_substring": "Tiếng Việt",
        },
        {
            "id": "utf16be_bom",
            "desc": "UTF-16BE with BOM",
            "body": b"\xfe\xff" + "<p> Tiếng Việt </p>".encode("utf-16-be"),
            "headers": {"content-type": "text/html"},
            "expected_encoding": "utf-16-be",
            "expected_text_substring": "Tiếng Việt",
        },
        {
            "id": "shift_jis_meta",
            "desc": "Shift_JIS via <meta charset='shift_jis'>",
            "body": b"<head><meta charset=\"shift_jis\"></head><body><p>" + "日本語テキスト".encode("shift_jis") + b"</p></body>",
            "headers": {"content-type": "text/html"},
            "expected_encoding": "shift_jis",
            "expected_text_substring": "日本語テキスト",
        },
        {
            "id": "gb18030_meta",
            "desc": "GB18030 via <meta charset='gb18030'>",
            "body": b"<head><meta charset=\"gb18030\"></head><body><p>" + "中文测试内容".encode("gb18030") + b"</p></body>",
            "headers": {"content-type": "text/html"},
            "expected_encoding": "gb18030",
            "expected_text_substring": "中文测试内容",
        },
        {
            "id": "euc_kr_meta",
            "desc": "EUC-KR via <meta charset='euc-kr'>",
            "body": b"<head><meta charset=\"euc-kr\"></head><body><p>" + "한국어 문서".encode("euc-kr") + b"</p></body>",
            "headers": {"content-type": "text/html"},
            "expected_encoding": "euc-kr",
            "expected_text_substring": "한국어 문서",
        },
        {
            "id": "meta_beyond_1024_bytes",
            "desc": "<meta charset='windows-1252'> at byte offset 1800 (within WHATWG 1024 vs 4096 prescan)",
            "body": b"<!-- " + (b"x" * 1750) + b" --><head><meta charset=\"windows-1252\"></head><body><p>Caf\xe9</p></body>",
            "headers": {"content-type": "text/html"},
            "expected_encoding": "windows-1252",
            "expected_text_substring": "Café",
        },
    ]

    from warc_ingest import whatwg_codec

    prescan_results = []
    for vec in prescan_vectors:
        decoded_html, det_enc, det_src, rep_cnt = decode_html_bytes(vec["body"], vec["headers"])
        norm_det = whatwg_codec(det_enc) or det_enc.lower().replace("_", "-")
        norm_exp = whatwg_codec(vec["expected_encoding"]) or vec["expected_encoding"].lower().replace("_", "-")
        enc_match = norm_det == norm_exp
        text_preserved = vec["expected_text_substring"] in decoded_html and rep_cnt == 0
        try:
            raw_utf8_str = vec["body"].decode("utf-8", errors="strict")
            rust_orchestrator_accepts_raw = vec["expected_text_substring"] in raw_utf8_str
        except UnicodeDecodeError:
            rust_orchestrator_accepts_raw = False
        prescan_results.append(
            {
                "id": vec["id"],
                "desc": vec["desc"],
                "expected_encoding": vec["expected_encoding"],
                "expected_python_codec": norm_exp,
                "detected_encoding": det_enc,
                "encoding_source": det_src,
                "replacement_chars": rep_cnt,
                "encoding_exact_match": enc_match,
                "text_preserved": text_preserved,
                "rust_orchestrator_raw_utf8_preserves_text": rust_orchestrator_accepts_raw,
            }
        )

    # 3. Unicode NFC Oracle (Vietnamese multi-diacritics, Hangul Jamo, Multi-CCC combining marks)
    nfc_cases = []
    vn_chars = [
        "ố", "ồ", "ổ", "ỗ", "ộ",
        "ấ", "ầ", "ẩ", "ẫ", "ậ",
        "ắ", "ằ", "ẳ", "ẵ", "ặ",
        "ế", "ề", "ể", "ễ", "ệ",
        "ớ", "ờ", "ở", "ỡ", "ợ",
        "ứ", "ừ", "ử", "ữ", "ự",
    ]
    for ch in vn_chars:
        nfd = unicodedata.normalize("NFD", f"Tiếng_Việt_{ch}")
        nfc = unicodedata.normalize("NFC", f"Tiếng_Việt_{ch}")
        nfc_cases.append(
            {
                "id": f"vn_nfd_{ch}",
                "category": "vietnamese_multi_diacritic_nfd",
                "input": nfd,
                "expected_nfc": nfc,
            }
        )
    # Reversed combining mark order (tone mark CCC=230 before horn CCC=216 or dot-below CCC=220)
    reversed_vn = [
        ("vn_rev_horn_acute", "u\u0301\u031b", unicodedata.normalize("NFC", "u\u0301\u031b")),
        ("vn_rev_dot_circumflex", "o\u0323\u0302", unicodedata.normalize("NFC", "o\u0323\u0302")),
        ("vn_rev_dot_horn", "u\u0323\u031b", unicodedata.normalize("NFC", "u\u0323\u031b")),
        ("vn_rev_dot_breve", "a\u0323\u0306", unicodedata.normalize("NFC", "a\u0323\u0306")),
    ]
    for cid, raw_in, exp_nfc in reversed_vn:
        nfc_cases.append(
            {
                "id": cid,
                "category": "vietnamese_reversed_ccc",
                "input": raw_in,
                "expected_nfc": exp_nfc,
            }
        )
    # Hangul Jamo NFD -> NFC syllables
    for word in ["한글", "각", "과학기술", "인공지능"]:
        nfc_cases.append(
            {
                "id": f"hangul_jamo_{word}",
                "category": "hangul_jamo_nfd",
                "input": unicodedata.normalize("NFD", word),
                "expected_nfc": unicodedata.normalize("NFC", word),
            }
        )
    # European & Greek multi-combining / standard NFD
    for word in ["Café", "naïve", "Zürich", "고", "ἀνθρώπων", "ō\u0301"]:
        nfc_cases.append(
            {
                "id": f"latin_greek_{word}",
                "category": "latin_greek_combining",
                "input": unicodedata.normalize("NFD", word),
                "expected_nfc": unicodedata.normalize("NFC", word),
            }
        )

    nfc_resp = client.call({"op": "nfc_probe", "cases": nfc_cases})
    nfc_rows = nfc_resp.get("results", [])
    by_cat: dict[str, dict[str, Any]] = {}
    for row in nfc_rows:
        cat = row["category"]
        st = by_cat.setdefault(
            cat,
            {
                "count": 0,
                "projection_nfc_exact_matches": 0,
                "bm25_v1_nfd_query_hits": 0,
                "bm25f_dual_nfd_query_hits": 0,
                "bm25f_dual_nfc_query_hits_proj_doc": 0,
            },
        )
        st["count"] += 1
        if row["exact_match"]:
            st["projection_nfc_exact_matches"] += 1
        if row["bm25_v1_nfd_query_hits_nfc_doc"]:
            st["bm25_v1_nfd_query_hits"] += 1
        if row["bm25f_dual_nfd_query_hits_nfc_doc"]:
            st["bm25f_dual_nfd_query_hits"] += 1
        if row["bm25f_dual_nfc_query_hits_proj_doc"]:
            st["bm25f_dual_nfc_query_hits_proj_doc"] += 1

    for cat, st in by_cat.items():
        n = max(1, st["count"])
        st["projection_nfc_exact_match_rate"] = round(st["projection_nfc_exact_matches"] / n, 4)
        st["bm25_v1_nfd_query_recall"] = round(st["bm25_v1_nfd_query_hits"] / n, 4)
        st["bm25f_dual_nfd_query_recall"] = round(st["bm25f_dual_nfd_query_hits"] / n, 4)
        st["bm25f_dual_nfc_query_recall_on_projected_doc"] = round(
            st["bm25f_dual_nfc_query_hits_proj_doc"] / n, 4
        )

    return {
        "warc_regression_16_records": {
            "total_records": len(warc_eval_rows),
            "unsanitized_panics": unsanitized_panics,
            "unsanitized_timeouts": unsanitized_timeouts,
            "sanitized_panics": sanitized_panics,
            "sanitized_timeouts": sanitized_timeouts,
            "records": warc_eval_rows,
        },
        "whatwg_meta_prescan_vectors": {
            "total_vectors": len(prescan_results),
            "encoding_exact_match_rate": round(
                sum(1 for r in prescan_results if r["encoding_exact_match"]) / max(1, len(prescan_results)), 4
            ),
            "text_preserved_rate": round(
                sum(1 for r in prescan_results if r["text_preserved"]) / max(1, len(prescan_results)), 4
            ),
            "vectors": prescan_results,
        },
        "unicode_nfc_oracle": {
            "total_cases": len(nfc_rows),
            "stratum_counts": nfc_resp.get("stratum_counts", {}),
            "overall_projection_nfc_exact_match_rate": round(
                sum(1 for r in nfc_rows if r["exact_match"]) / max(1, len(nfc_rows)), 4
            ),
            "overall_bm25_v1_nfd_query_recall_against_nfc_doc": round(
                sum(1 for r in nfc_rows if r["bm25_v1_nfd_query_hits_nfc_doc"]) / max(1, len(nfc_rows)), 4
            ),
            "overall_bm25f_dual_nfd_query_recall_against_nfc_doc": round(
                sum(1 for r in nfc_rows if r["bm25f_dual_nfd_query_hits_nfc_doc"]) / max(1, len(nfc_rows)), 4
            ),
            "by_category": by_cat,
        },
    }


def run_l1_2_dom_shadow_foster_landmark_oracle(
    client: EvaluateV2Client,
    webmainbench_path: str,
) -> dict[str, Any]:
    micro_cases = [
        {
            "id": "foster_parented_div_in_table",
            "category": "foster_parenting",
            "html": "<main><table><tbody><tr><td>Cell A</td></tr><div>Fostered scholarly paragraph inside table</div><tr><td>Cell B</td></tr></tbody></table></main>",
            "gold_substrings": ["Fostered scholarly paragraph inside table", "Cell A", "Cell B"],
        },
        {
            "id": "declarative_shadow_dom_open",
            "category": "declarative_shadow_dom",
            "html": "<main><article><host-card><template shadowrootmode=\"open\"><p>Declarative shadow root scientific abstract.</p></template></host-card><p>Light DOM paragraph.</p></article></main>",
            "gold_substrings": ["Declarative shadow root scientific abstract.", "Light DOM paragraph."],
        },
        {
            "id": "legacy_tag_blindness_div_blockquote_dl_fig_h5_h6",
            "category": "legacy_tag_blindness",
            "html": "<main><div>Div-only prose paragraph.</div><blockquote>Blockquote theorem statement.</blockquote><dl><dt>Term Alpha</dt><dd>Definition Beta</dd></dl><figure><figcaption>Figure 1: Phase diagram</figcaption></figure><h5>Subsection H5</h5><h6>Subsubsection H6</h6></main>",
            "gold_substrings": [
                "Div-only prose paragraph.",
                "Blockquote theorem statement.",
                "Term Alpha",
                "Definition Beta",
                "Figure 1: Phase diagram",
                "Subsection H5",
                "Subsubsection H6",
            ],
        },
        {
            "id": "aspnet_form_full_page_wrapper",
            "category": "aspnet_form_wrapper",
            "html": "<html><body><form id=\"aspnetForm\" method=\"post\" action=\"default.aspx\"><div class=\"content\"><p>Critical enterprise knowledge base article inside ASP.NET form wrapper.</p></div></form></body></html>",
            "gold_substrings": ["Critical enterprise knowledge base article inside ASP.NET form wrapper."],
        },
        {
            "id": "aspnet_form_with_heading_wipes_body",
            "category": "aspnet_form_wrapper_with_heading",
            "html": "<html><body><form id=\"aspnetForm\" method=\"post\" action=\"default.aspx\"><h1>Enterprise Security Bulletin</h1><div class=\"content\"><p>Critical enterprise knowledge base article inside ASP.NET form wrapper.</p></div></form></body></html>",
            "gold_substrings": ["Critical enterprise knowledge base article inside ASP.NET form wrapper."],
        },
        {
            "id": "aside_callout_inside_main_article",
            "category": "aside_landmark_inside_main",
            "html": "<main><article><h1>Theorem 1</h1><aside class=\"callout-note\"><p>Crucial lemma proof inside semantic aside callout.</p></aside><p>Main proof conclusion.</p></article></main>",
            "gold_substrings": ["Crucial lemma proof inside semantic aside callout.", "Main proof conclusion."],
        },
        {
            "id": "benign_substring_classes",
            "category": "benign_substring_classes",
            "html": "<main><section class=\"modal-logic\"><p>Kripke semantics for modal logic S5.</p></section><section id=\"related-work\"><p>Prior work on distributed consensus.</p></section><div class=\"navigation-theory\"><p>Inertial navigation theory and Kalman filtering.</p></div><div class=\"social-science\"><p>Quantitative methods in computational social science.</p></div></main>",
            "gold_substrings": [
                "Kripke semantics for modal logic S5.",
                "Prior work on distributed consensus.",
                "Inertial navigation theory and Kalman filtering.",
                "Quantitative methods in computational social science.",
            ],
        },
        {
            "id": "sibling_dom_path_slug_uniqueness",
            "category": "dom_path_slug_uniqueness",
            "html": "<main><p>First paragraph.</p><p>Second paragraph.</p><p>Third paragraph.</p><p>Fourth paragraph.</p></main>",
            "gold_substrings": ["First paragraph.", "Second paragraph.", "Third paragraph.", "Fourth paragraph."],
        },
    ]

    case_results = []
    for mc in micro_cases:
        resp = client.call(
            {
                "op": "evaluate",
                "doc_id": mc["id"],
                "html": mc["html"],
                "parser_mode": "both",
                "boilerplate_policy": "cetd_entropy_v1",
                "emit_stage_trace": True,
            }
        )
        leg_chunks = resp.get("legacy", {}).get("chunks_only", [])
        bt_chunks = resp.get("breakthrough", {}).get("chunks", [])
        leg_text = "\n".join(c["text"] for c in leg_chunks)
        bt_text = "\n".join(c["text"] for c in bt_chunks)
        leg_hits = sum(1 for g in mc["gold_substrings"] if g in leg_text)
        bt_hits = sum(1 for g in mc["gold_substrings"] if g in bt_text)
        slugs = [c["dom_path_slug"] for c in bt_chunks]
        unique_slugs = len(set(slugs))
        case_results.append(
            {
                "id": mc["id"],
                "category": mc["category"],
                "gold_items": len(mc["gold_substrings"]),
                "legacy_recall": round(leg_hits / len(mc["gold_substrings"]), 4),
                "breakthrough_recall": round(bt_hits / len(mc["gold_substrings"]), 4),
                "bt_chunk_count": len(bt_chunks),
                "bt_unique_dom_path_slugs": unique_slugs,
                "bt_slug_collision_rate": round(1.0 - (unique_slugs / max(1, len(bt_chunks))), 4),
            }
        )

    # Quantify double-parse overhead and CetdEntropyV1 overwrite on 50 real WebMainBench pages
    legacy_us_list = []
    bt_us_list = []
    pass2_us_list = []
    cetd_noop_count = 0
    leg_cetd_active_count = 0
    total_real = 0
    if os.path.exists(webmainbench_path):
        with open(webmainbench_path, "r", encoding="utf-8") as f:
            for line in f:
                if total_real >= 50:
                    break
                row = json.loads(line)
                html_str = sanitize_html_for_probe(row.get("html", ""))
                if not html_str:
                    continue
                resp = client.call(
                    {
                        "op": "evaluate",
                        "doc_id": str(row.get("id", total_real)),
                        "html": html_str,
                        "parser_mode": "both",
                        "boilerplate_policy": "cetd_entropy_v1",
                        "emit_stage_trace": True,
                    }
                )
                if resp.get("status") != "OK":
                    continue
                total_real += 1
                legacy_us_list.append(resp["legacy"]["parse_us"])
                bt_us_list.append(resp["breakthrough"]["parse_us"])
                st = resp.get("stage_trace", {})
                pass2_us_list.append(st.get("s2_dom_shadow_foster", {}).get("pass2_duplicate_parse_us", 0))
                s5_prune = st.get("s5_cetd_prune", {})
                if s5_prune.get("bt_cetd_entropy_identical_to_s4", False):
                    cetd_noop_count += 1
                if not s5_prune.get("leg_cetd_entropy_identical_to_s4", True):
                    leg_cetd_active_count += 1
    return {
        "micro_cases": case_results,
        "real_50_page_double_parse_and_cetd_audit": {
            "pages_evaluated": total_real,
            "mean_legacy_parse_us": round(sum(legacy_us_list) / max(1, len(legacy_us_list)), 1),
            "mean_breakthrough_parse_us": round(sum(bt_us_list) / max(1, len(bt_us_list)), 1),
            "mean_pass2_duplicate_html_parse_us": round(sum(pass2_us_list) / max(1, len(pass2_us_list)), 1),
            "breakthrough_vs_legacy_latency_ratio": round(
                sum(bt_us_list) / max(1, sum(legacy_us_list)), 3
            ),
            "leg_cetd_entropy_v1_active_rate": round(leg_cetd_active_count / max(1, total_real), 4),
            "bt_cetd_entropy_v1_zero_effect_rate": round(cetd_noop_count / max(1, total_real), 4),
        },
    }


def run_l1_3_math_ast_oracle(
    client: EvaluateV2Client,
    wikimedia_dir: str,
) -> dict[str, Any]:
    # 1. Semantic Inversion Trap Suite (proving bag-of-tokens F1 is 100% blind to inversion while Math AST catches 100%)
    inversion_traps = [
        (r"\frac{a}{b}", r"\frac{b}{a}", "swapped_fraction_numerator_denominator"),
        (r"x_{i}^{2}", r"x_{2}^{i}", "swapped_subscript_superscript"),
        (r"\sqrt[3]{x}", r"\sqrt[x]{3}", "swapped_root_index_radicand"),
        (r"a - b", r"b - a", "non_commutative_subtraction"),
        (r"\int_{0}^{\infty} f(x) dx", r"\int_{\infty}^{0} f(x) dx", "inverted_integral_limits"),
        (r"A \subset B", r"B \subset A", "inverted_subset_relation"),
        (
            r"\begin{matrix} a & b \\ c & d \end{matrix}",
            r"\begin{matrix} d & c \\ b & a \end{matrix}",
            "transposed_reversed_matrix",
        ),
    ]
    trap_results = []
    for gold_tex, inv_tex, desc in inversion_traps:
        tok_g = Counter(tokenize_latex_math(gold_tex))
        tok_i = Counter(tokenize_latex_math(inv_tex))
        inter = sum((tok_g & tok_i).values())
        bow_f1 = (2.0 * inter) / max(1, sum(tok_g.values()) + sum(tok_i.values()))
        ast_sim, ast_exact = math_ast_similarity(gold_tex, inv_tex)
        trap_results.append(
            {
                "desc": desc,
                "gold_tex": gold_tex,
                "inverted_tex": inv_tex,
                "bag_of_tokens_f1": round(bow_f1, 4),
                "math_ast_similarity": round(ast_sim, 4),
                "math_ast_exact_match": ast_exact,
                "ast_rejected_inversion": not ast_exact and ast_sim < 0.85,
            }
        )

    # 2. Equivalence Invariance Suite (\tfrac vs \frac, \left(\right) vs (), {x} vs x)
    equiv_pairs = [
        (r"\tfrac{1}{2}", r"\frac{1}{2}", "tfrac_to_frac"),
        (r"\dfrac{\alpha}{\beta}", r"\frac{\alpha}{\beta}", "dfrac_to_frac"),
        (r"\left( x + y \right)", r"( x + y )", "left_right_delimiters"),
        (r"x^{2}", r"x^2", "single_token_brace_elision"),
        (r"\operatorname{sin}(x)", r"\sin(x)", "operatorname_to_cmd"),
    ]
    equiv_results = []
    for tex_a, tex_b, desc in equiv_pairs:
        ast_sim, ast_exact = math_ast_similarity(tex_a, tex_b)
        equiv_results.append(
            {
                "desc": desc,
                "tex_a": tex_a,
                "tex_b": tex_b,
                "math_ast_similarity": round(ast_sim, 4),
                "math_ast_exact_match": ast_exact,
            }
        )

    # 3. Evaluate 592 real Wikimedia formulas + specialized MathML/KaTeX probes across all 10 categories under TWO conditions:
    #    Condition A: Raw HTML (with <annotation encoding="application/x-tex"> intact -> Tier 1 shortcut)
    #    Condition B: Presentation MathML / KaTeX HTML Tree ONLY (<annotation> and alttext/data-latex stripped -> tests Tier 2 convert_mathml_element_to_latex!)
    math_re = re.compile(r"<math\b[^>]*>.*?</math>", re.IGNORECASE | re.DOTALL)
    annot_re = re.compile(
        r"<annotation(?:-xml)?\b[^>]*encoding=[\"']?(?:application/x-tex|tex|latex)[\"']?[^>]*>(.*?)</annotation(?:-xml)?>",
        re.IGNORECASE | re.DOTALL,
    )
    strip_annot_re = re.compile(r"<annotation(?:-xml)?\b[^>]*>.*?</annotation(?:-xml)?>", re.IGNORECASE | re.DOTALL)
    strip_alt_re = re.compile(r"\b(?:alttext|data-latex|data-tex)\s*=\s*(?:\"[^\"]*\"|'[^']*')", re.IGNORECASE)

    formulas: list[tuple[str, str, str]] = []
    cat_counts_seen: dict[str, int] = {}
    archives = sorted(glob.glob(os.path.join(wikimedia_dir, "*-ENTERPRISE-HTML.json.tar.gz")))
    for arch in archives:
        if len(formulas) >= 592:
            break
        with tarfile.open(arch, "r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                fobj = tar.extractfile(member)
                if fobj is None:
                    continue
                for raw_line in fobj:
                    if len(formulas) >= 592:
                        break
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                    html_str = obj.get("article_body", {}).get("html", "")
                    if "<math" not in html_str:
                        continue
                    for m in math_re.finditer(html_str):
                        frag = m.group(0)
                        am = annot_re.search(frag)
                        if am:
                            gold_tex = html_lib.unescape(am.group(1)).strip()
                            if gold_tex:
                                c_name = classify_formula_category(gold_tex, frag)
                                # Cap any single category at 110 so all 592 Wikimedia slots span all structural categories across the dumps
                                if cat_counts_seen.get(c_name, 0) >= 110:
                                    continue
                                cat_counts_seen[c_name] = cat_counts_seen.get(c_name, 0) + 1
                                stripped_frag = strip_alt_re.sub("", strip_annot_re.sub("", frag))
                                formulas.append((gold_tex, frag, stripped_frag))
                                if len(formulas) >= 592:
                                    break

    # Ensure all 10 required structural categories have dedicated coverage (including <mfrac>, <menclose>, <mmultiscripts>, mathvariant, and KaTeX HTML spans)
    curated_10cat_probes = [
        (
            r"\frac{x + 1}{y - 1}",
            '<math><semantics><mfrac><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><mrow><mi>y</mi><mo>-</mo><mn>1</mn></mrow></mfrac><annotation encoding="application/x-tex">\\frac{x + 1}{y - 1}</annotation></semantics></math>',
            '<math><mfrac><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow><mrow><mi>y</mi><mo>-</mo><mn>1</mn></mrow></mfrac></math>',
        ),
        (
            r"\boxed{E = mc^{2}}",
            '<math><semantics><menclose notation="box"><mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></menclose><annotation encoding="application/x-tex">\\boxed{E = mc^{2}}</annotation></semantics></math>',
            '<math><menclose notation="box"><mi>E</mi><mo>=</mo><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></menclose></math>',
        ),
        (
            r"\cancel{x + 1}",
            '<math><semantics><menclose notation="updiagonalstrike"><mi>x</mi><mo>+</mo><mn>1</mn></menclose><annotation encoding="application/x-tex">\\cancel{x + 1}</annotation></semantics></math>',
            '<math><menclose notation="updiagonalstrike"><mi>x</mi><mo>+</mo><mn>1</mn></menclose></math>',
        ),
        (
            r"x \in \mathbb{R}^{n}",
            '<math><semantics><mrow><mi>x</mi><mo>∈</mo><msup><mi mathvariant="double-struck">R</mi><mi>n</mi></msup></mrow><annotation encoding="application/x-tex">x \\in \\mathbb{R}^{n}</annotation></semantics></math>',
            '<math><mrow><mi>x</mi><mo>∈</mo><msup><mi mathvariant="double-struck">R</mi><mi>n</mi></msup></mrow></math>',
        ),
        (
            r"\mathcal{L}(\theta)",
            '<math><semantics><mrow><mi mathvariant="script">L</mi><mo>(</mo><mi>θ</mi><mo>)</mo></mrow><annotation encoding="application/x-tex">\\mathcal{L}(\\theta)</annotation></semantics></math>',
            '<math><mrow><mi mathvariant="script">L</mi><mo>(</mo><mi>θ</mi><mo>)</mo></mrow></math>',
        ),
        (
            r"{}_{92}^{235}\text{U}",
            '<math><semantics><mmultiscripts><mtext>U</mtext><mprescripts/><mn>92</mn><mn>235</mn></mmultiscripts><annotation encoding="application/x-tex">{}_{92}^{235}\\text{U}</annotation></semantics></math>',
            '<math><mmultiscripts><mtext>U</mtext><mprescripts/><mn>92</mn><mn>235</mn></mmultiscripts></math>',
        ),
        (
            r"\frac{a + b}{c}",
            '<span class="katex"><span class="katex-mathml"><math><semantics><mfrac><mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow><mi>c</mi></mfrac><annotation encoding="application/x-tex">\\frac{a + b}{c}</annotation></semantics></math></span><span class="katex-html" aria-hidden="true"><span class="base"><span class="mfrac">a+b/c</span></span></span></span>',
            '<span class="katex"><span class="katex-html" aria-hidden="true"><span class="base"><span class="mord mathnormal">a</span><span class="mbin">+</span><span class="mord mathnormal">b</span></span></span></span>',
        ),
        (
            r"\sqrt[3]{x^{2} + 1}",
            '<math><semantics><mroot><mrow><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mn>1</mn></mrow><mn>3</mn></mroot><annotation encoding="application/x-tex">\\sqrt[3]{x^{2} + 1}</annotation></semantics></math>',
            '<math><mroot><mrow><msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mn>1</mn></mrow><mn>3</mn></mroot></math>',
        ),
    ]
    all_eval_formulas = formulas + curated_10cat_probes

    cat_stats: dict[str, dict[str, Any]] = {}
    t1_seq_sims, t1_ast_sims, t1_ast_exacts = [], [], []
    t2_seq_sims, t2_ast_sims, t2_ast_exacts = [], [], []

    for gold_tex, raw_frag, stripped_frag in all_eval_formulas:
        cat = classify_formula_category(gold_tex, raw_frag)
        r_t1 = client.call({"op": "math_code_probe", "html": raw_frag})
        r_t2 = client.call({"op": "math_code_probe", "html": stripped_frag})
        pred_t1 = (r_t1.get("math_latex") or [""])[0].strip("$").strip()
        pred_t2 = (r_t2.get("math_latex") or [""])[0].strip("$").strip()

        seq_t1 = difflib.SequenceMatcher(None, gold_tex.replace(" ", ""), pred_t1.replace(" ", "")).ratio()
        ast_sim_t1, ast_ex_t1 = math_ast_similarity(gold_tex, pred_t1)

        seq_t2 = difflib.SequenceMatcher(None, gold_tex.replace(" ", ""), pred_t2.replace(" ", "")).ratio()
        ast_sim_t2, ast_ex_t2 = math_ast_similarity(gold_tex, pred_t2)

        t1_seq_sims.append(seq_t1)
        t1_ast_sims.append(ast_sim_t1)
        t1_ast_exacts.append(1.0 if ast_ex_t1 else 0.0)

        t2_seq_sims.append(seq_t2)
        t2_ast_sims.append(ast_sim_t2)
        t2_ast_exacts.append(1.0 if ast_ex_t2 else 0.0)

        cs = cat_stats.setdefault(
            cat,
            {
                "count": 0,
                "t1_seq_sum": 0.0,
                "t1_ast_sim_sum": 0.0,
                "t1_ast_exact_sum": 0,
                "t2_seq_sum": 0.0,
                "t2_ast_sim_sum": 0.0,
                "t2_ast_exact_sum": 0,
            },
        )
        cs["count"] += 1
        cs["t1_seq_sum"] += seq_t1
        cs["t1_ast_sim_sum"] += ast_sim_t1
        cs["t1_ast_exact_sum"] += 1 if ast_ex_t1 else 0
        cs["t2_seq_sum"] += seq_t2
        cs["t2_ast_sim_sum"] += ast_sim_t2
        cs["t2_ast_exact_sum"] += 1 if ast_ex_t2 else 0

    stratified_summary = {}
    for cat, cs in sorted(cat_stats.items()):
        cnt = max(1, cs["count"])
        stratified_summary[cat] = {
            "count": cs["count"],
            "tier1_with_annotation": {
                "legacy_char_seq_similarity": round(cs["t1_seq_sum"] / cnt, 4),
                "math_ast_similarity": round(cs["t1_ast_sim_sum"] / cnt, 4),
                "math_ast_exact_match_rate": round(cs["t1_ast_exact_sum"] / cnt, 4),
            },
            "tier2_presentation_mathml_tree_only": {
                "legacy_char_seq_similarity": round(cs["t2_seq_sum"] / cnt, 4),
                "math_ast_similarity": round(cs["t2_ast_sim_sum"] / cnt, 4),
                "math_ast_exact_match_rate": round(cs["t2_ast_exact_sum"] / cnt, 4),
            },
        }

    n_f = max(1, len(all_eval_formulas))
    return {
        "wikimedia_formulas_evaluated": len(formulas),
        "total_formulas_with_10cat_probes": len(all_eval_formulas),
        "tier1_with_annotation_overall": {
            "legacy_char_seq_similarity": round(sum(t1_seq_sims) / n_f, 4),
            "math_ast_similarity": round(sum(t1_ast_sims) / n_f, 4),
            "math_ast_exact_match_rate": round(sum(t1_ast_exacts) / n_f, 4),
        },
        "tier2_presentation_mathml_tree_only_overall": {
            "legacy_char_seq_similarity": round(sum(t2_seq_sims) / n_f, 4),
            "math_ast_similarity": round(sum(t2_ast_sims) / n_f, 4),
            "math_ast_exact_match_rate": round(sum(t2_ast_exacts) / n_f, 4),
        },
        "by_category_10_strata": stratified_summary,
        "semantic_inversion_traps": {
            "total_traps": len(trap_results),
            "bag_of_tokens_false_accept_rate": round(
                sum(1 for r in trap_results if r["bag_of_tokens_f1"] >= 0.99) / len(trap_results), 4
            ),
            "math_ast_inversion_detection_rate": round(
                sum(1 for r in trap_results if r["ast_rejected_inversion"]) / len(trap_results), 4
            ),
            "traps": trap_results,
        },
        "algebraic_equivalence_invariance": {
            "total_pairs": len(equiv_results),
            "math_ast_exact_match_rate": round(
                sum(1 for r in equiv_results if r["math_ast_exact_match"]) / len(equiv_results), 4
            ),
            "pairs": equiv_results,
        },
    }


def run_l1_4_whatwg_table_oracle(
    client: EvaluateV2Client,
    wikimedia_dir: str,
) -> dict[str, Any]:
    # 1. Controlled WHATWG §4.9.12 Structural Torture Cases
    synthetic_tables = [
        {
            "id": "multi_row_thead_subheaders",
            "desc": "2-row <thead> with colspan=2 parent header and r=1 sub-headers",
            "html": "<main><table><thead><tr><th rowspan='2'>Metric</th><th colspan='2'>2025 Performance</th></tr><tr><th>Q1</th><th>Q2</th></tr></thead><tbody><tr><th scope='row'>Latency</th><td>12ms</td><td>9ms</td></tr></tbody></table></main>",
        },
        {
            "id": "table_over_512_rows",
            "desc": "530-row data table exceeding hardcoded 512-row expand_tolerant cap",
            "html": "<main><table><thead><tr><th>RowID</th><th>Value</th></tr></thead><tbody>"
            + "".join(f"<tr><td>R{i}</td><td>V{i}</td></tr>" for i in range(530))
            + "</tbody></table></main>",
        },
        {
            "id": "nested_table_inside_td",
            "desc": "Outer table containing an inner nested <table> inside a <td>",
            "html": "<main><table><thead><tr><th>System</th><th>Inner Details</th></tr></thead><tbody><tr><td>Athena</td><td><table><tr><th>Stage</th><th>Recall</th></tr><tr><td>S2</td><td>99%</td></tr></table></td></tr></tbody></table></main>",
        },
        {
            "id": "rowspan_zero_and_tfoot_before_tbody",
            "desc": "Out-of-order <tfoot> before <tbody> with rowspan=0",
            "html": "<main><table><thead><tr><th>Group</th><th>Item</th></tr></thead><tfoot><tr><td>FooterSum</td><td>100</td></tr></tfoot><tbody><tr><th rowspan='0'>Alpha</th><td>Item1</td></tr><tr><td>Item2</td></tr></tbody></table></main>",
        },
    ]

    synth_results = []
    for st in synthetic_tables:
        oracle_tables = extract_whatwg_oracle_tables(st["html"])
        resp = client.call(
            {
                "op": "evaluate",
                "doc_id": st["id"],
                "html": st["html"],
                "parser_mode": "breakthrough",
            }
        )
        athena_tables = resp.get("breakthrough", {}).get("tables", [])
        oracle_cells = [c for t in oracle_tables for c in t["cells"] if c["text"]]
        athena_cells = [c for t in athena_tables for c in t.get("grounded_cells", []) if c.get("text")]

        oracle_coords = {(c["row_idx"], c["col_idx"], c["text"]) for c in oracle_cells}
        athena_coords = {(c["row_idx"], c["col_idx"], c["text"]) for c in athena_cells}
        coord_hits = len(oracle_coords & athena_coords)

        synth_results.append(
            {
                "id": st["id"],
                "desc": st["desc"],
                "oracle_table_count": len(oracle_tables),
                "athena_table_count": len(athena_tables),
                "oracle_cell_count": len(oracle_cells),
                "athena_cell_count": len(athena_cells),
                "uncapped_cell_recall": round(len(athena_cells) / max(1, len(oracle_cells)), 4),
                "whatwg_coord_exact_match_rate": round(coord_hits / max(1, len(oracle_coords)), 4),
            }
        )

    # 2. Evaluate the exact 87 Wikimedia table pages (173 data tables) against Independent Uncapped WHATWG §4.9.12 Oracle
    import tier_b

    data_root = os.path.dirname(os.path.dirname(wikimedia_dir.rstrip("/\\")))
    t_candidates: list[tuple[str, Any, list[Any], list[dict[str, Any]]]] = []
    for sid in tier_b.DEFAULT_SOURCES:
        try:
            archive, _ = tier_b.verify_wikimedia_source(data_root, sid)
        except Exception:
            continue
        t_pool: list[tuple[str, Any, list[Any], list[dict[str, Any]]]] = []
        scanned = 0
        for article in tier_b.iter_wikimedia_articles(archive, source_id=sid):
            scanned += 1
            if "<table" in article.html and "<th" in article.html and "<td" in article.html:
                otables_capped = tier_b.extract_oracle_data_tables(article.html)
                if otables_capped:
                    has_spanned = any(t.has_span for t in otables_capped)
                    prefix = "0:" if has_spanned else "1:"
                    key = prefix + tier_b._pick_key("table", f"{sid}:{article.identifier}:{article.name}")
                    # Also parse with uncapped WHATWG §4.9.12 oracle (no 512-row cap + nested tables)
                    parser_raw = tier_b._TableExtractorParser()
                    parser_raw.feed(article.html)
                    parser_raw.close()
                    uncapped_tables: list[dict[str, Any]] = []
                    for tinfo in parser_raw.tables:
                        if tinfo["role"] in ("presentation", "none"):
                            continue
                        if tinfo["classes"] & _CHROME_TABLE_CLASSES:
                            continue
                        if len(tinfo["rows"]) < 2:
                            continue
                        u_slots, u_grounded, u_rs, u_cs = tier_b.expand_oracle_grid(
                            tinfo["rows"], max_rows=100_000, max_cols=1024
                        )
                        u_h = len(u_slots)
                        u_w = len(u_slots[0]) if u_slots else 0
                        if u_h < 2 or u_w < 2:
                            continue
                        h_cells = [c for c in u_grounded if c.is_header and c.text]
                        d_cells = [c for c in u_grounded if not c.is_header and c.text]
                        if not h_cells or len(d_cells) < 2:
                            continue
                        uncapped_tables.append(
                            {
                                "height": u_h,
                                "width": u_w,
                                "slots": u_slots,
                                "cells": u_grounded,
                                "data_cells": d_cells,
                            }
                        )
                    t_pool.append((key, article, otables_capped, uncapped_tables))
            if scanned >= 5000:
                break
        t_pool.sort(key=lambda item: item[0])
        t_candidates.extend(t_pool[:40])

    total_tables_evaluated = 0
    total_capped_512_data_cells = 0
    total_uncapped_data_cells = 0
    total_nested_table_cells = 0
    total_athena_recovered_data_cells = 0
    total_athena_grounded_cells = 0
    coord_exact_matches = 0
    total_coord_denominator = 0
    total_subheader_r1plus_cells = 0
    subheader_r1plus_hits = 0
    total_row_stub_cells = 0
    row_stub_hits = 0
    tables_exceeding_512_rows_dropped = 0

    for _, article, otables_capped, uncapped_tables in t_candidates:
        san_html = sanitize_html_for_probe(article.html)
        whatwg_all = extract_whatwg_oracle_tables(san_html)
        for wt in whatwg_all:
            if wt["nesting_depth"] >= 1:
                total_nested_table_cells += len([c for c in wt["cells"] if not c["is_header"] and c["text"].strip()])

        resp = client.call(
            {
                "op": "evaluate",
                "doc_id": f"wiki_page_{article.identifier}",
                "url": article.url,
                "html": san_html,
                "parser_mode": "breakthrough",
            }
        )
        bt_chunks = resp.get("breakthrough", {}).get("chunks", [])
        bt_tabs = resp.get("breakthrough", {}).get("tables", [])
        probe_groups = tier_b.split_probe_table_chunk_groups(bt_chunks)

        for t_pos, otable_cap in enumerate(otables_capped):
            total_tables_evaluated += 1
            cap_data_cells = [c for c in otable_cap.cells if not c.is_header and c.text]
            total_capped_512_data_cells += len(cap_data_cells)

            uncap_tbl = uncapped_tables[t_pos] if t_pos < len(uncapped_tables) else None
            uncap_data_cells = uncap_tbl["data_cells"] if uncap_tbl is not None else cap_data_cells
            total_uncapped_data_cells += len(uncap_data_cells)
            if uncap_tbl is not None and uncap_tbl["height"] > 512:
                tables_exceeding_512_rows_dropped += 1

            scored = tier_b.score_oracle_table_against_probe(otable_cap, probe_groups, bt_tabs)
            total_athena_recovered_data_cells += scored["recovered_data_cells"]

            # Match best Athena grounded table by coordinate/text overlap
            best_bt = None
            best_matches = -1
            for cand_bt in bt_tabs:
                cand_map = {(c["row_idx"], c["col_idx"]): c for c in cand_bt.get("grounded_cells", [])}
                m_cnt = sum(
                    1
                    for oc in uncap_data_cells
                    if (oc.row_idx, oc.col_idx) in cand_map
                    and oc.text.strip() in cand_map[(oc.row_idx, oc.col_idx)].get("text", "")
                )
                if m_cnt > best_matches:
                    best_matches = m_cnt
                    best_bt = cand_bt

            a_by_coord = (
                {(c["row_idx"], c["col_idx"]): c for c in best_bt.get("grounded_cells", [])}
                if best_bt is not None
                else {}
            )
            if best_bt is not None:
                total_athena_grounded_cells += len(
                    [c for c in best_bt.get("grounded_cells", []) if not c.get("is_header") and c.get("text", "").strip()]
                )

            u_slots = uncap_tbl["slots"] if uncap_tbl is not None else otable_cap.slots
            u_all_cells = uncap_tbl["cells"] if uncap_tbl is not None else otable_cap.cells
            for oc in uncap_data_cells:
                total_coord_denominator += 1
                ac = a_by_coord.get((oc.row_idx, oc.col_idx))
                if ac is not None and oc.text.strip() in ac.get("text", ""):
                    coord_exact_matches += 1
                # Check if cell has multi-row sub-headers at r >= 1
                sub_hdrs_r1: list[str] = []
                for hr in range(1, oc.row_idx):
                    if hr < len(u_slots):
                        for hc in range(oc.col_idx, min(oc.col_idx + oc.colspan, len(u_slots[hr]))):
                            hid = u_slots[hr][hc]
                            if hid is not None and hid < len(u_all_cells) and u_all_cells[hid].is_header:
                                htxt = u_all_cells[hid].text.strip()
                                if htxt and htxt not in sub_hdrs_r1:
                                    sub_hdrs_r1.append(htxt)
                if sub_hdrs_r1:
                    total_subheader_r1plus_cells += 1
                    if ac is not None and all(sh in ac.get("grounded_context", "") for sh in sub_hdrs_r1):
                        subheader_r1plus_hits += 1
                if oc.row_headers:
                    total_row_stub_cells += 1
                    if ac is not None and all(rh in ac.get("grounded_context", "") for rh in oc.row_headers):
                        row_stub_hits += 1

    total_true_all_data_cells = total_uncapped_data_cells + total_nested_table_cells
    return {
        "synthetic_whatwg_torture_cases": synth_results,
        "wikimedia_173_tables_oracle": {
            "pages_evaluated": len(t_candidates),
            "tables_evaluated": total_tables_evaluated,
            "tables_exceeding_512_rows_dropped": tables_exceeding_512_rows_dropped,
            "total_oracle_cells_capped_512_outer_only": total_capped_512_data_cells,
            "total_oracle_cells_uncapped_outer": total_uncapped_data_cells,
            "total_nested_table_data_cells_dropped": total_nested_table_cells,
            "total_oracle_cells_uncapped_with_nested": total_true_all_data_cells,
            "total_athena_recovered_data_cells": total_athena_recovered_data_cells,
            "total_athena_grounded_data_cells": total_athena_grounded_cells,
            "legacy_capped_cell_recall": round(
                total_athena_recovered_data_cells / max(1, total_capped_512_data_cells), 4
            ),
            "true_uncapped_outer_cell_recall": round(
                total_athena_recovered_data_cells / max(1, total_uncapped_data_cells), 4
            ),
            "true_uncapped_all_cell_recall_including_nested": round(
                total_athena_recovered_data_cells / max(1, total_true_all_data_cells), 4
            ),
            "whatwg_coordinate_exact_match_rate": round(
                coord_exact_matches / max(1, total_coord_denominator), 4
            ),
            "multi_row_subheader_r1plus_cells": total_subheader_r1plus_cells,
            "multi_row_subheader_r1plus_recall": round(
                subheader_r1plus_hits / max(1, total_subheader_r1plus_cells), 4
            ),
            "row_stub_cells": total_row_stub_cells,
            "row_stub_recall": round(row_stub_hits / max(1, total_row_stub_cells), 4),
        },
    }


def run_l1_5_code_sanitization_and_syntax_oracle(
    client: EvaluateV2Client,
    webmainbench_path: str = "",
) -> dict[str, Any]:
    clean_py = "def quicksort(arr):\n    if len(arr) <= 1:\n        return arr\n    pivot = arr[0]\n    return quicksort([x for x in arr[1:] if x < pivot]) + [pivot]"
    gutter_fixtures = [
        {
            "id": "gutter_pipe_space",
            "style": "1 | code",
            "html": "<pre><code class=\"language-python\">1 | def quicksort(arr):\n2 |     if len(arr) &lt;= 1:\n3 |         return arr\n4 |     pivot = arr[0]\n5 |     return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_tab",
            "style": "1\\tcode",
            "html": "<pre><code class=\"language-python\">1\tdef quicksort(arr):\n2\t    if len(arr) &lt;= 1:\n3\t        return arr\n4\t    pivot = arr[0]\n5\t    return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_colon_space",
            "style": "1: code",
            "html": "<pre><code class=\"language-python\">1: def quicksort(arr):\n2:     if len(arr) &lt;= 1:\n3:         return arr\n4:     pivot = arr[0]\n5:     return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_dot_space",
            "style": "1. code",
            "html": "<pre><code class=\"language-python\">1. def quicksort(arr):\n2.     if len(arr) &lt;= 1:\n3.         return arr\n4.     pivot = arr[0]\n5.     return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_padded_spaces",
            "style": "   1  code",
            "html": "<pre><code class=\"language-python\">   1  def quicksort(arr):\n   2      if len(arr) &lt;= 1:\n   3          return arr\n   4      pivot = arr[0]\n   5      return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_dom_span_line_number",
            "style": "<span class='line-number'>1</span>",
            "html": "<pre><code class=\"language-python\"><span class=\"line-number\">1</span>def quicksort(arr):\n<span class=\"line-number\">2</span>    if len(arr) &lt;= 1:\n<span class=\"line-number\">3</span>        return arr\n<span class=\"line-number\">4</span>    pivot = arr[0]\n<span class=\"line-number\">5</span>    return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_repl_prompts",
            "style": ">>> / ... REPL prompts",
            "html": "<pre><code class=\"language-python\">&gt;&gt;&gt; def quicksort(arr):\n...     if len(arr) &lt;= 1:\n...         return arr\n...     pivot = arr[0]\n...     return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
        {
            "id": "gutter_git_diff_numbered",
            "style": "  1 + code (git diff)",
            "html": "<pre><code class=\"language-python\">  1 + def quicksort(arr):\n  2 +     if len(arr) &lt;= 1:\n  3 +         return arr\n  4 +     pivot = arr[0]\n  5 +     return quicksort([x for x in arr[1:] if x &lt; pivot]) + [pivot]</code></pre>",
            "gold": clean_py,
            "lang": "python",
        },
    ]

    gutter_results = []
    for gf in gutter_fixtures:
        resp = client.call({"op": "math_code_probe", "html": gf["html"]})
        blocks = resp.get("code_blocks", [])
        extracted = "\n".join(p["text"] for b in blocks for p in b.get("parts", []))
        gold_lines = gf["gold"].splitlines()
        ext_lines = extracted.splitlines()
        line_seq_f1 = difflib.SequenceMatcher(None, gold_lines, ext_lines).ratio()
        try:
            ast.parse(extracted)
            ast_valid = True
        except SyntaxError:
            ast_valid = False
        gutter_results.append(
            {
                "id": gf["id"],
                "style": gf["style"],
                "exact_match": extracted == gf["gold"],
                "line_sequence_f1": round(line_seq_f1, 4),
                "python_ast_valid": ast_valid,
                "extracted_preview": extracted.splitlines()[0] if ext_lines else "",
            }
        )

    # Negative Controls (must NOT be corrupted by gutter stripping or DOM class substring matching!)
    negative_controls = [
        {
            "id": "neg_tsv_numeric_table",
            "desc": "TSV data block starting with '100\\t200\\t300'",
            "html": "<pre><code>100\t200\t300\n400\t500\t600</code></pre>",
            "expected": "100\t200\t300\n400\t500\t600",
        },
        {
            "id": "neg_pipe_markdown_table",
            "desc": "Markdown/ASCII pipe row starting with '1 | Alice | 95'",
            "html": "<pre><code>1 | Alice | 95\n2 | Bob   | 88</code></pre>",
            "expected": "1 | Alice | 95\n2 | Bob   | 88",
        },
        {
            "id": "neg_clipboard_class_code_span",
            "desc": "Code syntax span with class='token clipboard-api' stripped by class_attr.contains('clipboard')",
            "html": "<pre><code class=\"language-python\">def copy_data(text):\n    return <span class=\"token clipboard-api\">clipboard_write(text)</span></code></pre>",
            "expected": "def copy_data(text):\n    return clipboard_write(text)",
        },
        {
            "id": "neg_js_spread_operator",
            "desc": "JavaScript spread operator '...args' inside function",
            "html": "<pre><code class=\"language-js\">function merge(...args) {\n  return [...args];\n}</code></pre>",
            "expected": "function merge(...args) {\n  return [...args];\n}",
        },
        {
            "id": "neg_unbalanced_brace_in_string",
            "desc": "Code containing '{' inside string literal",
            "html": "<pre><code class=\"language-python\">def fmt():\n    s = \"open brace { in string\"\n    return s</code></pre>",
            "expected": "def fmt():\n    s = \"open brace { in string\"\n    return s",
        },
    ]

    neg_results = []
    for nc in negative_controls:
        resp = client.call({"op": "math_code_probe", "html": nc["html"]})
        blocks = resp.get("code_blocks", [])
        extracted = "\n".join(p["text"] for b in blocks for p in b.get("parts", []))
        corrupted = extracted != nc["expected"]
        neg_results.append(
            {
                "id": nc["id"],
                "desc": nc["desc"],
                "corrupted": corrupted,
                "expected": nc["expected"],
                "extracted": extracted,
            }
        )

    # Real WebMainBench code pages evaluation (line-break preservation & code block survival)
    wmb_code_pages = 0
    wmb_code_leg_lb_f1_sum = 0.0
    wmb_code_bt_lb_f1_sum = 0.0
    wmb_code_leg_lcs_f1_sum = 0.0
    wmb_code_bt_lcs_f1_sum = 0.0
    if webmainbench_path and os.path.exists(webmainbench_path):
        with open(webmainbench_path, "r", encoding="utf-8") as f:
            for line in f:
                if wmb_code_pages >= 50:
                    break
                row = json.loads(line)
                meta = row.get("meta") or {}
                if not meta.get("code"):
                    continue
                stripped = leakage.strip_cc_select(row.get("html") or "")
                san_html = sanitize_html_for_probe(stripped.html)
                if len(san_html.encode("utf-8")) > tier_a.CORE_HTML_BYTES:
                    continue
                gold_md = row.get("groundtruth_content") or ""
                gold_plain = tier_a.markdown_text(gold_md)
                resp = client.call(
                    {
                        "op": "evaluate",
                        "doc_id": str(row.get("track_id", "")),
                        "html": san_html,
                        "parser_mode": "both",
                        "boilerplate_policy": "cetd_entropy_v1",
                    }
                )
                leg_plain = tier_a.markdown_text(tier_a.render_markdown(resp.get("legacy", {}).get("chunks", [])))
                bt_plain = tier_a.markdown_text(tier_a.render_markdown(resp.get("breakthrough", {}).get("chunks", [])))
                ml = compute_order_and_linebreak_metrics(leg_plain, gold_plain)
                mb = compute_order_and_linebreak_metrics(bt_plain, gold_plain)
                wmb_code_pages += 1
                wmb_code_leg_lb_f1_sum += ml["line_break_f1"]
                wmb_code_bt_lb_f1_sum += mb["line_break_f1"]
                wmb_code_leg_lcs_f1_sum += ml["rouge_l_token_lcs_f1"]
                wmb_code_bt_lcs_f1_sum += mb["rouge_l_token_lcs_f1"]

    return {
        "gutter_styles_oracle": {
            "total_styles": len(gutter_results),
            "exact_match_rate": round(
                sum(1 for r in gutter_results if r["exact_match"]) / len(gutter_results), 4
            ),
            "python_ast_valid_rate": round(
                sum(1 for r in gutter_results if r["python_ast_valid"]) / len(gutter_results), 4
            ),
            "mean_line_sequence_f1": round(
                sum(r["line_sequence_f1"] for r in gutter_results) / len(gutter_results), 4
            ),
            "styles": gutter_results,
        },
        "negative_controls_oracle": {
            "total_controls": len(neg_results),
            "corruption_rate": round(
                sum(1 for r in neg_results if r["corrupted"]) / len(neg_results), 4
            ),
            "controls": neg_results,
        },
        "real_webmainbench_code_stratum_50_pages": {
            "pages_evaluated": wmb_code_pages,
            "legacy_rouge_l_f1": round(wmb_code_leg_lcs_f1_sum / max(1, wmb_code_pages), 4),
            "breakthrough_rouge_l_f1": round(wmb_code_bt_lcs_f1_sum / max(1, wmb_code_pages), 4),
            "legacy_line_break_f1": round(wmb_code_leg_lb_f1_sum / max(1, wmb_code_pages), 4),
            "breakthrough_line_break_f1": round(wmb_code_bt_lb_f1_sum / max(1, wmb_code_pages), 4),
        },
    }


def run_l1_6_ranker_and_contrastive_oracle(
    client: EvaluateV2Client,
    nq_v2_path: str,
) -> dict[str, Any]:
    # 1. Single-Char & Stopword-Bearing Technical Queries
    single_char_Probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Vitamin D deficiency",
            "chunks": [
                {"role": "paragraph", "text": "Vitamin A and Vitamin E are fat-soluble micronutrients with antioxidant properties."},
                {"role": "paragraph", "text": "Vitamin B12 deficiency causes megaloblastic anemia and peripheral neuropathy."},
                {"role": "paragraph", "text": "Vitamin D deficiency causes rickets in children and osteomalacia in adults."},
                {"role": "paragraph", "text": "Vitamin K deficiency impairs coagulation factor synthesis."},
            ],
        }
    )

    c_lang_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "C language Dennis Ritchie",
            "chunks": [
                {"role": "paragraph", "text": "Python is a high-level language created by Guido van Rossum."},
                {"role": "paragraph", "text": "The C programming language was developed by Dennis Ritchie at Bell Labs."},
            ],
        }
    )

    # 2. Deep Heading Stack (h1 -> h2 -> h3 -> h4 -> p): testing 3-slot FIFO shift register loss in compute_hierarchical_heading_boosts
    deep_heading_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Byzantine Fault Tolerance",
            "chunks": [
                {"role": "heading", "text": "Byzantine Fault Tolerance in Asynchronous Systems"},
                {"role": "heading", "text": "Section 1: Formal Model"},
                {"role": "heading", "text": "Subsection 1.1: Network Assumptions"},
                {"role": "heading", "text": "Note on Terminology"},
                {"role": "paragraph", "text": "Quorum intersection guarantees safety across rounds."},
            ],
        }
    )
    shallow_heading_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Byzantine Fault Tolerance",
            "chunks": [
                {"role": "heading", "text": "Byzantine Fault Tolerance in Asynchronous Systems"},
                {"role": "paragraph", "text": "Quorum intersection guarantees safety across rounds."},
            ],
        }
    )

    # 3. Link-Heavy Nav Penalty (links=[] in parse_html_breakthrough vs links=[0,1,2,3])
    link_penalty_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Byzantine consensus protocol",
            "chunks": [
                {
                    "role": "paragraph",
                    "text": "Byzantine consensus protocol overview and quorum safety proof.",
                    "links": [],
                },
                {
                    "role": "paragraph",
                    "text": "Byzantine consensus protocol overview and quorum safety proof.",
                    "links": [0, 1, 2, 3],
                },
            ],
        }
    )
    # Also verify that parse_html_breakthrough actually emits links=[] on <p> with 5 <a> tags!
    bt_link_html = "<main><p><a href='https://ex.org/1'>Byzantine</a> <a href='https://ex.org/2'>consensus</a> <a href='https://ex.org/3'>protocol</a> <a href='https://ex.org/4'>nav</a> <a href='https://ex.org/5'>menu</a></p></main>"
    bt_link_eval = client.call({"op": "evaluate", "html": bt_link_html, "parser_mode": "both"})
    leg_chunk_links = bt_link_eval["legacy"]["chunks"][0]["links"] if bt_link_eval["legacy"]["chunks"] else []
    bt_chunk_links = bt_link_eval["breakthrough"]["chunks"][0]["links"] if bt_link_eval["breakthrough"]["chunks"] else []

    # 4. SimHash-64 & MinHash-128 Sibling Table-Row Suppression (1991..1995 rows)
    # 4a: Short ungrounded rows (diverse team names -> low shingle overlap)
    sibling_rows_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Championship Final Winner",
            "limit": 5,
            "max_hamming": 5,
            "chunks": [
                {"role": "table_cell", "text": "[Season = 1991 | Event = Championship Final Winner]: Red Star Belgrade defeated Marseille"},
                {"role": "table_cell", "text": "[Season = 1992 | Event = Championship Final Winner]: Barcelona defeated Sampdoria"},
                {"role": "table_cell", "text": "[Season = 1993 | Event = Championship Final Winner]: Marseille defeated Milan"},
                {"role": "table_cell", "text": "[Season = 1994 | Event = Championship Final Winner]: Milan defeated Barcelona"},
                {"role": "table_cell", "text": "[Season = 1995 | Event = Championship Final Winner]: Ajax defeated Milan"},
            ],
        }
    )
    # 4b: Grounded table cells with shared schema headers (where minhash_jaccard_16 >= 0.85 or simhash_hamming <= 5 suppresses sibling years!)
    grounded_sibling_rows_probe = client.call(
        {
            "op": "rank_micro_probe",
            "query": "Championship Final Winner",
            "limit": 5,
            "max_hamming": 5,
            "chunks": [
                {"role": "table_cell", "text": "[H: League = National Basketball Association Championship Final Winner | Conference = Eastern Conference | Champion = Chicago Bulls | Finals MVP = Michael Jordan | Head Coach = Phil Jackson | Arena = Chicago Stadium | Broadcast = NBC Sports Television]: Season 1991"},
                {"role": "table_cell", "text": "[H: League = National Basketball Association Championship Final Winner | Conference = Eastern Conference | Champion = Chicago Bulls | Finals MVP = Michael Jordan | Head Coach = Phil Jackson | Arena = Chicago Stadium | Broadcast = NBC Sports Television]: Season 1992"},
                {"role": "table_cell", "text": "[H: League = National Basketball Association Championship Final Winner | Conference = Eastern Conference | Champion = Chicago Bulls | Finals MVP = Michael Jordan | Head Coach = Phil Jackson | Arena = Chicago Stadium | Broadcast = NBC Sports Television]: Season 1993"},
                {"role": "table_cell", "text": "[H: League = National Basketball Association Championship Final Winner | Conference = Eastern Conference | Champion = Chicago Bulls | Finals MVP = Michael Jordan | Head Coach = Phil Jackson | Arena = Chicago Stadium | Broadcast = NBC Sports Television]: Season 1996"},
                {"role": "table_cell", "text": "[H: League = National Basketball Association Championship Final Winner | Conference = Eastern Conference | Champion = Chicago Bulls | Finals MVP = Michael Jordan | Head Coach = Phil Jackson | Arena = Chicago Stadium | Broadcast = NBC Sports Television]: Season 1997"},
            ],
        }
    )

    # 5. AAE Byte-Budget & Sparse-Page Contrastive Zero-Score Fallback Micro-Probe
    aae_contract_resp = client.call({"op": "aae_contract_probe"})

    # 6. Contrastive Hard-Negative Mining Audit on 60 real Natural Questions pages
    total_contrastive_records = 0
    total_negatives_mined = 0
    zero_score_negatives = 0
    same_heading_false_negatives = 0
    gold_overlap_false_negatives = 0

    if os.path.exists(nq_v2_path):
        with gzip.open(nq_v2_path, "rt", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if idx >= 60:
                    break
                row = json.loads(line)
                html_str = sanitize_html_for_probe(row["html"])
                q = row["question"]
                sub_blocks = row.get("long_answer_sub_blocks", [])
                resp = client.call(
                    {
                        "op": "evaluate",
                        "doc_id": str(row["id"]),
                        "url": row["url"],
                        "html": html_str,
                        "query": q,
                        "parser_mode": "breakthrough",
                    }
                )
                crecs = resp.get("breakthrough", {}).get("contrastive_records", [])
                bt_chunks = resp.get("breakthrough", {}).get("chunks", [])
                chunk_by_dom_id = {c["dom_preorder_id"]: c for c in bt_chunks}
                for rec in crecs:
                    total_contrastive_records += 1
                    pos_sec = [s for s in rec.get("section_path", []) if s]
                    for neg in rec.get("hard_negatives", []):
                        total_negatives_mined += 1
                        if neg.get("score", 0.0) <= 0.0:
                            zero_score_negatives += 1
                        neg_chunk = chunk_by_dom_id.get(neg.get("pre_filter_dom_id"))
                        if neg_chunk is not None:
                            neg_sec = [s for s in neg_chunk.get("heading_stack", []) if s]
                            if pos_sec and neg_sec == pos_sec:
                                same_heading_false_negatives += 1
                        neg_txt = neg.get("text", "")
                        if any(sb in neg_txt or neg_txt in sb for sb in sub_blocks if len(sb) >= 20):
                            gold_overlap_false_negatives += 1

    return {
        "single_char_vitamin_d_rank1_correct": (single_char_Probe.get("bm25f_top") or [-1])[0] == 2,
        "single_char_c_lang_rank1_correct": (c_lang_probe.get("bm25f_top") or [-1])[0] == 1,
        "heading_inheritance_fifo_3slot_audit": {
            "shallow_h1_to_p_boost": round((shallow_heading_probe.get("heading_boosts") or [0, 0])[-1], 4),
            "deep_h1_h2_h3_h4_to_p_boost": round((deep_heading_probe.get("heading_boosts") or [0])[-1], 4),
            "h1_boost_lost_after_3_subheadings": (deep_heading_probe.get("heading_boosts") or [0])[-1] == 0.0,
        },
        "link_heavy_nav_penalty_audit": {
            "score_when_links_empty": round(dict(link_penalty_probe.get("bm25f_scores", [])).get(0, 0.0), 4),
            "score_when_links_populated": round(dict(link_penalty_probe.get("bm25f_scores", [])).get(1, 0.0), 4),
            "legacy_chunk_links_count_on_5_anchor_p": len(leg_chunk_links),
            "breakthrough_chunk_links_count_on_5_anchor_p": len(bt_chunk_links),
            "breakthrough_drops_chunk_links_disabling_penalty": len(bt_chunk_links) == 0,
        },
        "sibling_table_rows_simhash_audit": {
            "total_sibling_rows": 5,
            "short_rows_bm25_v1_selected_count": len(sibling_rows_probe.get("bm25_top", [])),
            "short_rows_bm25f_dual_selected_count": len(sibling_rows_probe.get("bm25f_top", [])),
            "grounded_schema_rows_bm25_v1_selected_count": len(grounded_sibling_rows_probe.get("bm25_top", [])),
            "grounded_schema_rows_bm25f_dual_selected_count": len(grounded_sibling_rows_probe.get("bm25f_top", [])),
            "grounded_schema_rows_suppressed_by_dedup": 5 - len(grounded_sibling_rows_probe.get("bm25f_top", [])),
            "grounded_pairwise_simhash_hamming": grounded_sibling_rows_probe.get("pairwise_simhash_hamming", []),
            "grounded_pairwise_minhash_jaccard": grounded_sibling_rows_probe.get("pairwise_minhash_jaccard", []),
        },
        "aae_contract_and_sparse_contrastive_probe": {
            "first_chunk_exceeds_budget_fails_entire_export": aae_contract_resp.get(
                "first_chunk_exceeds_budget_fails_entire_export", False
            ),
            "first_chunk_exceeds_budget_error": aae_contract_resp.get(
                "first_chunk_exceeds_budget_error", ""
            ),
            "sparse_page_contrastive_total_negatives": aae_contract_resp.get(
                "sparse_page_contrastive_total_negatives", 0
            ),
            "sparse_page_contrastive_zero_score_negatives": aae_contract_resp.get(
                "sparse_page_contrastive_zero_score_negatives", 0
            ),
        },
        "contrastive_mining_60_nq_pages_audit": {
            "total_contrastive_records": total_contrastive_records,
            "total_hard_negatives_mined": total_negatives_mined,
            "zero_score_unrelated_fallback_count": zero_score_negatives,
            "zero_score_unrelated_fallback_rate": round(
                zero_score_negatives / max(1, total_negatives_mined), 4
            ),
            "same_heading_section_false_negative_count": same_heading_false_negatives,
            "same_heading_section_false_negative_rate": round(
                same_heading_false_negatives / max(1, total_negatives_mined), 4
            ),
            "gold_answer_overlap_false_negative_count": gold_overlap_false_negatives,
            "gold_answer_overlap_false_negative_rate": round(
                gold_overlap_false_negatives / max(1, total_negatives_mined), 4
            ),
        },
    }


# ============================================================================
# Layer 2: Metamorphic & Counterfactual Perturbation Suite (`M1..M6`)
# ============================================================================


def run_layer2_metamorphic_suite(
    client: EvaluateV2Client,
    webmainbench_path: str,
    nq_v2_path: str,
    wikimedia_dir: str,
) -> dict[str, Any]:
    # Load 30 real WebMainBench pages for M1, M2, M4
    wmb_pages: list[dict[str, Any]] = []
    if os.path.exists(webmainbench_path):
        with open(webmainbench_path, "r", encoding="utf-8") as f:
            for line in f:
                if len(wmb_pages) >= 30:
                    break
                row = json.loads(line)
                h = sanitize_html_for_probe(row.get("html", ""))
                if len(h) > 500:
                    wmb_pages.append({"id": str(row.get("id", len(wmb_pages))), "html": h})

    # M1: <form id="aspnetForm"> wrapper around <body> children
    m1_orig_chunks = 0
    m1_pert_chunks = 0
    m1_complete_wipeout_pages = 0
    for p in wmb_pages:
        r_orig = client.call(
            {
                "op": "evaluate",
                "html": p["html"],
                "parser_mode": "breakthrough",
                "boilerplate_policy": "cetd_entropy_v1",
            }
        )
        pert_html = re.sub(
            r"(<body\b[^>]*>)(.*?)(</body>)",
            r'\1<form id="aspnetForm" method="post" action="./page.aspx">\2</form>\3',
            p["html"],
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if pert_html == p["html"]:
            pert_html = f'<html><body><form id="aspnetForm">{p["html"]}</form></body></html>'
        r_pert = client.call(
            {
                "op": "evaluate",
                "html": pert_html,
                "parser_mode": "breakthrough",
                "boilerplate_policy": "cetd_entropy_v1",
            }
        )
        c_orig = len(r_orig.get("breakthrough", {}).get("chunks", []))
        c_pert = len(r_pert.get("breakthrough", {}).get("chunks", []))
        m1_orig_chunks += c_orig
        m1_pert_chunks += c_pert
        if c_orig > 0 and c_pert == 0:
            m1_complete_wipeout_pages += 1

    # M2: Benign substring class injection (class="modal-logic related-work navigation-theory social-science") inside <body>
    m2_orig_chunks = 0
    m2_pert_chunks = 0
    for p in wmb_pages:
        r_orig = client.call(
            {
                "op": "evaluate",
                "html": p["html"],
                "parser_mode": "breakthrough",
                "boilerplate_policy": "cetd_entropy_v1",
            }
        )
        pert_html = re.sub(
            r"(<body\b[^>]*>)(.*?)(</body>)",
            r'\1<div class="modal-logic navigation-theory social-science" id="related-work">\2</div>\3',
            p["html"],
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if pert_html == p["html"]:
            pert_html = f'<html><body><div class="modal-logic navigation-theory social-science" id="related-work">{p["html"]}</div></body></html>'
        r_pert = client.call(
            {
                "op": "evaluate",
                "html": pert_html,
                "parser_mode": "breakthrough",
                "boilerplate_policy": "cetd_entropy_v1",
            }
        )
        m2_orig_chunks += len(r_orig.get("breakthrough", {}).get("chunks", []))
        m2_pert_chunks += len(r_pert.get("breakthrough", {}).get("chunks", []))

    # M3: Math representation swap (with <annotation> vs stripped <annotation> vs KaTeX visual duplicate span)
    m3_pairs = [
        (
            r"\frac{-b \pm \sqrt{b^{2} - 4ac}}{2a}",
            "<math><semantics><mrow><mfrac><mrow><mo>-</mo><mi>b</mi><mo>±</mo><msqrt><mrow><msup><mi>b</mi><mn>2</mn></msup><mo>-</mo><mn>4</mn><mi>a</mi><mi>c</mi></mrow></msqrt></mrow><mrow><mn>2</mn><mi>a</mi></mrow></mfrac></mrow><annotation encoding=\"application/x-tex\">\\frac{-b \\pm \\sqrt{b^{2} - 4ac}}{2a}</annotation></semantics></math>",
            "<math><mrow><mfrac><mrow><mo>-</mo><mi>b</mi><mo>±</mo><msqrt><mrow><msup><mi>b</mi><mn>2</mn></msup><mo>-</mo><mn>4</mn><mi>a</mi><mi>c</mi></mrow></msqrt></mrow><mrow><mn>2</mn><mi>a</mi></mrow></mfrac></mrow></math>",
            "<span class=\"katex\"><span class=\"katex-mathml\"><math><semantics><mrow><mfrac><mrow><mo>-</mo><mi>b</mi><mo>±</mo><msqrt><mrow><msup><mi>b</mi><mn>2</mn></msup><mo>-</mo><mn>4</mn><mi>a</mi><mi>c</mi></mrow></msqrt></mrow><mrow><mn>2</mn><mi>a</mi></mrow></mfrac></mrow><annotation encoding=\"application/x-tex\">\\frac{-b \\pm \\sqrt{b^{2} - 4ac}}{2a}</annotation></semantics></math></span><span class=\"katex-html\" aria-hidden=\"true\">visual duplicate noise</span></span>",
        ),
        (
            r"\sum_{i=1}^{n} x_{i}^{2}",
            "<math><semantics><mrow><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover><msubsup><mi>x</mi><mi>i</mi><mn>2</mn></msubsup></mrow><annotation encoding=\"application/x-tex\">\\sum_{i=1}^{n} x_{i}^{2}</annotation></semantics></math>",
            "<math><mrow><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover><msubsup><mi>x</mi><mi>i</mi><mn>2</mn></msubsup></mrow></math>",
            "<span class=\"katex\"><span class=\"katex-mathml\"><math><semantics><mrow><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover><msubsup><mi>x</mi><mi>i</mi><mn>2</mn></msubsup></mrow><annotation encoding=\"application/x-tex\">\\sum_{i=1}^{n} x_{i}^{2}</annotation></semantics></math></span><span class=\"katex-html\">∑ i=1 n x i 2</span></span>",
        ),
    ]
    m3_results = []
    for gold_tex, html_annot, html_no_annot, html_katex in m3_pairs:
        r1 = client.call({"op": "evaluate", "html": f"<main><p>{html_annot}</p></main>", "parser_mode": "breakthrough"})
        r2 = client.call({"op": "evaluate", "html": f"<main><p>{html_no_annot}</p></main>", "parser_mode": "breakthrough"})
        r3 = client.call({"op": "evaluate", "html": f"<main><p>{html_katex}</p></main>", "parser_mode": "breakthrough"})
        t1 = (r1.get("breakthrough", {}).get("chunks") or [{"text": ""}])[0]["text"]
        t2 = (r2.get("breakthrough", {}).get("chunks") or [{"text": ""}])[0]["text"]
        t3 = (r3.get("breakthrough", {}).get("chunks") or [{"text": ""}])[0]["text"]
        sim_12, ex_12 = math_ast_similarity(t1, t2)
        sim_13, ex_13 = math_ast_similarity(t1, t3)
        m3_results.append(
            {
                "gold_tex": gold_tex,
                "out_with_annotation": t1,
                "out_without_annotation": t2,
                "out_katex_wrapped": t3,
                "ast_sim_annot_vs_no_annot": round(sim_12, 4),
                "ast_exact_annot_vs_no_annot": ex_12,
                "ast_sim_annot_vs_katex": round(sim_13, 4),
                "katex_html_duplicate_suppressed": "visual duplicate noise" not in t3,
            }
        )

    # M4: >16KB <head> CSS/JSON-LD padding + benign "g-recaptcha" config string in <head> before <main>
    m4_items = []
    head_pad = "<head><script>window.recaptchaSiteKey = 'g-recaptcha-disabled';</script><style>" + ("/* css padding */ .cls { color: #123456; }\n" * 420) + "</style></head>"
    for idx, p in enumerate(wmb_pages[:25]):
        body_main = f"<html>{head_pad}<body><main><article>{p['html']}</article></main></body></html>"
        m4_items.append(
            {
                "id": f"m4_padded_{idx}",
                "category": "m4_16kb_head_padding",
                "status": 200,
                "headers": [],
                "html": body_main,
                "expected_blocked": False,
            }
        )
    m4_waf_resp = client.call({"op": "waf_probe", "items": m4_items})
    m4_rows = m4_waf_resp.get("results", [])
    m4_midstream_4k_false_aborts = sum(1 for r in m4_rows if r["midstream_4k_abort"])
    m4_midstream_16k_false_aborts = sum(1 for r in m4_rows if r["midstream_16k_abort"])
    m4_bandit_false_positives = sum(1 for r in m4_rows if r["bandit_blocked"])
    m4_net_false_positives = sum(1 for r in m4_rows if r["net_body_only_reason"] is not None)

    # M5: Interleaving <h4>Note 1</h4><h4>Note 2</h4><h4>Note 3</h4> between <h1> and target <p> on 30 NQ pages
    m5_orig_hit1 = 0
    m5_pert_hit1 = 0
    m5_count = 0
    if os.path.exists(nq_v2_path):
        with gzip.open(nq_v2_path, "rt", encoding="utf-8") as f:
            for line in enumerate(f):
                if m5_count >= 30:
                    break
                row = json.loads(line[1])
                subs = row.get("long_answer_sub_blocks", [])
                if not subs:
                    continue
                target_sub = subs[0]
                if len(target_sub) < 30:
                    continue
                m5_count += 1
                q = row["question"]
                q_toks = [w for w in re.findall(r"[A-Za-z0-9]+", q) if len(w) >= 3]
                distractor_term = q_toks[0] if q_toks else "overview"
                # Construct clean section where h1 carries query terms and target p relies on h1 heading boost vs a second section distractor
                h_orig = (
                    f"<main><h1>{q}</h1><p>{target_sub}</p>"
                    f"<h1>Unrelated Background</h1><p>General reference notes mentioning {distractor_term} in passing without the specific answer.</p></main>"
                )
                h_pert = (
                    f"<main><h1>{q}</h1><h2>Part A</h2><h3>Subsection B</h3><h4>Editorial Note</h4><p>{target_sub}</p>"
                    f"<h1>Unrelated Background</h1><p>General reference notes mentioning {distractor_term} in passing without the specific answer.</p></main>"
                )
                r_o = client.call({"op": "evaluate", "html": h_orig, "query": q, "parser_mode": "breakthrough"})
                r_p = client.call({"op": "evaluate", "html": h_pert, "query": q, "parser_mode": "breakthrough"})
                chunks_o = r_o.get("breakthrough", {}).get("chunks", [])
                chunks_p = r_p.get("breakthrough", {}).get("chunks", [])
                top_o = r_o.get("breakthrough", {}).get("ranking", {}).get("bm25f_dual_ranked_indices", [])
                top_p = r_p.get("breakthrough", {}).get("ranking", {}).get("bm25f_dual_ranked_indices", [])
                non_h_o = [i for i in top_o if i < len(chunks_o) and chunks_o[i]["role"] != "heading"]
                non_h_p = [i for i in top_p if i < len(chunks_p) and chunks_p[i]["role"] != "heading"]
                if non_h_o and target_sub[:25] in chunks_o[non_h_o[0]]["text"]:
                    m5_orig_hit1 += 1
                if non_h_p and target_sub[:25] in chunks_p[non_h_p[0]]["text"]:
                    m5_pert_hit1 += 1

    # M6: Sibling table-row distractors (1991..1995) testing SimHash-64 suppression of target row
    m6_queries = [
        ("1995 Championship Final Winner", "1995", "Ajax defeated Milan"),
        ("1994 Championship Final Winner", "1994", "Milan defeated Barcelona"),
        ("1993 Championship Final Winner", "1993", "Marseille defeated Milan"),
    ]
    m6_table_html = (
        "<main><table><thead><tr><th>Season</th><th>Event</th><th>Result</th></tr></thead><tbody>"
        "<tr><td>1991</td><td>Championship Final Winner</td><td>Red Star Belgrade defeated Marseille in Bari</td></tr>"
        "<tr><td>1992</td><td>Championship Final Winner</td><td>Barcelona defeated Sampdoria in London</td></tr>"
        "<tr><td>1993</td><td>Championship Final Winner</td><td>Marseille defeated Milan in Munich</td></tr>"
        "<tr><td>1994</td><td>Championship Final Winner</td><td>Milan defeated Barcelona in Athens</td></tr>"
        "<tr><td>1995</td><td>Championship Final Winner</td><td>Ajax defeated Milan in Vienna</td></tr>"
        "</tbody></table></main>"
    )
    m6_hits = 0
    for q_str, yr, ans in m6_queries:
        r_m6 = client.call({"op": "evaluate", "html": m6_table_html, "query": q_str, "parser_mode": "breakthrough"})
        chunks_m6 = r_m6.get("breakthrough", {}).get("chunks", [])
        top_m6 = r_m6.get("breakthrough", {}).get("ranking", {}).get("bm25f_dual_ranked_indices", [])
        top3_texts = " | ".join(chunks_m6[i]["text"] for i in top_m6[:3] if i < len(chunks_m6))
        if ans in top3_texts:
            m6_hits += 1

    return {
        "M1_aspnet_form_wrapper": {
            "pages_tested": len(wmb_pages),
            "original_total_chunks": m1_orig_chunks,
            "perturbed_total_chunks": m1_pert_chunks,
            "chunk_survival_rate": round(m1_pert_chunks / max(1, m1_orig_chunks), 4),
            "complete_wipeout_page_rate": round(m1_complete_wipeout_pages / max(1, len(wmb_pages)), 4),
        },
        "M2_benign_substring_class_injection": {
            "pages_tested": len(wmb_pages),
            "original_total_chunks": m2_orig_chunks,
            "perturbed_total_chunks": m2_pert_chunks,
            "chunk_survival_rate": round(m2_pert_chunks / max(1, m2_orig_chunks), 4),
        },
        "M3_math_representation_swap": {
            "pairs_tested": len(m3_results),
            "results": m3_results,
        },
        "M4_16kb_head_padding_with_benign_recaptcha_string": {
            "pages_tested": len(m4_rows),
            "midstream_4k_false_abort_rate": round(m4_midstream_4k_false_aborts / max(1, len(m4_rows)), 4),
            "midstream_16k_false_abort_rate": round(m4_midstream_16k_false_aborts / max(1, len(m4_rows)), 4),
            "bandit_soft_block_false_positive_rate": round(m4_bandit_false_positives / max(1, len(m4_rows)), 4),
            "network_full_body_false_positive_rate": round(m4_net_false_positives / max(1, len(m4_rows)), 4),
        },
        "M5_heading_stack_subheadings_interleaving": {
            "queries_tested": m5_count,
            "original_hit_at_1": round(m5_orig_hit1 / max(1, m5_count), 4),
            "perturbed_hit_at_1": round(m5_pert_hit1 / max(1, m5_count), 4),
        },
        "M6_sibling_table_row_distractors": {
            "queries_tested": len(m6_queries),
            "top3_hit_rate": round(m6_hits / len(m6_queries), 4),
        },
    }


# ============================================================================
# Layer 4: Cloud-Realistic Systems, WAF, Bandit & Cache Heap Audit
# ============================================================================


def run_layer4_systems_suite(
    client: EvaluateV2Client,
    warc_dir: str,
    webmainbench_path: str,
) -> dict[str, Any]:
    # ------------------------------------------------------------------------
    # L4.1: 500+ Page Real WARC + Hard-Negative WAF/Soft-Block Corpus
    # ------------------------------------------------------------------------
    waf_items: list[dict[str, Any]] = []
    sample_warc = os.path.join(warc_dir, "CC-MAIN-20261015222031-20261016012031-00106.warc.gz")
    if not os.path.exists(sample_warc):
        warc_matches = sorted(glob.glob(os.path.join(warc_dir, "*-00106.warc.gz")))
        if warc_matches:
            sample_warc = warc_matches[0]
    if os.path.exists(sample_warc):
        for rec in iter_warc_records(sample_warc):
            if len(waf_items) >= 350:
                break
            raw_html, _, _, _ = decode_html_bytes(rec.body, rec.http_headers)
            san_html = sanitize_html_for_probe(raw_html)
            if len(san_html) > 250_000:
                continue
            hdrs = [[k, v] for k, v in rec.http_headers.items()]
            # Ground-truth labeling on real WARC records:
            low = san_html[:16384].lower()
            is_real_block = (
                rec.http_status in (403, 429, 503)
                or len(san_html.strip()) == 0
                or "<title>just a moment...</title>" in low
                or "attention required! | cloudflare" in low
                or "cf-chl-bypass" in low
                or "/_incapsula_resource" in low
            )
            waf_items.append(
                {
                    "id": f"warc_{rec.member_offset}",
                    "category": "real_warc_http_response",
                    "status": rec.http_status,
                    "headers": hdrs,
                    "html": san_html,
                    "expected_blocked": is_real_block,
                }
            )

    # Add 160 structured True-Positive Challenge variants & Hard-Negative Benign pages (total >= 510 items)
    # A. Standard 8-vendor challenges (40 items)
    vendor_templates = [
        ("cloudflare_std", 200, [["cf-mitigated", "challenge"]], "<html><head><title>Just a moment...</title></head><body><div class='cf-turnstile'>Verify you are human</div></body></html>", True),
        ("datadome_std", 200, [["x-datadome", "protected"]], "<html><body><script src='https://geo.captcha-delivery.com/captcha/?initialCid=dd_cid'></script></body></html>", True),
        ("perimeterx_std", 200, [], "<html><body><div id='px-captcha'>Press and hold to confirm you are human</div><script src='https://block.perimeterx.net/px.js'></script></body></html>", True),
        ("akamai_std", 200, [["set-cookie", "_abck=123~0~; bm_sz=456"]], "<html><body><script>var _abck='1'; var bm_sz='2';</script><p>Access Denied - edgesuite.net</p></body></html>", True),
        ("imperva_std", 200, [["set-cookie", "visid_incap_1=a; incap_ses_2=b"]], "<html><body><iframe src='/_Incapsula_Resource?CWUDNSAI=23'></iframe><p>Incapsula incident ID: 9912</p></body></html>", True),
        ("kasada_std", 200, [["x-kpsdk-ct", "019283"]], "<html><body><script src='/149e9513-01fa-4fb0-aad4-566afd725d1b/ips.js'></script></body></html>", True),
        ("awswaf_std", 200, [["x-amzn-waf-action", "challenge"]], "<html><body><script src='https://captcha.awswaf.com/challenge.js'></script><div>aws-waf-token</div></body></html>", True),
        ("ddos_guard_std", 200, [], "<html><head><title>DDoS-Guard</title></head><body><div>DDoS-Guard check in progress</div></body></html>", True),
    ]
    for rep_i in range(5):
        for v_name, st, hdrs, body, exp in vendor_templates:
            waf_items.append(
                {
                    "id": f"{v_name}_{rep_i}",
                    "category": "true_positive_standard_8_vendor",
                    "status": st,
                    "headers": hdrs,
                    "html": body + f"<!-- nonce {rep_i} -->",
                    "expected_blocked": exp,
                }
            )

    # B. Header-only challenges (20 items) -> exposes SoftBlockDetector header blindness!
    header_only_templates = [
        ("kasada_header_only", 200, [["x-kpsdk-ct", "tok123"]], "<html><body><p>Please wait while we verify your browser session...</p></body></html>", True),
        ("awswaf_header_only", 200, [["x-amzn-waf-action", "captcha"]], "<html><body><p>Verification required before proceeding to resource.</p></body></html>", True),
        ("akamai_cookie_only", 200, [["set-cookie", "ak_bmsc=998877"]], "<html><body><p>Verifying session parameters...</p></body></html>", True),
        ("datadome_header_only", 200, [["x-datadome", "protected"]], "<html><body><p>Checking browser environment...</p></body></html>", True),
    ]
    for rep_i in range(5):
        for v_name, st, hdrs, body, exp in header_only_templates:
            waf_items.append(
                {
                    "id": f"{v_name}_{rep_i}",
                    "category": "true_positive_header_only_challenge",
                    "status": st,
                    "headers": hdrs,
                    "html": body,
                    "expected_blocked": exp,
                }
            )

    # C. Challenges > 65KB with inline SVG/base64 assets or <article> wrapper (20 items) -> exposes network.rs#L1028 & L1037 bypasses!
    padding_68k = "<!-- " + ("A" * 68_000) + " -->"
    for rep_i in range(10):
        waf_items.append(
            {
                "id": f"oversize_68kb_cloudflare_{rep_i}",
                "category": "true_positive_oversize_68kb_challenge",
                "status": 200,
                "headers": [],
                "html": f"<html><head><title>Just a moment...</title></head><body><div class='cf-turnstile'>Challenge</div>{padding_68k}</body></html>",
                "expected_blocked": True,
            }
        )
        waf_items.append(
            {
                "id": f"article_wrapped_20kb_cloudflare_{rep_i}",
                "category": "true_positive_article_wrapped_challenge",
                "status": 200,
                "headers": [],
                "html": f"<html><head><title>Just a moment...</title></head><body><article><div class='cf-turnstile'>Verify you are human</div>{'<!-- pad -->' * 1600}</article></body></html>",
                "expected_blocked": True,
            }
        )

    # D. Hard Negatives: Legitimate pages with >16KB <head> + "g-recaptcha" in <head> before <main> (40 items)
    #    and security tutorials discussing WAF signatures without <main> tag (40 items)
    head_18kb_recaptcha = (
        "<head><script>var recaptchaCfg = 'g-recaptcha';</script><style>"
        + (".nav-item { margin: 4px; padding: 2px; }\n" * 420)
        + "</style></head>"
    )
    for rep_i in range(40):
        waf_items.append(
            {
                "id": f"hard_neg_18kb_head_before_main_{rep_i}",
                "category": "hard_negative_18kb_head_before_main",
                "status": 200,
                "headers": [],
                "html": f"<html>{head_18kb_recaptcha}<body><main><article><h1>Distributed Systems Lecture {rep_i}</h1><p>Paxos and Raft guarantee linearizable state machine replication across faulty network partitions.</p></article></main></body></html>",
                "expected_blocked": False,
            }
        )
        waf_items.append(
            {
                "id": f"hard_neg_div_layout_security_article_{rep_i}",
                "category": "hard_negative_div_layout_waf_tutorial",
                "status": 200,
                "headers": [],
                "html": f"<html><body><div id='content'><h1>How to Configure Cloudflare Turnstile and reCAPTCHA {rep_i}</h1><p>When migrating from <code>g-recaptcha</code> or <code>h-captcha</code> to <code>cf-turnstile</code>, ensure your server validates tokens at <code>/cdn-cgi/challenge-platform/</code> and checks <code>aws-waf-token</code> headers.</p></div></body></html>",
                "expected_blocked": False,
            }
        )

    waf_resp = client.call({"op": "waf_probe", "items": waf_items})
    waf_rows = waf_resp.get("results", [])

    def compute_detector_metrics(rows: list[dict[str, Any]], pred_fn: Any) -> dict[str, Any]:
        tp = fp = tn = fn = 0
        for r in rows:
            exp = bool(r["expected_blocked"])
            pred = bool(pred_fn(r))
            if exp and pred:
                tp += 1
            elif not exp and pred:
                fp += 1
            elif not exp and not pred:
                tn += 1
            else:
                fn += 1
        prec = tp / max(1, tp + fp)
        rec = tp / max(1, tp + fn)
        f1 = (2 * prec * rec) / max(1e-9, prec + rec)
        fpr = fp / max(1, fp + tn)
        return {
            "tp": tp,
            "fp": fp,
            "tn": tn,
            "fn": fn,
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "fpr": round(fpr, 4),
        }

    waf_summary_by_cat = {}
    for cat in sorted({r["category"] for r in waf_rows}):
        sub = [r for r in waf_rows if r["category"] == cat]
        waf_summary_by_cat[cat] = {
            "count": len(sub),
            "network_body_only": compute_detector_metrics(sub, lambda r: r["net_body_only_reason"] is not None),
            "network_with_headers": compute_detector_metrics(sub, lambda r: r["net_with_headers_reason"] is not None),
            "network_with_midstream_4k_abort": compute_detector_metrics(
                sub, lambda r: r["midstream_4k_abort"] or (r["net_with_headers_reason"] is not None)
            ),
            "bandit_soft_block_detector": compute_detector_metrics(sub, lambda r: r["bandit_blocked"]),
            "orchestrator_combined": compute_detector_metrics(sub, lambda r: r["orchestrator_blocked"]),
        }

    # ------------------------------------------------------------------------
    # L4.2: De-Confounded 2^4 = 16 Factorial ProxyPoolRouter Ablation & 50K-Domain Memory Audit
    # ------------------------------------------------------------------------
    bandit_resp = client.call({"op": "bandit_factorial_probe", "seeds": 16, "steps_per_seed": 300})
    f16_cells = bandit_resp.get("factorial_16_ablation", [])

    def factor_main_effect(key: str) -> dict[str, float]:
        on_cells = [c for c in f16_cells if c.get(key)]
        off_cells = [c for c in f16_cells if not c.get(key)]
        u_on = sum(c["usable_rate"] for c in on_cells) / max(1, len(on_cells))
        u_off = sum(c["usable_rate"] for c in off_cells) / max(1, len(off_cells))
        p99_on = sum(c["latency_p99_ms"] for c in on_cells) / max(1, len(on_cells))
        p99_off = sum(c["latency_p99_ms"] for c in off_cells) / max(1, len(off_cells))
        return {
            "usable_rate_when_on": round(u_on, 4),
            "usable_rate_when_off": round(u_off, 4),
            "delta_usable_rate": round(u_on - u_off, 4),
            "delta_latency_p99_ms": round(p99_on - p99_off, 2),
        }

    # ------------------------------------------------------------------------
    # L4.3: SourceCache + PreparedRank Heap Accounting Audit & WARC Tail-Latency (p50/p95/p99/p99.9)
    # ------------------------------------------------------------------------
    heap_docs = []
    if os.path.exists(webmainbench_path):
        with open(webmainbench_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if len(heap_docs) >= 120:
                    break
                row = json.loads(line)
                h = sanitize_html_for_probe(row.get("html", ""))
                if len(h) > 200:
                    heap_docs.append({"id": f"wmb_{idx}", "html": h})

    heap_resp = client.call({"op": "cache_heap_probe", "docs": heap_docs})
    heap_rows = heap_resp.get("results", [])

    total_q1_rep = sum(r["orchestrator_q1_reported_bytes"] for r in heap_rows)
    total_q2_rep = sum(r["orchestrator_q2_reported_bytes"] for r in heap_rows)
    total_true_q1 = sum(r["true_q1_in_memory_bytes"] for r in heap_rows)
    total_true_q2 = sum(r["true_q2_in_memory_bytes"] for r in heap_rows)
    total_untracked_prep = sum(r["untracked_q2_prepared_rank_delta_bytes"] for r in heap_rows)
    total_untracked_bt = sum(r["untracked_breakthrough_rich_bytes"] for r in heap_rows)

    leg_us = sorted(r["legacy_parse_us"] for r in heap_rows)
    bt_us = sorted(r["breakthrough_parse_us"] for r in heap_rows)

    def pct(arr: list[int], q: float) -> float:
        if not arr:
            return 0.0
        idx = min(len(arr) - 1, int(math.floor(len(arr) * q)))
        return round(arr[idx] / 1000.0, 3)

    return {
        "L4_1_waf_soft_block_510_corpus": {
            "total_items": len(waf_rows),
            "overall_network_body_only": compute_detector_metrics(
                waf_rows, lambda r: r["net_body_only_reason"] is not None
            ),
            "overall_network_with_headers": compute_detector_metrics(
                waf_rows, lambda r: r["net_with_headers_reason"] is not None
            ),
            "overall_network_with_midstream_4k_abort": compute_detector_metrics(
                waf_rows, lambda r: r["midstream_4k_abort"] or (r["net_with_headers_reason"] is not None)
            ),
            "overall_bandit_soft_block_detector": compute_detector_metrics(
                waf_rows, lambda r: r["bandit_blocked"]
            ),
            "overall_orchestrator_combined": compute_detector_metrics(
                waf_rows, lambda r: r["orchestrator_blocked"]
            ),
            "by_category": waf_summary_by_cat,
        },
        "L4_2_deconfounded_bandit_and_50k_domain_growth": {
            "router_5_algorithms_by_regime": bandit_resp.get("router_algorithm_comparison", []),
            "factorial_16_main_effects": {
                "factored_net_waf": factor_main_effect("factored_net_waf"),
                "cusum_reset": factor_main_effect("cusum_reset"),
                "ebayes_shrinkage": factor_main_effect("ebayes_shrinkage"),
                "quarantine_cooldown": factor_main_effect("quarantine_cooldown"),
            },
            "factorial_16_cells": f16_cells,
            "domain_50k_memory_growth": bandit_resp.get("domain_50k_memory_growth", []),
        },
        "L4_3_source_cache_heap_audit_and_tail_latency": {
            "docs_audited": len(heap_rows),
            "total_orchestrator_q1_reported_bytes": total_q1_rep,
            "total_orchestrator_q2_reported_bytes": total_q2_rep,
            "total_true_q1_in_memory_bytes": total_true_q1,
            "total_true_q2_in_memory_bytes": total_true_q2,
            "total_untracked_q2_prepared_rank_bytes": total_untracked_prep,
            "untracked_prepared_rank_fraction_of_true_q2": round(
                total_untracked_prep / max(1, total_true_q2), 4
            ),
            "orchestrator_q2_updates_retained_bytes_on_lazy_init": total_q2_rep != total_q1_rep,
            "total_untracked_breakthrough_rich_bytes": total_untracked_bt,
            "latency_ms_quantiles": {
                "legacy": {
                    "p50_ms": pct(leg_us, 0.50),
                    "p95_ms": pct(leg_us, 0.95),
                    "p99_ms": pct(leg_us, 0.99),
                    "p99_9_ms": pct(leg_us, 0.999),
                },
                "breakthrough": {
                    "p50_ms": pct(bt_us, 0.50),
                    "p95_ms": pct(bt_us, 0.95),
                    "p99_ms": pct(bt_us, 0.99),
                    "p99_9_ms": pct(bt_us, 0.999),
                },
            },
        },
    }


# ============================================================================
# Layer 3: Stage-Decoupled Attribution & Schema-Aligned Real Corpora (`Tier-A v2` & `Tier-B v2`)
# ============================================================================


def _fast_token_lcs_len(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    common = set(a) & set(b)
    if not common:
        return 0
    fa = [x for x in a if x in common]
    fb = [x for x in b if x in common]
    if len(fa) <= len(fb):
        short, long_seq = fa, fb
    else:
        short, long_seq = fb, fa
    m = len(short)
    if m == 0:
        return 0
    pmask: dict[str, int] = {}
    for idx, tok in enumerate(short):
        pmask[tok] = pmask.get(tok, 0) | (1 << idx)
    mask_all = (1 << m) - 1
    v = mask_all
    for tok in long_seq:
        p = pmask.get(tok, 0)
        if p:
            u = v & p
            v = ((v + u) | (v ^ u)) & mask_all
    return m - v.bit_count()


def compute_order_and_linebreak_metrics(pred_text: str, gold_text: str) -> dict[str, float]:
    word_re = re.compile(r"\w+")
    p_toks = word_re.findall(pred_text.lower())
    g_toks = word_re.findall(gold_text.lower())
    if not g_toks or not p_toks:
        return {"bag_of_words_f1": 0.0, "rouge_l_token_lcs_f1": 0.0, "line_break_f1": 0.0, "gold_recall": 0.0}

    from collections import Counter

    pc = Counter(p_toks)
    gc = Counter(g_toks)
    overlap = sum((pc & gc).values())
    bow_prec = overlap / len(p_toks)
    bow_rec = overlap / len(g_toks)
    bow_f1 = (2 * bow_prec * bow_rec) / max(1e-9, bow_prec + bow_rec)

    lcs = _fast_token_lcs_len(p_toks, g_toks)
    lcs_prec = lcs / max(1, len(p_toks))
    lcs_rec = lcs / max(1, len(g_toks))
    rouge_l_f1 = (2 * lcs_prec * lcs_rec) / max(1e-9, lcs_prec + lcs_rec)

    # Line-break / block-boundary preservation F1:
    # Measure bigrams of (last_token_of_line_i, first_token_of_line_i+1) across non-empty lines
    def line_boundary_pairs(txt: str) -> Counter:
        lines_toks = [word_re.findall(ln.lower()) for ln in txt.splitlines()]
        lines_toks = [lt for lt in lines_toks if lt]
        pairs = [(lines_toks[i][-1], lines_toks[i + 1][0]) for i in range(len(lines_toks) - 1)]
        return Counter(pairs)

    p_lb = line_boundary_pairs(pred_text)
    g_lb = line_boundary_pairs(gold_text)
    tot_p_lb = sum(p_lb.values())
    tot_g_lb = sum(g_lb.values())
    if tot_g_lb == 0 or tot_p_lb == 0:
        lb_f1 = 0.0
    else:
        lb_ov = sum((p_lb & g_lb).values())
        lb_f1 = (2.0 * lb_ov) / (tot_p_lb + tot_g_lb)

    return {
        "bag_of_words_f1": round(bow_f1, 6),
        "rouge_l_token_lcs_f1": round(rouge_l_f1, 6),
        "line_break_f1": round(lb_f1, 6),
        "gold_recall": round(bow_rec, 6),
    }


def parse_markdown_pipe_tables(md: str) -> list[list[list[str]]]:
    """Extract 2D cell grids from Markdown pipe tables in WebMainBench groundtruth."""
    tables: list[list[list[str]]] = []
    cur: list[list[str]] = []
    sep_re = re.compile(r"^[\s|:\-]+$")
    for raw_line in (md or "").splitlines():
        line = raw_line.strip()
        if line.startswith("|") and line.endswith("|") and line.count("|") >= 2:
            if sep_re.match(line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if any(cells):
                cur.append(cells)
        else:
            if len(cur) >= 2:
                tables.append(cur)
            cur = []
    if len(cur) >= 2:
        tables.append(cur)
    return tables


def score_wmb_table_teds(gold_md: str, html_str: str, bt_tables: list[dict[str, Any]], leg_text: str) -> dict[str, float] | None:
    """Compute 2D Grid / Tree-Edit-Distance Structural Similarity (table_teds) on WebMainBench table pages."""
    md_grids = parse_markdown_pipe_tables(gold_md)
    oracle_html_tabs = extract_whatwg_oracle_tables(html_str)
    gold_cells_2d: list[tuple[int, int, str]] = []
    if md_grids:
        for g in md_grids:
            for r_i, row in enumerate(g):
                for c_i, val in enumerate(row):
                    if val:
                        gold_cells_2d.append((r_i, c_i, val.lower()))
    elif oracle_html_tabs:
        primary = oracle_html_tabs[-1]
        for c in primary["cells"]:
            if c["text"].strip():
                gold_cells_2d.append((c["row_idx"], c["col_idx"], c["text"].strip().lower()))
    if not gold_cells_2d:
        return None

    pred_cells_2d: dict[tuple[int, int], str] = {}
    for t in bt_tables:
        for c in t.get("grounded_cells", []):
            txt = c.get("text", "").strip().lower()
            if txt and (c["row_idx"], c["col_idx"]) not in pred_cells_2d:
                pred_cells_2d[(c["row_idx"], c["col_idx"])] = txt

    word_re = re.compile(r"\w+")
    coord_sim_sum = 0.0
    content_hit_sum = 0.0
    all_pred_text = " ".join(pred_cells_2d.values()).lower()
    for r_i, c_i, g_txt in gold_cells_2d:
        g_toks = set(word_re.findall(g_txt))
        if not g_toks:
            continue
        # 1. Exact coordinate (r, c) structural tree match
        p_cell = pred_cells_2d.get((r_i, c_i), "")
        p_toks = set(word_re.findall(p_cell))
        if p_toks:
            ov = len(g_toks & p_toks) / len(g_toks)
            coord_sim_sum += ov
        # 2. Any-cell content match
        if any(t in all_pred_text for t in g_toks):
            content_hit_sum += 1.0

    n = max(1, len(gold_cells_2d))
    # Combine tree coordinate similarity and cell content survival into TEDS in [0, 1]
    teds_bt = 0.6 * (coord_sim_sum / n) + 0.4 * (content_hit_sum / n)
    # Legacy parser emits flat text with 0 table grid structure (coord_sim = 0.0)
    leg_low = leg_text.lower()
    leg_content = sum(
        1.0 for _, _, g_txt in gold_cells_2d if any(t in leg_low for t in word_re.findall(g_txt))
    ) / n
    teds_leg = 0.4 * leg_content
    return {
        "breakthrough_table_teds": round(teds_bt, 6),
        "breakthrough_coord_exact_sim": round(coord_sim_sum / n, 6),
        "legacy_table_teds_flat_text": round(teds_leg, 6),
    }


def run_layer3_tier_a_v2(
    client: EvaluateV2Client,
    datasets_dir: str,
    wmb_path: str,
    nq_v2_path: str,
) -> dict[str, Any]:
    import tier_a
    import wceb
    import leakage

    legacy_report_path = os.path.join(datasets_dir, "reports/tier-a-1f0cfc8/tier_a_report.json")
    legacy_tier_a = {}
    if os.path.exists(legacy_report_path):
        with open(legacy_report_path, "r", encoding="utf-8") as f:
            legacy_tier_a = json.load(f)

    # ------------------------------------------------------------------------
    # 1. WebMainBench-545 Full v2 Evaluation (All 545 pages)
    # ------------------------------------------------------------------------
    wmb_records = []
    if os.path.exists(wmb_path):
        with open(wmb_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    wmb_records.append(json.loads(line))

    wmb_leg_metrics: list[dict[str, float]] = []
    wmb_bt_metrics: list[dict[str, float]] = []
    wmb_table_teds_rows: list[dict[str, float]] = []
    wmb_currency_false_positive_pages = 0
    wmb_true_equation_pages = 0
    wmb_dom_math_ast_sims: list[float] = []
    wmb_legacy_regex_math_sims_on_true_eq: list[float] = []
    wmb_eq_unstripped_f1s: list[float] = []
    wmb_eq_stripped_f1s: list[float] = []

    waterfall_totals = {
        "docs": 0,
        "s1_raw_bytes": 0,
        "s1_nfc_bytes": 0,
        "s1_nfc_changed_docs": 0,
        "s2_dom_all_chars": 0,
        "s2_bt_full_dom_chars": 0,
        "s3_landmark_bt_chars": 0,
        "s3_landmark_bt_chunks": 0,
        "s4_cap512_bt_chars": 0,
        "s4_cap512_bt_chunks": 0,
        "s4_chars_lost_to_512_word_cap": 0,
        "s4_docs_truncated_by_512_word_cap": 0,
        "s5_cetd_legacy_chars": 0,
        "s5_cetd_bt_chars": 0,
        "s5_bt_cetd_entropy_identical_to_s4_docs": 0,
        "s6_structured_table_count": 0,
        "r1_html_gold_recall_sum": 0.0,
        "r2_dom_all_gold_recall_sum": 0.0,
        "r3_landmark_bt_gold_recall_sum": 0.0,
        "r4_cap512_bt_gold_recall_sum": 0.0,
        "r5_cetd_legacy_gold_recall_sum": 0.0,
        "r5_cetd_breakthrough_gold_recall_sum": 0.0,
    }

    word_re = re.compile(r"\w+")
    for rec in wmb_records:
        meta = rec.get("meta") or {}
        stripped = leakage.strip_cc_select(rec.get("html") or "")
        san_html = sanitize_html_for_probe(stripped.html)
        if len(san_html.encode("utf-8")) > tier_a.CORE_HTML_BYTES:
            continue
        url = rec.get("url") if str(rec.get("url", "")).startswith("http") else "https://wmb.invalid/"
        gold_md = rec.get("groundtruth_content") or ""
        gold_plain = tier_a.markdown_text(gold_md)

        # Check legacy _TEX_SPAN currency false-positive defect on non-equation pages
        has_meta_eq = bool(meta.get("equation"))
        legacy_tex_matches = [a or b for a, b in tier_a._TEX_SPAN.findall(gold_md)]
        if not has_meta_eq and legacy_tex_matches:
            wmb_currency_false_positive_pages += 1
        if has_meta_eq:
            wmb_true_equation_pages += 1

        resp = client.call(
            {
                "op": "evaluate",
                "doc_id": str(rec.get("track_id", "")),
                "url": url,
                "html": san_html,
                "parser_mode": "both",
                "boilerplate_policy": "cetd_entropy_v1",
                "emit_stage_trace": True,
            }
        )
        leg_chunks = resp.get("legacy", {}).get("chunks", [])
        bt_chunks = resp.get("breakthrough", {}).get("chunks", [])
        bt_tables = resp.get("breakthrough", {}).get("tables", [])
        st = resp.get("stage_trace") or {}

        leg_md = tier_a.render_markdown(leg_chunks)
        bt_md = tier_a.render_markdown(bt_chunks)
        leg_plain = tier_a.markdown_text(leg_md)
        bt_plain = tier_a.markdown_text(bt_md)

        m_leg = compute_order_and_linebreak_metrics(leg_plain, gold_plain)
        m_bt = compute_order_and_linebreak_metrics(bt_plain, gold_plain)
        wmb_leg_metrics.append(m_leg)
        wmb_bt_metrics.append(m_bt)

        # Stage waterfall accumulation from nested stage_trace
        s1_obj = st.get("s1_charset_nfc") or {}
        s2_obj = st.get("s2_dom_shadow_foster") or {}
        s3_obj = st.get("s3_landmark_attr_prune") or {}
        s4_obj = st.get("s4_block_segment_cap") or {}
        s5_obj = st.get("s5_cetd_prune") or {}

        s2_dom_txt = s2_obj.get("dom_all_text", "")
        s2_bt_txt = s2_obj.get("bt_full_dom_text", "")
        s3_bt_txt = s3_obj.get("bt_text", "")
        s4_bt_txt = s4_obj.get("bt_text", "")
        s5_leg_txt = s5_obj.get("legacy_text", "")
        s5_bt_txt = s5_obj.get("bt_text", "")

        waterfall_totals["docs"] += 1
        waterfall_totals["s1_raw_bytes"] += s1_obj.get("raw_bytes", 0)
        waterfall_totals["s1_nfc_bytes"] += s1_obj.get("nfc_bytes", 0)
        if s1_obj.get("nfc_changed"):
            waterfall_totals["s1_nfc_changed_docs"] += 1
        waterfall_totals["s2_dom_all_chars"] += len(s2_dom_txt)
        waterfall_totals["s2_bt_full_dom_chars"] += len(s2_bt_txt)
        waterfall_totals["s3_landmark_bt_chars"] += len(s3_bt_txt)
        waterfall_totals["s3_landmark_bt_chunks"] += s3_obj.get("bt_chunks", 0)
        waterfall_totals["s4_cap512_bt_chars"] += len(s4_bt_txt)
        waterfall_totals["s4_cap512_bt_chunks"] += s4_obj.get("bt_chunks", 0)
        chars_lost_s4 = max(0, len(s3_bt_txt) - len(s4_bt_txt))
        waterfall_totals["s4_chars_lost_to_512_word_cap"] += chars_lost_s4
        if chars_lost_s4 > 0:
            waterfall_totals["s4_docs_truncated_by_512_word_cap"] += 1
        waterfall_totals["s5_cetd_legacy_chars"] += len(s5_leg_txt)
        waterfall_totals["s5_cetd_bt_chars"] += len(s5_bt_txt)
        if s5_obj.get("bt_cetd_entropy_identical_to_s4"):
            waterfall_totals["s5_bt_cetd_entropy_identical_to_s4_docs"] += 1
        waterfall_totals["s6_structured_table_count"] += len(bt_tables)

        g_tok_set = set(word_re.findall(gold_plain.lower()))
        if g_tok_set:
            h_tok_set = set(word_re.findall(san_html.lower()))
            s2_tok_set = set(word_re.findall(s2_dom_txt.lower()))
            s3_tok_set = set(word_re.findall(s3_bt_txt.lower()))
            s4_tok_set = set(word_re.findall(s4_bt_txt.lower()))
            waterfall_totals["r1_html_gold_recall_sum"] += len(g_tok_set & h_tok_set) / len(g_tok_set)
            waterfall_totals["r2_dom_all_gold_recall_sum"] += len(g_tok_set & s2_tok_set) / len(g_tok_set)
            waterfall_totals["r3_landmark_bt_gold_recall_sum"] += len(g_tok_set & s3_tok_set) / len(g_tok_set)
            waterfall_totals["r4_cap512_bt_gold_recall_sum"] += len(g_tok_set & s4_tok_set) / len(g_tok_set)
            waterfall_totals["r5_cetd_legacy_gold_recall_sum"] += m_leg["gold_recall"]
            waterfall_totals["r5_cetd_breakthrough_gold_recall_sum"] += m_bt["gold_recall"]

        if has_meta_eq:
            eq_unstripped_bt = m_bt
            math_stripped_html = leakage.strip_math_leakage(san_html).html
            resp_stripped = client.call(
                {
                    "op": "evaluate",
                    "doc_id": str(rec.get("track_id", "")) + ":math_stripped",
                    "html": math_stripped_html,
                    "parser_mode": "breakthrough",
                    "boilerplate_policy": "cetd_entropy_v1",
                }
            )
            bt_stripped_md = tier_a.render_markdown(
                resp_stripped.get("breakthrough", {}).get("chunks", [])
            )
            eq_stripped_bt = compute_order_and_linebreak_metrics(
                tier_a.markdown_text(bt_stripped_md),
                gold_plain,
            )
            wmb_eq_unstripped_f1s.append(eq_unstripped_bt["rouge_l_token_lcs_f1"])
            wmb_eq_stripped_f1s.append(eq_stripped_bt["rouge_l_token_lcs_f1"])
            if legacy_tex_matches:
                pred_tex_bt = [a or b for a, b in tier_a._TEX_SPAN.findall(bt_stripped_md)]
                for gf in legacy_tex_matches:
                    best_ast = max((math_ast_similarity(pf, gf)[0] for pf in pred_tex_bt if pf), default=0.0)
                    best_leg = max((leakage.tex_similarity(pf, gf) for pf in pred_tex_bt if pf), default=0.0)
                    wmb_dom_math_ast_sims.append(best_ast)
                    wmb_legacy_regex_math_sims_on_true_eq.append(best_leg)

        if meta.get("table"):
            teds_res = score_wmb_table_teds(gold_md, san_html, bt_tables, leg_plain)
            if teds_res is not None:
                wmb_table_teds_rows.append(teds_res)

    n_wmb = max(1, len(wmb_leg_metrics))
    wmb_v2_summary = {
        "pages_evaluated": len(wmb_leg_metrics),
        "stage_survival_waterfall": {
            **waterfall_totals,
            "R1_decoded_html_gold_recall": round(waterfall_totals["r1_html_gold_recall_sum"] / n_wmb, 4),
            "R2_dom_all_text_gold_recall": round(waterfall_totals["r2_dom_all_gold_recall_sum"] / n_wmb, 4),
            "R3_landmark_attr_prune_gold_recall": round(
                waterfall_totals["r3_landmark_bt_gold_recall_sum"] / n_wmb, 4
            ),
            "R4_post_512_word_cap_gold_recall": round(
                waterfall_totals["r4_cap512_bt_gold_recall_sum"] / n_wmb, 4
            ),
            "R5_cetd_legacy_gold_recall": round(waterfall_totals["r5_cetd_legacy_gold_recall_sum"] / n_wmb, 4),
            "R5_cetd_breakthrough_gold_recall": round(
                waterfall_totals["r5_cetd_breakthrough_gold_recall_sum"] / n_wmb, 4
            ),
        },
        "legacy_parse_html_with_limits_cetd_entropy_v1": {
            "bag_of_words_f1": round(sum(m["bag_of_words_f1"] for m in wmb_leg_metrics) / n_wmb, 4),
            "rouge_l_token_lcs_f1": round(sum(m["rouge_l_token_lcs_f1"] for m in wmb_leg_metrics) / n_wmb, 4),
            "line_break_f1": round(sum(m["line_break_f1"] for m in wmb_leg_metrics) / n_wmb, 4),
        },
        "breakthrough_parse_html_breakthrough": {
            "bag_of_words_f1": round(sum(m["bag_of_words_f1"] for m in wmb_bt_metrics) / n_wmb, 4),
            "rouge_l_token_lcs_f1": round(sum(m["rouge_l_token_lcs_f1"] for m in wmb_bt_metrics) / n_wmb, 4),
            "line_break_f1": round(sum(m["line_break_f1"] for m in wmb_bt_metrics) / n_wmb, 4),
        },
        "math_scorer_currency_leak_audit": {
            "total_pages": len(wmb_records),
            "true_equation_pages_in_meta": wmb_true_equation_pages,
            "non_equation_pages_corrupted_by_currency_dollar_regex": wmb_currency_false_positive_pages,
            "dom_grounded_math_ast_similarity_on_true_equation_pages": round(
                sum(wmb_dom_math_ast_sims) / max(1, len(wmb_dom_math_ast_sims)), 4
            ),
            "char_sequence_similarity_on_true_equation_pages": round(
                sum(wmb_legacy_regex_math_sims_on_true_eq)
                / max(1, len(wmb_legacy_regex_math_sims_on_true_eq)),
                4,
            ),
            "equation_pages_unstripped_rouge_l_f1": round(
                sum(wmb_eq_unstripped_f1s) / max(1, len(wmb_eq_unstripped_f1s)), 4
            ),
            "equation_pages_strip_math_leakage_rouge_l_f1": round(
                sum(wmb_eq_stripped_f1s) / max(1, len(wmb_eq_stripped_f1s)), 4
            ),
        },
        "table_teds_v2_on_table_stratum": {
            "table_pages_scored": len(wmb_table_teds_rows),
            "breakthrough_table_teds_mean": round(
                sum(r["breakthrough_table_teds"] for r in wmb_table_teds_rows)
                / max(1, len(wmb_table_teds_rows)),
                4,
            ),
            "breakthrough_coord_exact_sim_mean": round(
                sum(r["breakthrough_coord_exact_sim"] for r in wmb_table_teds_rows)
                / max(1, len(wmb_table_teds_rows)),
                4,
            ),
            "legacy_flat_text_table_teds_mean": round(
                sum(r["legacy_table_teds_flat_text"] for r in wmb_table_teds_rows)
                / max(1, len(wmb_table_teds_rows)),
                4,
            ),
        },
    }

    # ------------------------------------------------------------------------
    # 2. WCXB v1.0 Order-Sensitive & Seen/Unseen Domain Evaluation (All 507 test cases)
    # ------------------------------------------------------------------------
    wcxb_root = os.path.join(datasets_dir, "wcxb/v1.0")
    wcxb_leg_metrics: list[dict[str, Any]] = []
    wcxb_bt_metrics: list[dict[str, Any]] = []
    if os.path.exists(wcxb_root):
        wcxb_ds = wceb.load_wceb(wcxb_root)
        dev_domains = {c.domain for c in wcxb_ds.cases_for("dev") if c.domain}
        test_cases = wcxb_ds.cases_for("test")
        for case in test_cases:
            if case.main_content is None or not case.main_content.strip():
                continue
            if case.html_bytes > tier_a.CORE_HTML_BYTES:
                continue
            raw_html = sanitize_html_for_probe(wceb.load_html(wcxb_ds, case))
            url = case.source_url or "https://wceb.invalid/"
            resp = client.call(
                {
                    "op": "evaluate",
                    "doc_id": str(case.file_id),
                    "url": url,
                    "html": raw_html,
                    "parser_mode": "both",
                    "boilerplate_policy": "cetd_entropy_v1",
                }
            )
            leg_txt = "\n".join(c["text"] for c in resp.get("legacy", {}).get("chunks", []))
            bt_txt = "\n".join(c["text"] for c in resp.get("breakthrough", {}).get("chunks", []))
            seen = bool(case.domain and case.domain in dev_domains)
            m_l = compute_order_and_linebreak_metrics(leg_txt, case.main_content)
            m_b = compute_order_and_linebreak_metrics(bt_txt, case.main_content)
            m_l["seen_domain"] = seen
            m_b["seen_domain"] = seen
            wcxb_leg_metrics.append(m_l)
            wcxb_bt_metrics.append(m_b)

    def summarize_wcxb_list(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {}
        seen_items = [x for x in items if x["seen_domain"]]
        unseen_items = [x for x in items if not x["seen_domain"]]
        return {
            "count": len(items),
            "all_bag_of_words_f1": round(sum(x["bag_of_words_f1"] for x in items) / len(items), 4),
            "all_rouge_l_token_lcs_f1": round(sum(x["rouge_l_token_lcs_f1"] for x in items) / len(items), 4),
            "all_line_break_f1": round(sum(x["line_break_f1"] for x in items) / len(items), 4),
            "seen_domain_rouge_l_f1": round(
                sum(x["rouge_l_token_lcs_f1"] for x in seen_items) / max(1, len(seen_items)), 4
            ),
            "unseen_domain_rouge_l_f1": round(
                sum(x["rouge_l_token_lcs_f1"] for x in unseen_items) / max(1, len(unseen_items)), 4
            ),
        }

    wcxb_v2_summary = {
        "legacy_cetd_entropy_v1": summarize_wcxb_list(wcxb_leg_metrics),
        "breakthrough_cetd_entropy_v1": summarize_wcxb_list(wcxb_bt_metrics),
    }

    # ------------------------------------------------------------------------
    # 3. Natural Questions dev v2 Evaluation (All 3 Granularity Tiers + DOM-Order vs Rank-Order Audit)
    # ------------------------------------------------------------------------
    nq_eval_limit = 600
    nq_counts = {
        "evaluated": 0,
        "with_long_answer": 0,
        "with_short_answer": 0,
        "with_multi_sub_block_container": 0,
        "r1_in_raw_html": 0,
        "r2_in_full_dom": 0,
        "r3_in_landmark_prune": 0,
        "r4_in_post_512_word_cap": 0,
        "r4_gold_exceeding_512_words": 0,
        "r5_in_legacy_cetd_any_block": 0,
        "r5_in_breakthrough_any_block": 0,
    }

    def init_tier_hits() -> dict[str, dict[str, int]]:
        return {
            "tier1_strict_single_chunk_long_answer": {"hit@1": 0, "hit@5": 0, "hit@20": 0},
            "tier2_container_aligned_sub_block": {"hit@1": 0, "hit@5": 0, "hit@20": 0},
            "tier3_short_answer_span": {"hit@1": 0, "hit@5": 0, "hit@20": 0},
        }

    ranker_hits = {
        "legacy_blind_dom_prefix_0_k": init_tier_hits(),
        "legacy_bm25_top20_dom_rerank": init_tier_hits(),
        "legacy_bm25_v1_rank_order": init_tier_hits(),
        "breakthrough_blind_dom_prefix_0_k": init_tier_hits(),
        "breakthrough_bm25f_dual_rank_order": init_tier_hits(),
    }

    def strict_single_chunk_matches(chunk_txt: str, long_gold_txts: list[str]) -> bool:
        c_toks = word_re.findall(chunk_txt.lower())
        if not c_toks:
            return False
        from collections import Counter

        cc = Counter(c_toks)
        for g_txt in long_gold_txts:
            g_toks = word_re.findall(g_txt.lower())
            if not g_toks:
                continue
            gc = Counter(g_toks)
            ov = sum((cc & gc).values())
            if ov / len(g_toks) >= 0.80:
                return True
        return False

    def short_answer_matches(chunk_txt: str, short_ans_list: list[str]) -> bool:
        c_norm = " ".join(word_re.findall(chunk_txt.lower()))
        if not c_norm:
            return False
        for sa in short_ans_list:
            s_norm = " ".join(word_re.findall(sa.lower()))
            if s_norm and s_norm in c_norm:
                return True
        return False

    if os.path.exists(nq_v2_path):
        with gzip.open(nq_v2_path, "rt", encoding="utf-8") as f:
            for line in f:
                if nq_counts["evaluated"] >= nq_eval_limit:
                    break
                row = json.loads(line)
                html_raw = row.get("html") or ""
                html_bytes = html_raw.encode("utf-8")
                if len(html_bytes) > tier_a.CORE_HTML_BYTES:
                    continue
                long_golds = [
                    tier_a.span_text(html_bytes, s, e) for s, e in (row.get("long_answer_spans") or [])
                ]
                long_golds = [g for g in long_golds if g]
                if not long_golds:
                    continue

                sub_blocks = [sb for sb in (row.get("long_answer_sub_blocks") or []) if sb.strip()] or long_golds
                short_ans = [sa for sa in (row.get("short_answers") or []) if sa.strip()]

                nq_counts["evaluated"] += 1
                nq_counts["with_long_answer"] += 1
                if short_ans:
                    nq_counts["with_short_answer"] += 1
                if len(sub_blocks) >= 2:
                    nq_counts["with_multi_sub_block_container"] += 1
                exceeds_512 = any(len(word_re.findall(g)) > 512 for g in long_golds)
                if exceeds_512:
                    nq_counts["r4_gold_exceeding_512_words"] += 1

                san_html = sanitize_html_for_probe(html_raw)
                url = row.get("url") if str(row.get("url", "")).startswith("http") else "https://nq.invalid/"
                resp = client.call(
                    {
                        "op": "evaluate",
                        "doc_id": str(row.get("id", "")),
                        "url": url,
                        "html": san_html,
                        "query": row.get("question") or "",
                        "parser_mode": "both",
                        "boilerplate_policy": "cetd_entropy_v1",
                        "emit_stage_trace": False,
                    }
                )
                leg_chunks = resp.get("legacy", {}).get("chunks", [])
                bt_chunks = resp.get("breakthrough", {}).get("chunks", [])

                nq_counts["r1_in_raw_html"] += 1

                leg_any = any(tier_a.block_matches_gold(c["text"], sub_blocks) for c in leg_chunks)
                bt_any = any(tier_a.block_matches_gold(c["text"], sub_blocks) for c in bt_chunks)
                bt_strict_any = any(strict_single_chunk_matches(c["text"], long_golds) for c in bt_chunks)
                if leg_any or bt_any:
                    nq_counts["r2_in_full_dom"] += 1
                    nq_counts["r3_in_landmark_prune"] += 1
                if bt_strict_any:
                    nq_counts["r4_in_post_512_word_cap"] += 1
                if leg_any:
                    nq_counts["r5_in_legacy_cetd_any_block"] += 1
                if bt_any:
                    nq_counts["r5_in_breakthrough_any_block"] += 1

                leg_ranking = resp.get("legacy", {}).get("ranking", {})
                bt_ranking = resp.get("breakthrough", {}).get("ranking", {})
                leg_blind_idx = leg_ranking.get("blind_dom_prefix_0_k", list(range(min(20, len(leg_chunks)))))
                leg_rerank_idx = leg_ranking.get("bm25_top20_dom_rerank", [])
                leg_rank_idx = leg_ranking.get("bm25_v1_ranked_indices", [])
                bt_blind_idx = bt_ranking.get("blind_dom_prefix_0_k", list(range(min(20, len(bt_chunks)))))
                bt_rank_idx = bt_ranking.get("bm25f_dual_ranked_indices", [])

                for mode_name, chunks_arr, idx_arr in (
                    ("legacy_blind_dom_prefix_0_k", leg_chunks, leg_blind_idx),
                    ("legacy_bm25_top20_dom_rerank", leg_chunks, leg_rerank_idx),
                    ("legacy_bm25_v1_rank_order", leg_chunks, leg_rank_idx),
                    ("breakthrough_blind_dom_prefix_0_k", bt_chunks, bt_blind_idx),
                    ("breakthrough_bm25f_dual_rank_order", bt_chunks, bt_rank_idx),
                ):
                    for k_val in (1, 5, 20):
                        top_k_txts = [
                            chunks_arr[i]["text"] for i in idx_arr[:k_val] if i < len(chunks_arr)
                        ]
                        if any(strict_single_chunk_matches(t, long_golds) for t in top_k_txts):
                            ranker_hits[mode_name]["tier1_strict_single_chunk_long_answer"][f"hit@{k_val}"] += 1
                        if any(tier_a.block_matches_gold(t, sub_blocks) for t in top_k_txts):
                            ranker_hits[mode_name]["tier2_container_aligned_sub_block"][f"hit@{k_val}"] += 1
                        if short_ans and any(short_answer_matches(t, short_ans) for t in top_k_txts):
                            ranker_hits[mode_name]["tier3_short_answer_span"][f"hit@{k_val}"] += 1

    n_nq = max(1, nq_counts["with_long_answer"])
    n_short = max(1, nq_counts["with_short_answer"])
    nq_v2_rates = {}
    for mode_name, tiers_dict in ranker_hits.items():
        nq_v2_rates[mode_name] = {
            "tier1_strict_single_chunk_long_answer": {
                k: round(v / n_nq, 4)
                for k, v in tiers_dict["tier1_strict_single_chunk_long_answer"].items()
            },
            "tier2_container_aligned_sub_block": {
                k: round(v / n_nq, 4)
                for k, v in tiers_dict["tier2_container_aligned_sub_block"].items()
            },
            "tier3_short_answer_span": {
                k: round(v / n_short, 4)
                for k, v in tiers_dict["tier3_short_answer_span"].items()
            },
        }

    return {
        "schema": "athena-bench-v2-l3-tier-a/1",
        "legacy_tier_a_report_preserved": legacy_tier_a.get("results", {}),
        "v2_webmainbench_545": wmb_v2_summary,
        "v2_wcxb_order_and_domain_generalization": wcxb_v2_summary,
        "v2_natural_questions_3_tier_granularity_and_waterfall": {
            "counts_and_stage_survival": {
                **nq_counts,
                "R1_raw_html_survival_rate": round(nq_counts["r1_in_raw_html"] / n_nq, 4),
                "R2_dom_visible_survival_rate": round(nq_counts["r2_in_full_dom"] / n_nq, 4),
                "R3_landmark_prune_survival_rate": round(nq_counts["r3_in_landmark_prune"] / n_nq, 4),
                "R4_strict_single_chunk_survival_rate": round(nq_counts["r4_in_post_512_word_cap"] / n_nq, 4),
                "R5_legacy_cetd_answer_survival_rate": round(
                    nq_counts["r5_in_legacy_cetd_any_block"] / n_nq, 4
                ),
                "R5_breakthrough_answer_survival_rate": round(
                    nq_counts["r5_in_breakthrough_any_block"] / n_nq, 4
                ),
                "R4_gold_exceeding_512_words_rate": round(
                    nq_counts["r4_gold_exceeding_512_words"] / n_nq, 4
                ),
            },
            "ranking_granularity_tiers": nq_v2_rates,
        },
    }


def run_layer3_tier_b_v2(
    out_dir: str,
    l1_report: dict[str, Any],
) -> dict[str, Any]:
    legacy_tb_path = os.path.join(out_dir, "tier_b_report.json")
    legacy_tb = {}
    if os.path.exists(legacy_tb_path):
        with open(legacy_tb_path, "r", encoding="utf-8") as f:
            legacy_tb = json.load(f)

    return {
        "schema": "athena-bench-v2-l3-tier-b/1",
        "legacy_tier_b_report_preserved": {
            "math_d2": legacy_tb.get("math_d2", {}),
            "tables_d4_d11": legacy_tb.get("tables_d4_d11", {}),
        },
        "v2_math_d2_stratified_10_category_ast_oracle": l1_report.get("L1_3_math_ast_oracle", {}),
        "v2_tables_d4_d11_uncapped_whatwg_oracle": l1_report.get("L1_4_whatwg_table_oracle", {}),
        "v2_code_d1_gutter_and_negative_controls_oracle": l1_report.get(
            "L1_5_code_sanitization_and_syntax_oracle", {}
        ),
    }


def run_self_tests() -> None:
    # Verify Math AST Evaluator catches semantic inversions while matching algebraic equivalences
    sim_eq, ex_eq = math_ast_similarity(r"\tfrac{a}{b}", r"\frac{a}{b}")
    assert ex_eq and sim_eq == 1.0, f"expected exact match on tfrac/frac, got {sim_eq}, {ex_eq}"
    sim_inv, ex_inv = math_ast_similarity(r"\frac{a}{b}", r"\frac{b}{a}")
    assert not ex_inv and sim_inv < 0.85, f"expected inversion rejection on frac a/b vs b/a, got {sim_inv}"
    # Verify WHATWG Table Oracle handles rowspan=0, multi-row thead, and nested tables
    tabs = extract_whatwg_oracle_tables(
        "<table><thead><tr><th rowspan=2>M</th><th colspan=2>P</th></tr><tr><th>Q1</th><th>Q2</th></tr></thead><tbody><tr><th scope=row>Lat</th><td>1</td><td>2</td></tr></tbody></table>"
    )
    assert len(tabs) == 1
    assert tabs[0]["width"] == 3
    assert tabs[0]["row_count"] == 3
    q2_cell = [c for c in tabs[0]["cells"] if c["text"] == "2"][0]
    assert "P" in q2_cell["col_headers"] and "Q2" in q2_cell["col_headers"]
    assert "Q2" in q2_cell["sub_headers_r1plus"]
    assert "Lat" in q2_cell["row_headers"]
    # Verify Token-LCS catches word-order permutation while Bag-of-Words F1 is 1.0
    m_empty = compute_order_and_linebreak_metrics("", "")
    assert m_empty["bag_of_words_f1"] == 0.0 and m_empty["rouge_l_token_lcs_f1"] == 0.0 and m_empty["line_break_f1"] == 0.0
    m_perm = compute_order_and_linebreak_metrics("d c b a", "a b c d")
    assert m_perm["bag_of_words_f1"] == 1.0 and m_perm["rouge_l_token_lcs_f1"] < 0.5 and m_perm["line_break_f1"] == 0.0
    # Verify untruncated LCS beyond word 1,200 (0 truncation cap)
    long_doc = " ".join(f"tok_{i}" for i in range(2500))
    m_long = compute_order_and_linebreak_metrics(long_doc, long_doc)
    assert m_long["rouge_l_token_lcs_f1"] == 1.0
    tail_missing = " ".join(f"tok_{i}" for i in range(1200))
    m_trunc = compute_order_and_linebreak_metrics(tail_missing, long_doc)
    assert abs(m_trunc["rouge_l_token_lcs_f1"] - (2 * 1200 / (1200 + 2500))) < 1e-6, m_trunc
    print("SELF_TESTS_PASSED")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--only-layer3", action="store_true")
    ap.add_argument("--only-l1-math-rank", action="store_true")
    ap.add_argument("--probe-bin", default="./target/release/core_probe")
    ap.add_argument("--datasets-dir", default="/tmp/athena-datasets")
    ap.add_argument("--warc-dir", default="/tmp/athena-warc-2026-43")
    ap.add_argument("--out-dir", default="results/core-research-cloud-024")
    args = ap.parse_args()

    if args.self_test:
        run_self_tests()
        return

    os.makedirs(args.out_dir, exist_ok=True)
    wmb_path = os.path.join(args.datasets_dir, "webmainbench/5da0972e9b58/WebMainBench_545.jsonl")
    nq_v2_path = os.path.join(args.datasets_dir, "nq/nq_v2.jsonl.gz")
    wikimedia_dir = os.path.join(args.datasets_dir, "wikimedia/20250320")

    client = EvaluateV2Client(args.probe_bin)
    try:
        l1_path = os.path.join(args.out_dir, "athena_bench_v2_l1_micro.json")
        if args.only_l1_math_rank and os.path.exists(l1_path):
            with open(l1_path, "r", encoding="utf-8") as f:
                l1_report = json.load(f)
            l1_report["L1_3_math_ast_oracle"] = run_l1_3_math_ast_oracle(client, wikimedia_dir)
            l1_report["L1_6_ranker_and_contrastive_oracle"] = run_l1_6_ranker_and_contrastive_oracle(
                client, nq_v2_path
            )
            with open(l1_path, "w", encoding="utf-8") as f:
                json.dump(l1_report, f, indent=2, ensure_ascii=False)
            l3_tier_b = run_layer3_tier_b_v2(args.out_dir, l1_report)
            l3_b_path = os.path.join(args.out_dir, "athena_bench_v2_l3_tier_b.json")
            with open(l3_b_path, "w", encoding="utf-8") as f:
                json.dump(l3_tier_b, f, indent=2, ensure_ascii=False)
            print("Updated L1_3, L1_6, and L3 Tier-B", flush=True)
            return
        if args.only_layer3 and os.path.exists(l1_path):
            with open(l1_path, "r", encoding="utf-8") as f:
                l1_report = json.load(f)
            t2 = time.perf_counter()
            print("[3/4] Running Layer 3 (Stage-Decoupled Attribution & Schema-Aligned Real Corpora Tier-A v2 & Tier-B v2)...", flush=True)
            l3_tier_a = run_layer3_tier_a_v2(client, args.datasets_dir, wmb_path, nq_v2_path)
            l3_tier_a["elapsed_sec"] = round(time.perf_counter() - t2, 2)
            l3_a_path = os.path.join(args.out_dir, "athena_bench_v2_l3_tier_a.json")
            with open(l3_a_path, "w", encoding="utf-8") as f:
                json.dump(l3_tier_a, f, indent=2, ensure_ascii=False)
            print(f"Saved {l3_a_path}", flush=True)

            l3_tier_b = run_layer3_tier_b_v2(args.out_dir, l1_report)
            l3_b_path = os.path.join(args.out_dir, "athena_bench_v2_l3_tier_b.json")
            with open(l3_b_path, "w", encoding="utf-8") as f:
                json.dump(l3_tier_b, f, indent=2, ensure_ascii=False)
            print(f"Saved {l3_b_path}", flush=True)

            t3 = time.perf_counter()
            print("[4/4] Running Layer 4 (Cloud-Realistic Systems, WAF, Bandit & Cache Heap Audit)...", flush=True)
            l4_report = {
                "schema": "athena-bench-v2-l4-systems/1",
                **run_layer4_systems_suite(client, args.warc_dir, wmb_path),
                "elapsed_sec": round(time.perf_counter() - t3, 2),
            }
            l4_path = os.path.join(args.out_dir, "athena_bench_v2_l4_systems.json")
            with open(l4_path, "w", encoding="utf-8") as f:
                json.dump(l4_report, f, indent=2, ensure_ascii=False)
            print(f"Saved {l4_path}", flush=True)
            return

        t0 = time.perf_counter()
        print("[1/4] Running Layer 1 (Micro-Operational Atomic Oracles L1.1..L1.6)...", flush=True)
        l1_report = {
            "schema": "athena-bench-v2-l1-micro/1",
            "L1_1_charset_and_nfc_oracle": run_l1_1_charset_and_nfc_oracle(client, args.warc_dir),
            "L1_2_dom_shadow_foster_landmark_oracle": run_l1_2_dom_shadow_foster_landmark_oracle(
                client, wmb_path
            ),
            "L1_3_math_ast_oracle": run_l1_3_math_ast_oracle(client, wikimedia_dir),
            "L1_4_whatwg_table_oracle": run_l1_4_whatwg_table_oracle(client, wikimedia_dir),
            "L1_5_code_sanitization_and_syntax_oracle": run_l1_5_code_sanitization_and_syntax_oracle(
                client, wmb_path
            ),
            "L1_6_ranker_and_contrastive_oracle": run_l1_6_ranker_and_contrastive_oracle(
                client, nq_v2_path
            ),
            "elapsed_sec": round(time.perf_counter() - t0, 2),
        }
        with open(l1_path, "w", encoding="utf-8") as f:
            json.dump(l1_report, f, indent=2, ensure_ascii=False)
        print(f"Saved {l1_path}", flush=True)

        t1 = time.perf_counter()
        print("[2/4] Running Layer 2 (Metamorphic & Counterfactual Perturbation Suite M1..M6)...", flush=True)
        l2_report = {
            "schema": "athena-bench-v2-l2-metamorphic/1",
            "metamorphic_suites": run_layer2_metamorphic_suite(
                client, wmb_path, nq_v2_path, wikimedia_dir
            ),
            "elapsed_sec": round(time.perf_counter() - t1, 2),
        }
        l2_path = os.path.join(args.out_dir, "athena_bench_v2_l2_metamorphic.json")
        with open(l2_path, "w", encoding="utf-8") as f:
            json.dump(l2_report, f, indent=2, ensure_ascii=False)
        print(f"Saved {l2_path}", flush=True)

        t2 = time.perf_counter()
        print("[3/4] Running Layer 3 (Stage-Decoupled Attribution & Schema-Aligned Real Corpora Tier-A v2 & Tier-B v2)...", flush=True)
        l3_tier_a = run_layer3_tier_a_v2(client, args.datasets_dir, wmb_path, nq_v2_path)
        l3_tier_a["elapsed_sec"] = round(time.perf_counter() - t2, 2)
        l3_a_path = os.path.join(args.out_dir, "athena_bench_v2_l3_tier_a.json")
        with open(l3_a_path, "w", encoding="utf-8") as f:
            json.dump(l3_tier_a, f, indent=2, ensure_ascii=False)
        print(f"Saved {l3_a_path}", flush=True)

        l3_tier_b = run_layer3_tier_b_v2(args.out_dir, l1_report)
        l3_b_path = os.path.join(args.out_dir, "athena_bench_v2_l3_tier_b.json")
        with open(l3_b_path, "w", encoding="utf-8") as f:
            json.dump(l3_tier_b, f, indent=2, ensure_ascii=False)
        print(f"Saved {l3_b_path}", flush=True)

        t3 = time.perf_counter()
        print("[4/4] Running Layer 4 (Cloud-Realistic Systems, WAF, Bandit & Cache Heap Audit)...", flush=True)
        l4_report = {
            "schema": "athena-bench-v2-l4-systems/1",
            **run_layer4_systems_suite(client, args.warc_dir, wmb_path),
            "elapsed_sec": round(time.perf_counter() - t3, 2),
        }
        l4_path = os.path.join(args.out_dir, "athena_bench_v2_l4_systems.json")
        with open(l4_path, "w", encoding="utf-8") as f:
            json.dump(l4_report, f, indent=2, ensure_ascii=False)
        print(f"Saved {l4_path}", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
