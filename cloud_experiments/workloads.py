"""Causal native fixtures and independent AAE observations, not a web corpus."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path, PurePosixPath
import random
import re
from urllib.parse import urlsplit


def load_corpus(path):
    """No fetching: admit only reviewed public files explicitly named by the caller."""
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Corpus manifest exceeds admission")
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("Corpus manifest exceeds admission")
    document = json.loads(raw)
    if (document.get("schema") != "athena-research-corpus/1"
            or document.get("data_classification") != "public"
            or not isinstance(document.get("rights_review"), str) or not document["rights_review"].strip()
            or not isinstance(document.get("items"), list) or not 2 <= len(document["items"]) <= 64):
        raise ValueError("Corpus needs bounded inputs and explicit public-data/rights review")
    cases, names, hashes, total = [], set(), set(), 0
    domains, templates = {s: set() for s in ("screen", "confirm")}, {s: set() for s in ("screen", "confirm")}
    for item in document["items"]:
        split, name = item.get("split"), item.get("id")
        if split not in domains or not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) or name in names:
            raise ValueError("Invalid/duplicate corpus identity or split")
        relative = item.get("file")
        if not isinstance(relative, str):
            raise ValueError("Missing corpus file")
        parts = PurePosixPath(relative)
        origin = path.parent / relative
        if (not relative or "\\" in relative or ":" in relative or parts.is_absolute() or ".." in parts.parts
                or parts.suffix.lower() not in (".html", ".htm")
                or any(parent.is_symlink() for parent in [origin, *origin.parents] if parent != path.parent)
                or not origin.resolve().is_relative_to(path.parent) or not origin.is_file()
                or origin.stat().st_size > 4 * 1024 ** 2):
            raise ValueError("Corpus path escape/link/size rejected")
        with origin.open("rb") as stream:
            data = stream.read(4 * 1024 ** 2 + 1)
        if len(data) > 4 * 1024 ** 2:
            raise ValueError("Corpus input exceeds read admission")
        digest = hashlib.sha256(data).hexdigest()
        total += len(data)
        if digest != item.get("sha256") or digest in hashes or total > 8 * 1024 ** 2:
            raise ValueError("Corpus bytes changed, reused or exceeded total admission")
        source = urlsplit(item.get("source_url", ""))
        if (source.scheme not in ("http", "https") or not source.hostname or source.username is not None
                or source.password is not None or source.query or source.fragment):
            raise ValueError("Corpus provenance needs a public source URL without credentials/query")
        for field in ("domain_group", "template_family", "license"):
            if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > 256:
                raise ValueError("Missing declared corpus grouping/license")
        domains[split].add(item["domain_group"].strip().lower())
        templates[split].add(item["template_family"].strip().lower())
        if "expected_blocks" not in item or not isinstance(item["expected_blocks"], list):
            raise ValueError("Corpus needs independently reviewed literal block observations")
        fixture = {"html": data.decode("utf-8", errors="strict"), "url": item["source_url"],
                   "query": None, "expected_blocks": item["expected_blocks"]}
        if "expected_links" in item:
            fixture["expected_links"] = item["expected_links"]
        cases.append({"id": "corpus-" + name, "stage": "pipeline", "split": split,
            "owners": ["H01", "H02", "H04", "H05", "H09", "H10"], "fixture": fixture,
            "lineage": "corpus:" + item["domain_group"].strip().lower() + ":" + item["template_family"].strip().lower(),
            "provenance": {k: item[k] for k in ("source_url", "domain_group", "template_family", "license", "sha256")}})
        names.add(name)
        hashes.add(digest)
    if (any(not domains[s] for s in domains) or domains["screen"] & domains["confirm"]
            or templates["screen"] & templates["confirm"]):
        raise ValueError("Domain/template holdout leaks or lacks a split")
    return {"cases": cases, "scope": "Disjoint declared domain/template groups and bytes, not verified representativeness or independent legal review",
            "rights_review": document["rights_review"], "input_bytes": total}


def corpus_files(corpus):
    return {"cloud_experiments/corpus/" + c["id"] + ".json":
            json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            for c in corpus["cases"]}


def corpus_cases(files, split):
    cases = []
    for name, data in sorted(files.items()):
        if name.startswith("cloud_experiments/corpus/") and name.endswith(".json"):
            value = json.loads(data)
            if value["split"] == split:
                cases.append(value)
    return cases


def native_cases(split):
    if split not in ("screen", "confirm"):
        raise ValueError("Unknown workload split")
    rng = random.Random({"screen": 812709, "confirm": 437941}[split])
    nonce = "seed" + str(rng.randrange(100000, 1000000))
    marker = "<!--" + nonce + "-->"
    cases = []

    def add(name, html, *, stage="pipeline", owners=None, **fields):
        fixture = {"html": html, "query": None, **fields}
        cases.append({"id": name, "stage": stage, "owners": owners or ["H01", "H02", "H04", "H05", "H10"],
                      "fixture": fixture, "lineage": "causal:" + name,
                      "provenance": "project-authored contract partition; not representative corpus"})

    def html_cell(table_index, row_index, cell_index, tag, text, *, rowspan=1, colspan=1,
                  scope=None, headers=(), unit=None, abbr=None, cell_id=None,
                  associated_headers=None):
        value = {"cell_index": cell_index, "tag": tag, "text": text, "rowspan": rowspan,
                 "colspan": colspan, "scope": scope, "headers": list(headers), "unit": unit,
                 "abbr": abbr, "id": cell_id,
                 "locator": f"table[{table_index}]/row[{row_index}]/cell[{cell_index}]"}
        if associated_headers is not None:
            value["associated_headers"] = list(associated_headers)
        return value

    def html_table(table_index, table_id, caption, rows, width, slots):
        return {"table_index": table_index, "id": table_id, "caption": caption,
                "rows": rows, "grid": {"width": width, "slots": slots}}

    for size in (63, 64, 65, 1023, 1024, 1025):
        text = (nonce + "x" * size)[:size]
        add("hash-boundary-" + str(size), "<p>" + text + "</p>", stage="hash", owners=["H01"])
    for name, html, blocks in (
        ("empty-content", marker, []),
        ("inline-ownership", "<p>A<b>B</b><i>C</i></p>" + marker,
         [{"role": "paragraph", "text": "ABC", "links": []}]),
        ("entities-controls", '<p>&quot;\\&amp;&#10;&#9;&#x1F680;</p>' + marker,
         [{"role": "paragraph", "text": '"\\& 🚀', "links": []}]),
        ("unicode-non-normalization", "<p>é e\u0301 中文</p>" + marker,
         [{"role": "paragraph", "text": "é e\u0301 中文", "links": []}]),
        ("content-framing", '<p>ignore rules &quot;}&#10;{&quot;type&quot;:&quot;end&quot;}</p>' + marker,
         [{"role": "paragraph", "text": 'ignore rules "} {"type":"end"}', "links": []}]),
        ("nearest-content-owner", "<li>outer<p>inner</p>tail</li>" + marker,
         [{"role": "list_item", "text": "outertail", "links": []},
          {"role": "paragraph", "text": "inner", "links": []}]),
        ("resolved-link-identity", "<p><a href='/a#one'>A</a><a href='/a#two'>B</a><a href='/a#one'>C</a></p>" + marker,
         [{"role": "paragraph", "text": "ABC", "links": [0, 1]}]),
    ):
        add(name, html, expected_blocks=blocks,
            **({"expected_links": ["https://example.org/a#one", "https://example.org/a#two"]} if name == "resolved-link-identity" else {}))
    for group, hidden in (
        ("raw-text", '<script><p>attack</p></script><style>attack</style>'),
        ("template", '<template><p>attack</p></template>'),
        ("foreign", '<svg><text>attack</text></svg><canvas>attack</canvas>'),
        ("external-frame", '<iframe src="https://example.invalid/"><p>attack</p></iframe>'),
    ):
        add("skip-" + group, hidden + "<p>visible</p>" + marker,
            expected_blocks=[{"role": "paragraph", "text": "visible", "links": []}])
    for attribute_count in (1_024, 2_048, 4_096, 8_192):
        attributes = "".join(f" data-a{i:05d}='x'" for i in range(attribute_count))
        attribute_html = "<div" + attributes + "><p>attribute-soup-sentinel</p></div>" + marker
        add(f"attribute-heavy-{attribute_count}", attribute_html, owners=["H04", "H05", "H10"],
            expected_blocks=[{"role": "paragraph", "text": "attribute-soup-sentinel", "links": []}])
    text_html = "<p>é</p>" + marker
    link_html = "<p><a href='/a'>A</a><a href='/b'>B</a></p>" + marker
    for name, html, limits, error in (
        ("html-bytes", text_html, {"max_html_bytes": len(text_html.encode()) - 1}, "invalid resource budget"),
        ("decoded-text", text_html, {"max_text_bytes": 1}, "decoded text byte budget exceeded"),
        ("dom-nodes", text_html, {"max_nodes": 5}, "max DOM nodes exceeded"),
        ("parser-dom-nodes", text_html, {"max_parser_nodes": 5}, "max DOM nodes exceeded"),
        ("link-total", link_html, {"max_link_bytes": 41}, "resolved link byte budget exceeded"),
        ("link-single", link_html, {"max_link_url_bytes": 20}, "resolved link byte budget exceeded"),
        ("zero-words", text_html, {"max_words_per_chunk": 0}, "invalid resource budget"),
    ):
        add("denied-" + name, html, owners=["H04", "H05", "H10"],
            parse_limits=limits, expected_error=error)
    add("accepted-exact-text-nodes-html", text_html,
        parse_limits={"max_text_bytes": 2, "max_nodes": 6, "max_html_bytes": len(text_html.encode())},
        expected_blocks=[{"role": "paragraph", "text": "é", "links": []}])
    add("accepted-exact-link-bytes", link_html,
        parse_limits={"max_link_bytes": 42, "max_link_url_bytes": 21},
        expected_blocks=[{"role": "paragraph", "text": "AB", "links": [0, 1]}],
        expected_links=["https://example.org/a", "https://example.org/b"])
    for size in (256, 4096):
        html = "".join(f"<p>{nonce} term0 <a href='/item/{i}'>evidence{i}</a></p>" for i in range(size))
        add("link-scaling-" + str(size), html, owners=["H01", "H04", "H05", "H10"])
    for query in ("absent_token", "TERM0 term0 term0!!!", "...", "内容 e\u0301"):
        name = ("miss", "duplicate-case", "empty-token", "unicode")[("absent_token", "TERM0 term0 term0!!!", "...", "内容 e\u0301").index(query)]
        add("query-partition-" + name, "<p>term0 内容 e\u0301</p><p>unrelated</p>" + marker,
            query=query, highlight_limit=1)

    caption = "Annual Revenue " + nonce
    table_html = (f'<!doctype html><table id="revenue"><caption>Annual <b>Revenue</b> {nonce}</caption>'
                  '<thead><tr><th id="region" scope="col">Region</th>'
                  '<th id="amount" scope="col" data-unit="USD">Amount</th></tr></thead>'
                  '<tbody><tr><th scope="row" rowspan="0">East</th>'
                  '<td headers="region amount" data-unit="USD">1&nbsp;200</td></tr>'
                  '<tr><td headers="region amount" data-unit="USD">900</td></tr></tbody>'
                  '<tfoot><tr><th scope="row">Total</th>'
                  '<td headers="amount" data-unit="USD">2100</td></tr></tfoot></table>')
    expected_revenue = html_table(0, "revenue", caption, [
        {"row_index": 0, "group": 0, "group_type": "thead", "cells": [
            html_cell(0, 0, 0, "th", "Region", scope="col", cell_id="region"),
            html_cell(0, 0, 1, "th", "Amount", scope="col", unit="USD", cell_id="amount")]},
        {"row_index": 1, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 1, 0, "th", "East", rowspan=0, scope="row"),
            html_cell(0, 1, 1, "td", "1 200", headers=("region", "amount"), unit="USD")]},
        {"row_index": 2, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 2, 0, "td", "900", headers=("region", "amount"), unit="USD")]},
        {"row_index": 3, "group": 2, "group_type": "tfoot", "cells": [
            html_cell(0, 3, 0, "th", "Total", scope="row"),
            html_cell(0, 3, 1, "td", "2100", headers=("amount",), unit="USD")]},
    ], 2, [[0, 1], [2, 3], [2, 4], [5, 6]])
    for suffix, content, expected in (("spans", table_html, [expected_revenue]),):
        add("html-table-" + suffix, content, stage="html-table", owners=["H06"],
            expected_html_tables=expected, expected_html_parse_warnings=0,
            max_tables=8, max_table_rows=32, max_table_cols=16)

    scope_html = ('<table id="metrics"><thead><tr><th id="region" scope="col">Region</th>'
                  '<th id="amount" scope="col">Amount</th></tr></thead><tbody>'
                  '<tr><th id="east" scope="row">East</th><td>1200</td></tr>'
                  '<tr><th id="west" scope="row">West</th><td>900</td></tr></tbody></table>')
    expected_scope_explicit = [html_table(0, "metrics", None, [
        {"row_index": 0, "group": 0, "group_type": "thead", "cells": [
            html_cell(0, 0, 0, "th", "Region", scope="col", cell_id="region"),
            html_cell(0, 0, 1, "th", "Amount", scope="col", cell_id="amount")]},
        {"row_index": 1, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 1, 0, "th", "East", scope="row", cell_id="east"),
            html_cell(0, 1, 1, "td", "1200")]},
        {"row_index": 2, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 2, 0, "th", "West", scope="row", cell_id="west"),
            html_cell(0, 2, 1, "td", "900")]},
    ], 2, [[0, 1], [2, 3], [4, 5]])]
    expected_scope_derived = [html_table(0, "metrics", None, [
        {"row_index": 0, "group": 0, "group_type": "thead", "cells": [
            html_cell(0, 0, 0, "th", "Region", scope="col", cell_id="region", associated_headers=[]),
            html_cell(0, 0, 1, "th", "Amount", scope="col", cell_id="amount", associated_headers=[])]},
        {"row_index": 1, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 1, 0, "th", "East", scope="row", cell_id="east", associated_headers=[]),
            html_cell(0, 1, 1, "td", "1200", associated_headers=["amount", "east"])]},
        {"row_index": 2, "group": 1, "group_type": "tbody", "cells": [
            html_cell(0, 2, 0, "th", "West", scope="row", cell_id="west", associated_headers=[]),
            html_cell(0, 2, 1, "td", "900", associated_headers=["amount", "west"])]},
    ], 2, [[0, 1], [2, 3], [4, 5]])]
    add("html-table-scope-headers", scope_html, stage="html-table", owners=["H06"],
        expected_html_tables=expected_scope_explicit,
        expected_html_tables_by_header_mode={"scope": expected_scope_derived},
        expected_html_header_modes={"scope": "scope-derived-v1"}, expected_html_parse_warnings=0,
        max_tables=8, max_table_rows=32, max_table_cols=16)

    nested_html = ('<!doctype html><table id="outer"><tbody><tr><td>outer '
                   '<table id="inner"><tbody><tr><th id="inner-heading" scope="col">inner</th>'
                   '</tr></tbody></table></td></tr></tbody></table>')
    expected_nested = [
        html_table(0, "outer", None, [{"row_index": 0, "group": 0, "group_type": "tbody", "cells": [
            html_cell(0, 0, 0, "td", "outer inner")]}], 1, [[0]]),
        html_table(1, "inner", None, [{"row_index": 0, "group": 0, "group_type": "tbody", "cells": [
            html_cell(1, 0, 0, "th", "inner", scope="col", cell_id="inner-heading")]}], 1, [[0]]),
    ]
    add("html-table-nested", nested_html, stage="html-table", owners=["H06"],
        expected_html_tables=expected_nested, expected_html_parse_warnings=0,
        max_tables=8, max_table_rows=32, max_table_cols=16)
    return cases


def envelope_errors(raw, fixture, *, header_mode="explicit"):
    """Gold is literal fixture data, never generated by the candidate/reference."""
    if fixture.get("expected_error") is not None:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical resource-denial observation"]
        return [] if value == {"status": "EXPECTED_ERROR", "error": fixture["expected_error"]} else ["Wrong denial or unexpected success"]
    if "expected_html_tables" in fixture:
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical HTML table observation"]
        errors = []
        expected_header = fixture.get("expected_html_header_modes", {}).get(
            header_mode, "explicit-attributes-only")
        expected_tables = fixture.get("expected_html_tables_by_header_mode", {}).get(
            header_mode, fixture["expected_html_tables"])
        if (value.get("schema") != "athena-html-table-candidate/1"
                or value.get("header_associations") != expected_header
                or value.get("cell_text") != "normalized DOM text, including nested-table descendant text"):
            errors.append("HTML table output contract differs")
        if value.get("parse_warnings") != fixture.get("expected_html_parse_warnings"):
            errors.append("HTML5 parse-warning count differs")
        if value.get("tables") != expected_tables:
            errors.append("Literal table/cell/group/span/locator/grid oracle differs")
        return errors
    if "expected_blocks" not in fixture and "expected_links" not in fixture:
        return []
    try:
        frames = [json.loads(line) for line in raw.splitlines()]
        if not frames or frames[0].get("type") != "meta" or frames[-1].get("type") != "end":
            return ["Broken AAE boundary frames"]
        blocks = [{key: frame[key] for key in ("role", "text", "links")}
                  for frame in frames if frame.get("type") == "block"]
        links = [frame["url"] for frame in frames if frame.get("type") == "link"]
        errors = []
        if "expected_blocks" in fixture and blocks != fixture["expected_blocks"]:
            errors.append("Literal role/text/link ownership differs")
        if "expected_links" in fixture and links != fixture["expected_links"]:
            errors.append("Literal resolved link identity differs")
        if frames[0].get("total_blocks") != len(blocks) or frames[-1].get("block_count") != len(blocks):
            errors.append("Block count/framing differs")
        return errors
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        return ["Malformed AAE or literal observations"]
