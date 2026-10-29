"""Causal native fixtures and independent AAE observations, not a web corpus."""
from __future__ import annotations

import json
import hashlib
import ipaddress
from pathlib import Path, PurePosixPath
import random
import re
from datetime import date
from urllib.parse import urlsplit

from kaggle_experiments.protocol import strict_json_loads


_DATASET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,191}")
_LICENSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}")


def _normalized_hostname(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("Corpus source hostname is invalid")
    value = value.rstrip(".")
    try:
        return ipaddress.ip_address(value).compressed.lower()
    except ValueError:
        try:
            hostname = value.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("Corpus source hostname is invalid") from error
        labels = hostname.split(".")
        if len(hostname) > 253 or any(not label or len(label) > 63 for label in labels):
            raise ValueError("Corpus source hostname is invalid")
        return hostname


def _https_provenance_url(value, field):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("Pinned dataset/license provenance needs bounded HTTPS URLs")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as error:
        raise ValueError("Pinned dataset/license provenance has an invalid " + field) from error
    if (parsed.scheme != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or parsed.query):
        raise ValueError("Pinned dataset/license provenance needs an HTTPS " + field + " URL")
    return value


def _pinned_dataset(document):
    dataset = document.get("dataset")
    dataset_fields = {"id", "revision", "source_url", "license_id", "license_source_url"}
    if not isinstance(dataset, dict) or set(dataset) != dataset_fields:
        raise ValueError("Corpus requires schema/2 and pinned dataset/license provenance")
    if (not isinstance(dataset["id"], str) or "://" in dataset["id"]
            or not _DATASET_ID.fullmatch(dataset["id"])):
        raise ValueError("Pinned dataset/license provenance has an invalid dataset id")
    revision = dataset["revision"]
    if not isinstance(revision, dict) or set(revision) != {"kind", "value"}:
        raise ValueError("Pinned dataset/license provenance needs a typed immutable revision")
    kind, value = revision["kind"], revision["value"]
    if kind == "git_commit":
        valid_revision = isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value)
    elif kind == "kaggle_version":
        valid_revision = type(value) is int and value > 0
    else:
        valid_revision = False
    if not valid_revision:
        raise ValueError("Pinned dataset/license provenance needs a Git commit or Kaggle version")
    if not isinstance(dataset["license_id"], str) or not _LICENSE_ID.fullmatch(dataset["license_id"]):
        raise ValueError("Pinned dataset/license provenance needs a machine-readable dataset license id")
    _https_provenance_url(dataset["source_url"], "dataset source")
    _https_provenance_url(dataset["license_source_url"], "dataset license source")

    review = document.get("rights_review")
    review_fields = {"decision", "reviewed_on", "evidence_urls", "rationale"}
    if not isinstance(review, dict) or set(review) != review_fields:
        raise ValueError("Pinned dataset/license provenance needs a structured rights review")
    if review["decision"] != "ALLOW_CLOUD_STAGING":
        raise ValueError("Corpus rights review does not allow cloud staging")
    reviewed_on = review["reviewed_on"]
    try:
        if not isinstance(reviewed_on, str) or date.fromisoformat(reviewed_on).isoformat() != reviewed_on:
            raise ValueError
    except ValueError as error:
        raise ValueError("Corpus rights review needs an ISO calendar date") from error
    evidence_urls = review["evidence_urls"]
    if not isinstance(evidence_urls, list) or not 1 <= len(evidence_urls) <= 8:
        raise ValueError("Corpus rights review needs 1–8 evidence URLs")
    if any(not isinstance(url, str) for url in evidence_urls):
        raise ValueError("Corpus rights review evidence URLs must be strings")
    if len(set(evidence_urls)) != len(evidence_urls):
        raise ValueError("Corpus rights review evidence URLs must be distinct")
    for url in evidence_urls:
        _https_provenance_url(url, "rights evidence")
    if not isinstance(review["rationale"], str) or not review["rationale"].strip() or len(review["rationale"]) > 2048:
        raise ValueError("Corpus rights review needs a bounded rationale")
    return dict(dataset), dict(review)


def load_corpus(path):
    """No fetching: admit only pinned, explicitly reviewed public files."""
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Corpus manifest exceeds admission")
    with path.open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("Corpus manifest exceeds admission")
    document = strict_json_loads(raw)
    if not isinstance(document, dict):
        raise ValueError("Corpus manifest must be a JSON object")
    if document.get("schema") != "athena-research-corpus/2":
        raise ValueError("Corpus requires schema/2 and pinned dataset/license provenance")
    dataset, rights_review = _pinned_dataset(document)
    if (document.get("data_classification") != "public"
            or not isinstance(document.get("items"), list) or not 2 <= len(document["items"]) <= 64):
        raise ValueError("Corpus needs bounded inputs and explicit public-data/rights review")
    cases, names, hashes, total = [], set(), set(), 0
    hosts, templates = {s: set() for s in ("screen", "confirm")}, {s: set() for s in ("screen", "confirm")}
    for item in document["items"]:
        split, name = item.get("split"), item.get("id")
        if split not in hosts or not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) or name in names:
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
        file_limit = 4 * 1024 ** 2
        chunks, size = [], 0
        with origin.open("rb") as stream:
            while size <= file_limit:
                chunk = stream.read(min(65536, file_limit - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        if size > file_limit:
            raise ValueError("Corpus input exceeds read admission")
        data = (chunks[0] if chunks else b"") if len(chunks) <= 1 else b"".join(chunks)
        digest = hashlib.sha256(data).hexdigest()
        total += len(data)
        if digest != item.get("sha256") or digest in hashes or total > 8 * 1024 ** 2:
            raise ValueError("Corpus bytes changed, reused or exceeded total admission")
        try:
            source = urlsplit(item.get("source_url", ""))
            source.port
        except (TypeError, ValueError) as error:
            raise ValueError("Corpus provenance needs a valid HTTP(S) source URL") from error
        if (source.scheme not in ("http", "https") or not source.hostname or source.username is not None
                or source.password is not None or source.query or source.fragment):
            raise ValueError("Corpus provenance needs an HTTP(S) source URL without credentials/query/fragment")
        for field in ("domain_group", "template_family"):
            if not isinstance(item.get(field), str) or not item[field].strip() or len(item[field]) > 256:
                raise ValueError("Missing declared corpus grouping/license")
        host = _normalized_hostname(source.hostname)
        domain_group = _normalized_hostname(item["domain_group"])
        if domain_group != host:
            raise ValueError("Corpus domain_group must match the normalized source hostname")
        if not isinstance(item.get("license"), str) or not _LICENSE_ID.fullmatch(item["license"]):
            raise ValueError("Corpus item needs a machine-readable content license id")
        _https_provenance_url(item.get("license_source_url"), "content license source")
        hosts[split].add(host)
        templates[split].add(item["template_family"].strip().lower())
        if "expected_blocks" not in item or not isinstance(item["expected_blocks"], list):
            raise ValueError("Corpus needs independently reviewed literal block observations")
        fixture = {"html": data.decode("utf-8", errors="strict"), "url": item["source_url"],
                   "query": None, "expected_blocks": item["expected_blocks"]}
        if "expected_links" in item:
            fixture["expected_links"] = item["expected_links"]
        cases.append({"id": "corpus-" + name, "stage": "pipeline", "split": split,
            "owners": ["H01", "H02", "H04", "H05", "H09", "H10"], "fixture": fixture,
            "lineage": "corpus:" + host + ":" + item["template_family"].strip().lower(),
            "provenance": {"dataset_id": dataset["id"], "dataset_revision": dataset["revision"],
                "dataset_source_url": dataset["source_url"], "dataset_license_id": dataset["license_id"],
                "dataset_license_source_url": dataset["license_source_url"],
                "domain_group": host,
                **{k: item[k] for k in ("source_url", "template_family", "license",
                                         "license_source_url", "sha256")}}})
        names.add(name)
        hashes.add(digest)
    if (any(not hosts[s] for s in hosts) or hosts["screen"] & hosts["confirm"]
            or templates["screen"] & templates["confirm"]):
        raise ValueError("Hostname/template holdout leaks or lacks a split")
    return {"cases": cases,
            "scope": "Disjoint normalized source hostnames, declared template groups, and bytes; pinned provenance and rights declarations are not independently verified",
            "dataset": dataset, "rights_review": rights_review, "input_bytes": total}


def corpus_files(corpus):
    return {"cloud_experiments/corpus/" + c["id"] + ".json":
            json.dumps(c, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            for c in corpus["cases"]}


def corpus_cases(files, split):
    cases = []
    for name, data in sorted(files.items()):
        if name.startswith("cloud_experiments/corpus/") and name.endswith(".json"):
            value = strict_json_loads(data)
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
                  scope=None, headers=(), unit=None, abbr=None, cell_id=None):
        return {"cell_index": cell_index, "tag": tag, "text": text, "rowspan": rowspan,
                "colspan": colspan, "scope": scope, "headers": list(headers), "unit": unit,
                "abbr": abbr, "id": cell_id,
                "locator": f"table[{table_index}]/row[{row_index}]/cell[{cell_index}]"}

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
    # Synthetic crossover probes, not an estimate of real-site attribute counts.
    # Include both sides of each lazy-index promotion threshold and nearby values.
    for attribute_count in (0, 1, 2, 4, 7, 8, 9, 16, 31, 32, 33, 64, 65,
                            127, 128, 129):
        attributes = "".join(f" data-a{i:05d}='x'" for i in range(attribute_count))
        add(f"attribute-boundary-{attribute_count}", "<div" + attributes + "></div>" + marker,
            stage="parser", owners=["H04"])
    for threshold in (8, 32, 128):
        attributes = "".join(
            f" data-a{i:05d}='{'first' if i == 0 else 'x'}'" for i in range(threshold))
        duplicate_fixture = {
            "expected_parser_html_contains": ['data-a00000="first"'],
            "expected_parser_html_excludes": ['data-a00000="replacement"'],
        }
        add(f"attribute-duplicate-{threshold}",
            "<div" + attributes + " data-a00000='replacement'></div>" + marker,
            stage="parser", owners=["H04"], **duplicate_fixture)
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

    nested_html = ('<!doctype html><table id="outer"><tbody><tr><td>outer '
                   '<table id="inner"><tbody><tr><th id="inner-heading" scope="col">inner</th>'
                   '</tr></tbody></table> after</td></tr></tbody></table>' + marker)
    expected_nested = [
        html_table(0, "outer", None, [{"row_index": 0, "group": 0, "group_type": "tbody", "cells": [
            html_cell(0, 0, 0, "td", "outer after")]}], 1, [[0]]),
        html_table(1, "inner", None, [{"row_index": 0, "group": 0, "group_type": "tbody", "cells": [
            html_cell(1, 0, 0, "th", "inner", scope="col", cell_id="inner-heading")]}], 1, [[0]]),
    ]
    add("html-table-nested", nested_html, stage="html-table", owners=["H06"],
        expected_html_tables=expected_nested, expected_html_parse_warnings=0,
        max_tables=8, max_table_rows=32, max_table_cols=16)
    return cases


def envelope_errors(raw, fixture):
    """Gold is literal fixture data, never generated by the candidate/reference."""
    if fixture.get("expected_error") is not None:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical resource-denial observation"]
        return [] if value == {"status": "EXPECTED_ERROR", "error": fixture["expected_error"]} else ["Wrong denial or unexpected success"]
    parser_fields = {"expected_parser_html_contains", "expected_parser_html_excludes"}
    if parser_fields.intersection(fixture):
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical HTML parser observation"]
        if not isinstance(value, dict) or not isinstance(value.get("html"), str):
            return ["Expected a canonical HTML parser observation"]
        errors = []
        html = value["html"]
        for key, missing_message in (
                ("expected_parser_html_contains", "HTML parser output omitted a required literal"),
                ("expected_parser_html_excludes", "HTML parser output retained a forbidden literal")):
            expected = fixture.get(key, [])
            if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
                errors.append("Invalid literal HTML parser oracle")
                continue
            for item in expected:
                if ((key == "expected_parser_html_contains" and item not in html)
                        or (key == "expected_parser_html_excludes" and item in html)):
                    errors.append(missing_message)
        return errors
    if "expected_html_tables" in fixture:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical HTML table observation"]
        errors = []
        if (value.get("schema") != "athena-html-table-candidate/2"
                or value.get("header_associations") != "explicit-attributes-only"
                or value.get("cell_text") != "normalized DOM text, excluding nested-table descendants from each cell"):
            errors.append("HTML table output contract differs")
        if value.get("parse_warnings") != fixture.get("expected_html_parse_warnings"):
            errors.append("HTML5 parse-warning count differs")
        if value.get("tables") != fixture["expected_html_tables"]:
            errors.append("Literal table/cell/group/span/locator/grid oracle differs")
        return errors
    if "expected_blocks" not in fixture and "expected_links" not in fixture:
        return []
    try:
        frames = [strict_json_loads(line) for line in raw.splitlines()]
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
