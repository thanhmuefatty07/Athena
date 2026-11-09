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


def _html_cell(table_index, row_index, cell_index, tag, text, *, rowspan=1, colspan=1,
               scope=None, headers=(), unit=None, abbr=None, cell_id=None):
    return {"cell_index": cell_index, "tag": tag, "text": text, "rowspan": rowspan,
            "colspan": colspan, "scope": scope, "headers": list(headers), "unit": unit,
            "abbr": abbr, "id": cell_id,
            "locator": f"table[{table_index}]/row[{row_index}]/cell[{cell_index}]"}


def _html_table(table_index, table_id, caption, rows, width, slots):
    return {"table_index": table_index, "id": table_id, "caption": caption,
            "rows": rows, "grid": {"width": width, "slots": slots}}


def harsh_stress_cases(split):
    if split not in ("screen", "confirm"):
        raise ValueError("Unknown workload split")
    rng = random.Random({"screen": 812709, "confirm": 437941}[split])
    nonce = "seed" + str(rng.randrange(100000, 1000000))
    marker = "<!--" + nonce + "-->"
    cases = []

    def add(name, html, *, stage="pipeline", owners=None, **fields):
        fixture = {"html": html, "query": None, **fields}
        cases.append({"id": name, "stage": stage, "owners": owners or ["H01", "H02", "H04", "H05", "H10"],
                      "fixture": fixture, "lineage": "stress:" + name,
                      "provenance": "adversarial stress partition; not representative corpus"})

    # 1. stress-zipf-skewed-lengths (H02, H03)
    # 512 chunks with log-normal lengths (3..256 tokens) from a 2,048-word Zipfian
    # vocabulary spanning byte lengths 1..=24, first/last ASCII bytes a..z, and
    # multi-byte UTF-8 case-folding tokens (İ, ß, accented Latin).
    utf8_fold_tokens = ["İB", "ßeta", "Éclair", "Àlpha", "Ümlaut", "İzmir", "straße", "内容"]
    vocab = [f"term{k}" for k in range(16)] + utf8_fold_tokens
    for k in range(len(vocab), 2048):
        target_len = 1 + (k % 24)
        first_ch = chr(97 + (k % 26))
        last_ch = chr(97 + ((k // 26) % 26))
        if target_len == 1:
            word = first_ch.upper() if k % 3 == 0 else first_ch
        elif target_len == 2:
            word = first_ch + last_ch
        else:
            mid_len = target_len - 2
            mid = (f"{k:x}" * ((mid_len // 2) + 2))[:mid_len]
            word = first_ch + mid + last_ch
            if k % 5 == 0:
                word = word.upper()
        vocab.append(word)
    lognormal_lengths = [
        3, 4, 5, 6, 7, 8, 10, 12, 15, 18, 22, 27, 33, 40, 49, 60,
        74, 90, 110, 134, 164, 200, 240, 256, 19, 29, 44, 67, 102, 155, 215, 11,
    ]
    zipf_parts = [
        f"<h2>{nonce}-h0 term0</h2>",
        (
            f"<p>{nonce} "
            + " ".join(
                f"TERM{k % 10} term{k % 10} İB ßeta Éclair 内容"
                for k in range(32)
            )
            + "</p>"
        ),
    ]
    for i in range(2, 512):
        tok_count = lognormal_lengths[i % len(lognormal_lengths)]
        tag_name = "h3" if i % 16 == 0 else "p"
        words_i = [
            vocab[((j * j * 3 + i * 7 + 24) % 2024) + 24]
            for j in range(tok_count - 1)
        ]
        if i % 9 == 0:
            words_i[0] = utf8_fold_tokens[i % len(utf8_fold_tokens)]
        zipf_parts.append(f"<{tag_name}>{nonce} {' '.join(words_i)}</{tag_name}>")
    zipf_html = "".join(zipf_parts) + marker
    add(
        "stress-zipf-skewed-lengths",
        zipf_html,
        stage="prepared",
        owners=["H02", "H03"],
        query="TERM0 term1 Term2 TERM3 term4 Term5 TERM6 term7 Term8 TERM9 i\u0307b sseta éclair 内容",
        queries=8,
        invalidate_after=4,
        replacement_html=f"<h2>{nonce}-rep</h2><p>{nonce} term0 term1 term2 term3 İB ßeta Éclair 内容 replacement</p>",
        replacement_policy_revision="policy-zipf-2",
        expected_prepared_top_indices=[1, 1, 1, 1, 1, 1, 1, 1],
    )

    # 2. stress-boilerplate-novelty-trap (H02)
    # highlight_limit=8 over 512 chunks where the highest-scoring 48 chunks cycle
    # through 3 repeated boilerplate strings (with mixed ASCII case) and the
    # 464 distinct chunks sit at ranks 49..=512 (indices 48..512).
    boilerplate_variants = [
        (f"{nonce} Term0 Boilerplate AlphaBlock term0 boilerplate", f"{nonce} TERM0 boilerplate alphablock TERM0 Boilerplate"),
        (f"{nonce} TERM0 Boilerplate BetaBlock term0 boilerplate", f"{nonce} term0 boilerplate betablock Term0 BOILERPLATE"),
        (f"{nonce} term0 BOILERPLATE GammaBlock TERM0 boilerplate", f"{nonce} Term0 boilerplate gammablock term0 Boilerplate"),
    ]
    boilerplate_html = (
        "".join(
            f"<p>{boilerplate_variants[i % 3][(i // 3) % 2]}</p>"
            for i in range(48)
        )
        + "".join(
            f"<p>{nonce} term0 distinct{i:04d}</p>"
            for i in range(48, 512)
        )
        + marker
    )
    add(
        "stress-boilerplate-novelty-trap",
        boilerplate_html,
        stage="highlight",
        owners=["H02"],
        query="term0 boilerplate",
        highlight_limit=8,
        expected_selected=[0, 1, 2, 48, 49, 50, 51, 52],
    )

    # 3. stress-dense-multi-link-blocks (H04, H05)
    # 256 <p> chunks each containing 16 <a href="..."> tags (4,096 total link occurrences)
    # mixing long paths (120..240 bytes), relative paths (../), intra-chunk duplicate links
    # (50% repeat rate per chunk), non-HTTPS schemes (mailto:, javascript:), and
    # unlinked <nav>/<footer> boilerplate.
    long_seg = (
        "category-alpha-beta-gamma-delta-epsilon-zeta-eta-theta-"
        "iota-kappa-lambda-mu-nu-xi-omicron-pi-rho-sigma-tau"
    )
    dense_parts = ["<nav><div></div></nav>"]
    expected_dense_blocks = []
    expected_dense_links = []
    seen_link_to_id = {}

    def register_resolved_url(url):
        if url not in seen_link_to_id:
            seen_link_to_id[url] = len(expected_dense_links)
            expected_dense_links.append(url)
        return seen_link_to_id[url]

    for i in range(256):
        sec = f"section-{i // 16:02d}"
        unique_hrefs = []
        chunk_link_ids = []
        for k in range(6):
            item_id = (i * 6 + k) % 128
            href = f"/catalog/{sec}/{long_seg}/item-{item_id:04d}"
            unique_hrefs.append(href)
            cid = register_resolved_url(f"https://example.org{href}")
            if cid not in chunk_link_ids:
                chunk_link_ids.append(cid)
        rel_id = i % 32
        rel_href = f"sub/../catalog/{sec}/{long_seg}/rel-{rel_id:04d}"
        unique_hrefs.append(rel_href)
        rel_resolved = f"https://example.org/catalog/{sec}/{long_seg}/rel-{rel_id:04d}"
        rel_cid = register_resolved_url(rel_resolved)
        if rel_cid not in chunk_link_ids:
            chunk_link_ids.append(rel_cid)
        non_https = "mailto:skip@example.org" if i % 2 == 0 else "javascript:void(0)"
        unique_hrefs.append(non_https)
        # 16 <a> tags per <p>: 8 unique hrefs followed by the same 8 hrefs (50% intra-chunk duplicate rate)
        all_16_hrefs = unique_hrefs + unique_hrefs
        anchors_html = " ".join(
            f"<a href='{href}'>L{j}</a>" for j, href in enumerate(all_16_hrefs)
        )
        dense_parts.append(
            f"<p>{nonce} inter<i>national</i> p{i} {anchors_html}</p>"
        )
        expected_dense_blocks.append(
            {
                "role": "paragraph",
                "text": f"{nonce} international p{i} " + " ".join(f"L{j}" for j in range(16)),
                "links": chunk_link_ids,
            }
        )
    dense_parts.append("<footer><div></div></footer>")
    dense_links_html = "".join(dense_parts) + marker
    add(
        "stress-dense-multi-link-blocks",
        dense_links_html,
        stage="parse",
        owners=["H04", "H05"],
        expected_blocks=expected_dense_blocks,
        expected_links=expected_dense_links,
    )

    # 4. stress-html-table-multi-64x16 (I-HTML-TABLE, H06)
    # Multiple <table> elements combining thead/tbody/tfoot, ragged rowspan/colspan
    # geometry up to 64x16, and nested sub-tables.
    multi_tables_html = ["<!doctype html>"]
    expected_multi_tables = []
    for t_idx in range(4):
        t_id = f"grid-{t_idx}"
        t_cap = f"Grid {t_idx} {nonce}"
        multi_tables_html.append(f'<table id="{t_id}"><caption>{t_cap}</caption>')
        rows_out = []
        slots_out = []
        cell_counter = 0
        # thead: row 0 (16 cols)
        multi_tables_html.append("<thead><tr>")
        r0_cells = []
        r0_slots = []
        for c_idx in range(16):
            cid = f"t{t_idx}-h{c_idx}"
            txt = f"H{c_idx}"
            multi_tables_html.append(f'<th id="{cid}" scope="col">{txt}</th>')
            r0_cells.append(_html_cell(t_idx, 0, c_idx, "th", txt, scope="col", cell_id=cid))
            r0_slots.append(cell_counter)
            cell_counter += 1
        multi_tables_html.append("</tr></thead><tbody>")
        rows_out.append({"row_index": 0, "group": 0, "group_type": "thead", "cells": r0_cells})
        slots_out.append(r0_slots)
        # tbody: rows 1..62 (62 rows x 16 cols, with ragged colspan=2 on row 1 and rowspan=2 on row 2..3)
        row2_span_slot = None
        for r_idx in range(1, 63):
            multi_tables_html.append("<tr>")
            r_cells = []
            r_slots = []
            if r_idx == 1:
                # Ragged colspan=2 at c_idx == 1 (15 cells covering 16 columns)
                cell_pos = 0
                c_idx = 0
                while c_idx < 16:
                    txt = f"R{r_idx}C{c_idx}"
                    if c_idx == 0:
                        multi_tables_html.append(f'<th scope="row">{txt}</th>')
                        r_cells.append(_html_cell(t_idx, r_idx, cell_pos, "th", txt, scope="row"))
                        r_slots.append(cell_counter)
                        cell_counter += 1
                        c_idx += 1
                    elif c_idx == 1:
                        multi_tables_html.append(f'<td colspan="2">{txt}</td>')
                        r_cells.append(_html_cell(t_idx, r_idx, cell_pos, "td", txt, colspan=2))
                        r_slots.extend([cell_counter, cell_counter])
                        cell_counter += 1
                        c_idx += 2
                    else:
                        multi_tables_html.append(f"<td>{txt}</td>")
                        r_cells.append(_html_cell(t_idx, r_idx, cell_pos, "td", txt))
                        r_slots.append(cell_counter)
                        cell_counter += 1
                        c_idx += 1
                    cell_pos += 1
            elif r_idx == 2:
                # Ragged rowspan=2 at c_idx == 2
                for c_idx in range(16):
                    txt = f"R{r_idx}C{c_idx}"
                    if c_idx == 0:
                        multi_tables_html.append(f'<th scope="row">{txt}</th>')
                        r_cells.append(_html_cell(t_idx, r_idx, c_idx, "th", txt, scope="row"))
                    elif c_idx == 2:
                        multi_tables_html.append(f'<td rowspan="2">{txt}</td>')
                        r_cells.append(_html_cell(t_idx, r_idx, c_idx, "td", txt, rowspan=2))
                        row2_span_slot = cell_counter
                    else:
                        multi_tables_html.append(f"<td>{txt}</td>")
                        r_cells.append(_html_cell(t_idx, r_idx, c_idx, "td", txt))
                    r_slots.append(cell_counter)
                    cell_counter += 1
            elif r_idx == 3:
                # Column 2 is occupied by row 2's rowspan=2 cell
                cell_pos = 0
                for c_idx in range(16):
                    if c_idx == 2:
                        r_slots.append(row2_span_slot)
                        continue
                    txt = f"R{r_idx}C{c_idx}"
                    if c_idx == 0:
                        multi_tables_html.append(f'<th scope="row">{txt}</th>')
                        r_cells.append(_html_cell(t_idx, r_idx, cell_pos, "th", txt, scope="row"))
                    else:
                        multi_tables_html.append(f"<td>{txt}</td>")
                        r_cells.append(_html_cell(t_idx, r_idx, cell_pos, "td", txt))
                    r_slots.append(cell_counter)
                    cell_counter += 1
                    cell_pos += 1
            else:
                for c_idx in range(16):
                    txt = f"R{r_idx}C{c_idx}"
                    tag = "th" if c_idx == 0 else "td"
                    scope = "row" if c_idx == 0 else None
                    if c_idx == 0:
                        multi_tables_html.append(f'<th scope="row">{txt}</th>')
                    else:
                        multi_tables_html.append(f"<td>{txt}</td>")
                    r_cells.append(_html_cell(t_idx, r_idx, c_idx, tag, txt, scope=scope))
                    r_slots.append(cell_counter)
                    cell_counter += 1
            multi_tables_html.append("</tr>")
            rows_out.append({"row_index": r_idx, "group": 1, "group_type": "tbody", "cells": r_cells})
            slots_out.append(r_slots)
        # tfoot: row 63 (16 cols)
        multi_tables_html.append("</tbody><tfoot><tr>")
        rf_cells = []
        rf_slots = []
        for c_idx in range(16):
            txt = f"F{c_idx}"
            tag = "th" if c_idx == 0 else "td"
            scope = "row" if c_idx == 0 else None
            if c_idx == 0:
                multi_tables_html.append(f'<th scope="row">{txt}</th>')
            else:
                multi_tables_html.append(f"<td>{txt}</td>")
            rf_cells.append(_html_cell(t_idx, 63, c_idx, tag, txt, scope=scope))
            rf_slots.append(cell_counter)
            cell_counter += 1
        multi_tables_html.append("</tr></tfoot></table>")
        rows_out.append({"row_index": 63, "group": 2, "group_type": "tfoot", "cells": rf_cells})
        slots_out.append(rf_slots)
        expected_multi_tables.append(_html_table(t_idx, t_id, t_cap, rows_out, 16, slots_out))
    # Nested sub-table pair (tables 4 and 5)
    multi_tables_html.append(
        '<table id="grid-nested-outer"><tbody><tr><td>outer-before '
        '<table id="grid-nested-inner"><tbody><tr><th id="nested-th" scope="col">inner-val</th>'
        "</tr></tbody></table> outer-after</td></tr></tbody></table>"
    )
    expected_multi_tables.append(
        _html_table(
            4,
            "grid-nested-outer",
            None,
            [
                {
                    "row_index": 0,
                    "group": 0,
                    "group_type": "tbody",
                    "cells": [_html_cell(4, 0, 0, "td", "outer-before outer-after")],
                }
            ],
            1,
            [[0]],
        )
    )
    expected_multi_tables.append(
        _html_table(
            5,
            "grid-nested-inner",
            None,
            [
                {
                    "row_index": 0,
                    "group": 0,
                    "group_type": "tbody",
                    "cells": [_html_cell(5, 0, 0, "th", "inner-val", scope="col", cell_id="nested-th")],
                }
            ],
            1,
            [[0]],
        )
    )
    add(
        "stress-html-table-multi-64x16",
        "".join(multi_tables_html),
        stage="html-table",
        owners=["H06"],
        expected_html_tables=expected_multi_tables,
        expected_html_parse_warnings=0,
        max_tables=8,
        max_table_rows=256,
        max_table_cols=16,
    )

    # 5. stress-false-table-script-noise (I-PARSE-SHARE, H04, H05)
    # 512-chunk prose HTML containing <script>, <style>, <!-- ... -->, and
    # <table-viewer> tags with literal <table><tr><td> substrings but zero actual
    # <table> elements, with include_table_cells=True on pipeline stage (RT-DEEP-04).
    false_table_prefix = (
        '<script>const tpl = "<table><tr><td>noise</td></tr></table>";</script>'
        '<style>.tbl::after { content: "<table><tr><td>css</td></tr></table>"; }</style>'
        "<textarea><table><tr><td>noise</td></tr></table></textarea>"
        "<!-- <table><tr><td>comment-table</td></tr></table> -->"
        '<table-viewer data-grid="<table><tr><td>custom-element</td></tr></table>">'
    )
    false_table_blocks = [
        {"role": "paragraph", "text": f"{nonce} paragraph {i} clean text", "links": []}
        for i in range(512)
    ]
    false_table_html = (
        false_table_prefix
        + "".join(f"<p>{b['text']}</p>" for b in false_table_blocks)
        + "</table-viewer>"
        + marker
    )
    add(
        "stress-false-table-script-noise",
        false_table_html,
        stage="pipeline",
        owners=["H04", "H05"],
        include_table_cells=True,
        expected_blocks=false_table_blocks,
        expected_links=[],
    )

    # 6. stress-json-escape-burst (H01)
    # <pre> and <p> chunks alternating clean ASCII prose with dense bursts of
    # ", \, \n, \t, and control characters straddling 4 KiB / 8 KiB buffer boundaries.
    clean_ascii_run = (
        "steady ascii prose segment for vectorized fast path scanning across "
        "four kilobyte and eight kilobyte serialization buffer boundaries "
    )
    escape_burst_parts = []
    escape_burst_blocks = []
    for i in range(64):
        if i % 2 == 0:
            escape_burst_parts.append(
                f'<pre>{nonce} c{i} {clean_ascii_run}'
                f'&quot;\\&quot;\\\t\n&#x08;&#x0c;&#x1f; &lt;tag&gt; &amp; TÀI liệu 内容 日本語</pre>'
            )
            escape_burst_blocks.append(
                {
                    "role": "preformatted",
                    "text": (
                        f'{nonce} c{i} {clean_ascii_run}'
                        f'"\\"\\\t\n\x08\x0c\x1f <tag> & TÀI liệu 内容 日本語'
                    ),
                    "links": [],
                }
            )
        else:
            escape_burst_parts.append(
                f'<p>{nonce} c{i} {clean_ascii_run}'
                f'&quot;escaped&quot; \\backslash\\ &#x07;&#x1b; &amp; control</p>'
            )
            escape_burst_blocks.append(
                {
                    "role": "paragraph",
                    "text": (
                        f'{nonce} c{i} {clean_ascii_run}'
                        f'"escaped" \\backslash\\ \x07\x1b & control'
                    ),
                    "links": [],
                }
            )
    escape_burst_html = "".join(escape_burst_parts) + marker
    add(
        "stress-json-escape-burst",
        escape_burst_html,
        stage="serialization",
        owners=["H01"],
        expected_blocks=escape_burst_blocks,
        expected_links=[],
    )
    return cases


def annas_archive_cases(split):
    """Anna's Archive and optional-link (LinkMode::None / LinkMode::HighlightedOnly) benchmark cohorts."""
    if split not in ("screen", "confirm"):
        raise ValueError("Unknown workload split")
    rng = random.Random({"screen": 812709, "confirm": 437941}[split])
    nonce = "seed" + str(rng.randrange(100000, 1000000))
    marker = "<!--" + nonce + "-->"
    cases = []

    def add(name, html, *, stage="pipeline", owners=None, **fields):
        fixture = {"html": html, "query": None, **fields}
        cases.append(
            {
                "id": name,
                "stage": stage,
                "owners": owners or ["H01", "H02", "H04", "H05", "H10"],
                "fixture": fixture,
                "lineage": "annas-archive:" + name,
                "provenance": "project-authored Anna's Archive and LinkMode contract partition",
            }
        )

    # 1. annas-search-dense: visible cards + HTML-commented scroll cards + nav/footer chrome
    annas_parts = [
        "<header><nav><p>Anna's Archive navigation donate login</p></nav></header><main>",
        f"<h1>{nonce} Search results for quantum error correction</h1>",
    ]
    annas_expected_blocks = [
        {"role": "heading", "text": f"{nonce} Search results for quantum error correction", "links": []}
    ]
    annas_expected_links = []
    for i in range(32):
        md5 = f"{i + 1:032x}"
        card_html = (
            f'<p><a href="/md5/{md5}">{nonce} Book {i} Quantum Error Correction '
            f'DOI: 10.1038/s41586-024-{i:04d}-x · ISBN 978-0-387-{i:05d}-2 · pdf · 4.2MB</a></p>'
        )
        if i >= 4:
            annas_parts.append(f"<!-- {card_html} -->")
        else:
            annas_parts.append(card_html)
        annas_expected_blocks.append(
            {
                "role": "paragraph",
                "text": (
                    f"{nonce} Book {i} Quantum Error Correction "
                    f"DOI: 10.1038/s41586-024-{i:04d}-x · ISBN 978-0-387-{i:05d}-2 · pdf · 4.2MB"
                ),
                "links": [i],
            }
        )
        annas_expected_links.append(f"https://example.org/md5/{md5}")
    annas_parts.append("</main><footer><p>Footer mirror status</p></footer>" + marker)
    add(
        "annas-search-dense",
        "".join(annas_parts),
        stage="pipeline",
        owners=["H01", "H02", "H04", "H05", "H10"],
        query="Quantum Error Correction",
        highlight_limit=4,
        parse_limits={
            "comment_unwrap": "annas_archive",
            "boilerplate": "main_content_v1",
            "include_links": True,
        },
        expected_blocks=annas_expected_blocks,
        expected_links=annas_expected_links,
    )

    # 2. annas-md5-detail-table: bibliographic metadata table
    md5_hex = "0123456789abcdef0123456789abcdef"
    detail_table_html = (
        f'<!doctype html><table id="md5-metadata"><caption>Bibliographic Metadata {nonce}</caption>'
        '<thead><tr><th id="field" scope="col">Field</th><th id="val" scope="col">Value</th></tr></thead>'
        f'<tbody><tr><th scope="row">MD5</th><td headers="field val">{md5_hex}</td></tr>'
        '<tr><th scope="row">DOI</th><td headers="field val">10.1103/PhysRevLett.132.010601</td></tr>'
        '<tr><th scope="row">ISBN-13</th><td headers="field val">978-0-521-87996-5</td></tr>'
        '<tr><th scope="row">Extension</th><td headers="field val">pdf</td></tr></tbody></table>'
    )
    expected_detail_table = _html_table(
        0,
        "md5-metadata",
        f"Bibliographic Metadata {nonce}",
        [
            {
                "row_index": 0,
                "group": 0,
                "group_type": "thead",
                "cells": [
                    _html_cell(0, 0, 0, "th", "Field", scope="col", cell_id="field"),
                    _html_cell(0, 0, 1, "th", "Value", scope="col", cell_id="val"),
                ],
            },
            {
                "row_index": 1,
                "group": 1,
                "group_type": "tbody",
                "cells": [
                    _html_cell(0, 1, 0, "th", "MD5", scope="row"),
                    _html_cell(0, 1, 1, "td", md5_hex, headers=("field", "val")),
                ],
            },
            {
                "row_index": 2,
                "group": 1,
                "group_type": "tbody",
                "cells": [
                    _html_cell(0, 2, 0, "th", "DOI", scope="row"),
                    _html_cell(0, 2, 1, "td", "10.1103/PhysRevLett.132.010601", headers=("field", "val")),
                ],
            },
            {
                "row_index": 3,
                "group": 1,
                "group_type": "tbody",
                "cells": [
                    _html_cell(0, 3, 0, "th", "ISBN-13", scope="row"),
                    _html_cell(0, 3, 1, "td", "978-0-521-87996-5", headers=("field", "val")),
                ],
            },
            {
                "row_index": 4,
                "group": 1,
                "group_type": "tbody",
                "cells": [
                    _html_cell(0, 4, 0, "th", "Extension", scope="row"),
                    _html_cell(0, 4, 1, "td", "pdf", headers=("field", "val")),
                ],
            },
        ],
        2,
        [[0, 1], [2, 3], [4, 5], [6, 7], [8, 9]],
    )
    add(
        "annas-md5-detail-table",
        detail_table_html,
        stage="html-table",
        owners=["H06"],
        expected_html_tables=[expected_detail_table],
        expected_html_parse_warnings=0,
        max_tables=8,
        max_table_rows=32,
        max_table_cols=16,
    )

    # 3. export-links-none: include_links=False omits link resolution and Frame::Link records
    no_links_html = "".join(
        f'<p>{nonce} block {i} <a href="/doc/{i}">citation{i}</a></p>' for i in range(64)
    ) + marker
    add(
        "export-links-none",
        no_links_html,
        stage="pipeline",
        owners=["H01", "H04", "H05"],
        link_mode="none",
        parse_limits={"include_links": False},
        expected_blocks=[
            {"role": "paragraph", "text": f"{nonce} block {i} citation{i}", "links": []}
            for i in range(64)
        ],
        expected_links=[],
    )

    # 4. export-links-highlighted-only: emits and remaps only links cited by highlighted blocks
    hl_only_html = (
        f'<p>{nonce} background0 <a href="/bg/0">bg0</a></p>'
        f'<p>{nonce} target_alpha <a href="/hit/alpha">hitA</a></p>'
        f'<p>{nonce} background1 <a href="/bg/1">bg1</a></p>'
        + marker
    )
    add(
        "export-links-highlighted-only",
        hl_only_html,
        stage="export",
        owners=["H01"],
        query="target_alpha",
        highlight_limit=1,
        link_mode="highlighted_only",
        expected_blocks=[
            {"role": "paragraph", "text": f"{nonce} target_alpha hitA", "links": [0]},
            {"role": "paragraph", "text": f"{nonce} background0 bg0", "links": []},
            {"role": "paragraph", "text": f"{nonce} background1 bg1", "links": []},
        ],
        expected_links=["https://example.org/hit/alpha"],
    )

    # 5. parse-cetd-entropy-v1: CETD link-entropy boilerplate pruning
    cetd_menu = "".join(f'<a href="/nav/{i}">Link{i}</a> ' for i in range(16))
    cetd_html = (
        f'<div class="top-links"><p>{cetd_menu}</p></div>'
        f"<article><h1>{nonce} CETD Signal Extraction</h1>"
        f"<p>{nonce} Dense scholarly prose paragraph on content-to-tag ratio and link entropy filtering.</p></article>"
        + marker
    )
    add(
        "parse-cetd-entropy-v1",
        cetd_html,
        stage="parse",
        owners=["H04", "H05"],
        parse_limits={"boilerplate": "cetd_entropy_v1"},
        expected_blocks=[
            {"role": "heading", "text": f"{nonce} CETD Signal Extraction", "links": []},
            {
                "role": "paragraph",
                "text": f"{nonce} Dense scholarly prose paragraph on content-to-tag ratio and link entropy filtering.",
                "links": [],
            },
        ],
        expected_links=[],
    )

    # 6. select-bm25f-mslp: field-weighted BM25F + minimal-span lexical proximity selection
    bm25f_html = (
        f"<h2>{nonce} fault tolerant Logical Qubit Architecture</h2>"
        f"<p>{nonce} background noise paragraph without target terms.</p>"
        f"<p>{nonce} logical qubit syndrome decoding and fault tolerant stabilizer measurement.</p>"
        + marker
    )
    add(
        "select-bm25f-mslp",
        bm25f_html,
        stage="highlight",
        owners=["H02"],
        query="logical qubit fault tolerant",
        highlight_limit=2,
        rank_mode="bm25f",
        expected_selected=[0, 2],
    )

    # 7. select-bm25f-simhash64: BM25F + 64-bit SimHash near-duplicate suppression
    simhash_html = (
        f"<p>{nonce} surface code syndrome decoding threshold theorem quantum stabilizer check alpha</p>"
        f"<p>{nonce} surface code syndrome decoding threshold theorem quantum stabilizer check alpha!</p>"
        f"<p>{nonce} surface code syndrome decoding topological lattice surgery logical gate circuit</p>"
        + marker
    )
    add(
        "select-bm25f-simhash64",
        simhash_html,
        stage="highlight",
        owners=["H02"],
        query="surface code syndrome decoding",
        highlight_limit=2,
        rank_mode="bm25f_simhash",
        expected_selected=[0, 2],
    )

    # 8. rank-bm25-plus: Lv-Zhai lower-bounded TF BM25+ ranking
    bm25_plus_html = (
        f"<p>{nonce} unrelated classical thermodynamics paragraph</p>"
        f"<p>{nonce} topological stabilizer code and topological quantum memory</p>"
        f"<p>{nonce} stabilizer measurement circuit</p>"
        + marker
    )
    add(
        "rank-bm25-plus",
        bm25_plus_html,
        stage="rank",
        owners=["H02"],
        query="topological stabilizer",
        rank_mode="bm25_plus",
        expected_rank_order=[1, 2, 0],
    )

    # 9. rank-bm25f: Field-weighted BM25F + sliding-window MSLP (heading > paragraph with heading_context_boost)
    rank_bm25f_html = (
        f"<h2>{nonce} byzantine quorum consensus</h2>"
        f"<p>{nonce} byzantine quorum consensus</p>"
        f"<p>{nonce} byzantine filler one two three four five six byzantine</p>"
        + marker
    )
    add(
        "rank-bm25f",
        rank_bm25f_html,
        stage="rank",
        owners=["H02"],
        query="byzantine quorum",
        rank_mode="bm25f",
        expected_rank_order=[0, 1, 2],
    )

    # 10. rank-bm25l: Lv-Zhai length-normalized BM25L ranking
    add(
        "rank-bm25l",
        bm25_plus_html,
        stage="rank",
        owners=["H02"],
        query="topological stabilizer",
        rank_mode="bm25l",
        expected_rank_order=[1, 2, 0],
    )

    # 11. rank-ql-dirichlet: Query-Likelihood with Dirichlet prior smoothing
    add(
        "rank-ql-dirichlet",
        bm25_plus_html,
        stage="rank",
        owners=["H02"],
        query="topological stabilizer",
        rank_mode="ql_dirichlet",
        expected_rank_order=[1, 2, 0],
    )
    return cases


def native_cases(split, *, include_harsh_stress=False, include_annas_archive=False, include_cloud_024=False):
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

    html_cell = _html_cell
    html_table = _html_table

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
    if include_harsh_stress:
        cases.extend(harsh_stress_cases(split))
    if include_annas_archive:
        cases.extend(annas_archive_cases(split))
    if include_cloud_024:
        cases.extend(cloud_024_breakthrough_cases(split))
    return cases


def philox_4x32_10(seed, step, substream, draw):
    """Stateless counter-based Philox 4x32-10 PRNG for de-confounded Common Random Numbers (CRN)."""
    c0 = int(step) & 0xFFFFFFFF
    c1 = (int(step) >> 32) & 0xFFFFFFFF
    c2 = int(substream) & 0xFFFFFFFF
    c3 = int(draw) & 0xFFFFFFFF
    k0 = int(seed) & 0xFFFFFFFF
    k1 = (int(seed) >> 32) & 0xFFFFFFFF
    m0 = 0xD2511F53
    m1 = 0xCD9E8D57
    w0 = 0x9E3779B9
    w1 = 0xBB67AE85
    for _ in range(10):
        prod0 = m0 * c0
        prod1 = m1 * c2
        hi0 = (prod0 >> 32) & 0xFFFFFFFF
        lo0 = prod0 & 0xFFFFFFFF
        hi1 = (prod1 >> 32) & 0xFFFFFFFF
        lo1 = prod1 & 0xFFFFFFFF
        c0 = hi1 ^ c1 ^ k0
        c1 = lo1
        c2 = hi0 ^ c3 ^ k1
        c3 = lo0
        k0 = (k0 + w0) & 0xFFFFFFFF
        k1 = (k1 + w1) & 0xFFFFFFFF
    return (c0, c1, c2, c3)


def philox_u01(seed, step, substream, draw):
    """Return a stateless uniform float in (0, 1) from Philox 4x32-10."""
    c0, c1, _, _ = philox_4x32_10(seed, step, substream, draw)
    u64 = ((c0 & 0xFFFFFFFF) << 32) | (c1 & 0xFFFFFFFF)
    return ((u64 >> 11) + 0.5) * (1.0 / float(1 << 53))


def levenshtein_distance(a, b):
    """Compute character-level Levenshtein edit distance for Math/LaTeX LED evaluation."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def cloud_024_breakthrough_cases(split):
    """6 Ground-Truth Real-World Archetypes and De-Confounded CRN WAF Replay for cloud-024."""
    if split not in ("screen", "confirm"):
        raise ValueError("Unknown workload split")
    rng = random.Random({"screen": 812709, "confirm": 437941}[split])
    nonce = "seed" + str(rng.randrange(100000, 1000000))
    marker = "<!--" + nonce + "-->"
    cases = []

    def add(name, html, *, stage="cloud-024", owners=None, **fields):
        fixture = {"html": html, "query": None, **fields}
        cases.append(
            {
                "id": name,
                "stage": stage,
                "owners": owners or ["H01", "H02", "H04", "H05", "H06", "H10"],
                "fixture": fixture,
                "lineage": "cloud-024:" + name,
                "provenance": "cloud-024 real-world breakthrough archetype partition",
            }
        )

    # Archetype 1: 4-Tier Math & 22-Tag Presentation MathML-to-LaTeX + sup/sub disambiguation (D2)
    math_html = (
        f"<!doctype html><article><h1>{nonce} Relativistic Electrodynamics and Water Isotopes</h1>"
        f'<p>{nonce} Tier-1 annotation: <math><semantics><mrow><mi>E</mi><mo>=</mo><mi>m</mi>'
        '<msup><mi>c</mi><mn>2</mn></msup></mrow><annotation encoding="application/x-tex">'
        "E = mc^2</annotation></semantics></math></p>"
        f'<p>{nonce} Tier-2 attribute: <math alttext="\\nabla \\cdot \\mathbf{{B}} = 0">'
        "<mi>B</mi></math></p>"
        f"<p>{nonce} Tier-3 Presentation MathML: <math><mrow><msup><mi>x</mi><mn>2</mn></msup>"
        "<mo>+</mo><mfrac><mrow><mi>a</mi><mo>+</mo><mi>b</mi></mrow>"
        "<msub><mi>y</mi><mi>i</mi></msub></mfrac><mo>=</mo><msqrt><mi>z</mi></msqrt></mrow></math></p>"
        f"<p>{nonce} Chemical and algebraic scripts: H<sub>2</sub>O and x<sup>2k+1</sup> "
        'cited in theorem<sup><a href="#ref12">[12]</a></sup>.</p></article>' + marker
    )
    add(
        "cloud-024-mathml-latex-led",
        math_html,
        owners=["H04", "H05"],
        archetype="mathml_latex_led",
        expected_cloud_024={
            "archetype": "mathml_latex_led",
            "expected_substrings": [
                "$E = mc^2$",
                "$\\nabla \\cdot \\mathbf{B} = 0$",
                "$x^{2} + \\frac{a + b}{y_{i}} = \\sqrt{z}$",
                "H_{2}O",
                "x^{2k+1}",
                "[^12]",
            ],
            "max_math_led": 0.0,
        },
    )

    # Archetype 2: WHATWG §4.9.12 Tolerant Table Expansion + 2D Grounding + Nav-Table vs Data-Table (M2, D10, D11, R2-5)
    table_html = (
        "<!doctype html>"
        '<table id="nav-layout"><tbody><tr><td><a href="/home">Home</a></td>'
        '<td><a href="/about">About</a></td></tr></tbody></table>'
        f"<main><h1>{nonce} Benchmark Telemetry</h1>"
        '<table id="telemetry"><caption>Telemetry Summary</caption>'
        "<tfoot><tr><th>Total</th><td>2100</td></tr></tfoot>"
        "<thead><tr><th>Contender</th><th>Latency p99</th></tr></thead>"
        '<tbody><tr><th rowspan="10">Athena</th><td colspan="50">1.79 ms</td></tr>'
        "<tr><td>pdf</td></tr></tbody></table></main>" + marker
    )
    add(
        "cloud-024-tolerant-table-teds",
        table_html,
        owners=["H04", "H05", "H06"],
        archetype="tolerant_table_teds",
        expected_cloud_024={
            "archetype": "tolerant_table_teds",
            "expected_grounded_cells": [
                "[Contender = Athena | Latency p99]: 1.79 ms",
                "[Contender = Athena | Latency p99]: pdf",
                "[Contender = Total | Latency p99]: 2100",
            ],
            "forbidden_substrings": ["Home", "About"],
            "min_table_teds": 1.0,
        },
    )

    # Archetype 3: AST-Clean <pre><code> Sanitizer + Line-Boundary Continuation Splitting (M1, D1, D31)
    code_html = (
        f"<!doctype html><main><h2>{nonce} Reference Implementation</h2>"
        '<pre><code class="language-python">'
        '<span class="linenos">1\n2\n3</span>'
        '<button class="copy-button">Copy</button>'
        "def verify_invariant(x):\n    if x &gt; 0:\n        return x * 2\n    return 0"
        "</code></pre></main>" + marker
    )
    add(
        "cloud-024-code-ast-sanitization",
        code_html,
        owners=["H01", "H04", "H05"],
        archetype="code_ast_sanitization",
        expected_cloud_024={
            "archetype": "code_ast_sanitization",
            "expected_code_language": "python",
            "expected_code_exact": "def verify_invariant(x):\n    if x > 0:\n        return x * 2\n    return 0",
            "forbidden_substrings": ["Copy", "1\n2\n3"],
            "code_ast_valid": True,
        },
    )

    # Archetype 4: Orphan Prose Capture + <article><header> Abstract Preservation + NFC + Lead-in Discourse (D10, D19, D21, D22)
    orphan_html = (
        "<!doctype html><article>"
        f"<header><h1>{nonce} Quantum LDPC</h1>"
        "<p>Abstract: Quantum LDPC codes achieve constant rate and linear distance in re\u0301sume\u0301 form.</p></header>"
        "<div>Introductory theorem lead-in:<p>Nested proof detail paragraph.</p>Trailing corollary text.</div>"
        "<dl><dt>Syndrome</dt><dd>Stabilizer parity check measurement.</dd></dl>"
        "<blockquote>Quoted fault-tolerance threshold theorem.</blockquote>"
        "<figcaption>Figure 1: Logical error rate vs physical error rate.</figcaption>"
        "</article>" + marker
    )
    add(
        "cloud-024-orphan-prose-discourse-cleaneval",
        orphan_html,
        owners=["H04", "H05"],
        archetype="orphan_prose_discourse_cleaneval",
        expected_cloud_024={
            "archetype": "orphan_prose_discourse_cleaneval",
            "expected_substrings": [
                "Abstract: Quantum LDPC codes achieve constant rate and linear distance in résumé form.",
                "Introductory theorem lead-in:",
                "Nested proof detail paragraph.",
                "Trailing corollary text.",
                "Syndrome",
                "Stabilizer parity check measurement.",
                "Quoted fault-tolerance threshold theorem.",
                "Figure 1: Logical error rate vs physical error rate.",
            ],
            "require_lead_in_link": True,
            "min_cleaneval_f1": 0.99,
        },
    )

    # Archetype 5: Saturation-Free Multi-Hop Graded IR + Guarded Dual-Channel Stemmer + aae/2 Contrastive Export (D5, D6, D22, D23, D24, D30)
    ir_html = (
        f"<!doctype html><main><h1>{nonce} Distributed Consensus</h1>"
        "<h2>Adaptive Proxy Routing</h2><h3>Hierarchical Empirical Bayes</h3>"
        "<p>The SoftBlockDetector module regularizes empirical survival probabilities across ASN and subnet tiers:</p>"
        "<p> Specifically, regularized beta priors prevent cold-start variance during proxy_pool_router rotation.</p>"
        "<p>The SoftBlockDetector module regularizes empirical survival probabilities across ASN and subnet tiers!</p>"
        "<p>Classical thermodynamic equilibrium in ideal gas reservoirs under isothermal expansion.</p>"
        "<p>Unrelated bibliographic catalog entry for medieval botanical manuscripts and parchment bindings.</p>"
        "</main>" + marker
    )
    add(
        "cloud-024-multihop-ir-contrastive-aae2",
        ir_html,
        owners=["H01", "H02", "H10"],
        archetype="multihop_ir_contrastive_aae2",
        query="regularization SoftBlockDetector",
        expected_cloud_024={
            "archetype": "multihop_ir_contrastive_aae2",
            "query": "regularization SoftBlockDetector",
            "expected_top_chunk_contains": "SoftBlockDetector module regularizes",
            "require_contrastive_records": True,
            "require_merkle_provenance": True,
        },
    )

    # Archetype 6: De-Confounded 7-Vendor WAF Soft-Block Lattice & Philox CRN Bandit Replay (M3, M4, D12, D13, D18, R2-3, R2-4)
    waf_html = (
        f"<!doctype html><main><p>{nonce} Authentic scholarly payload after WAF clearance.</p></main>" + marker
    )
    add(
        "cloud-024-deconfounded-waf-crn-replay",
        waf_html,
        owners=["H07", "H08", "H10"],
        archetype="deconfounded_waf_crn_replay",
        expected_cloud_024={
            "archetype": "deconfounded_waf_crn_replay",
            "expected_soft_block_recall": 1.0,
            "expected_false_positive_rate": 0.0,
            "expected_cache_poisoning_rate": 0.0,
            "require_crn_orthogonality": True,
            "require_separate_latency_distributions": True,
        },
    )
    return cases


def envelope_errors(raw, fixture):
    """Gold is literal fixture data, never generated by the candidate/reference."""
    if fixture.get("expected_error") is not None:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical resource-denial observation"]
        return [] if value == {"status": "EXPECTED_ERROR", "error": fixture["expected_error"]} else ["Wrong denial or unexpected success"]
    if "expected_cloud_024" in fixture:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical cloud-024 breakthrough observation"]
        if not isinstance(value, dict) or value.get("schema") != "athena-cloud-024-breakthrough/1":
            return ["Invalid cloud-024 breakthrough schema"]
        spec = fixture["expected_cloud_024"]
        errors = []
        if value.get("archetype") != spec.get("archetype"):
            errors.append("cloud-024 archetype mismatch")
        combined_text = "\n".join(c.get("text", "") for c in value.get("chunks", []) if isinstance(c, dict))
        for req in spec.get("expected_substrings", ()):
            if req not in combined_text:
                errors.append(f"Missing expected substring: {req}")
        for forb in spec.get("forbidden_substrings", ()):
            if forb in combined_text:
                errors.append(f"Retained forbidden substring: {forb}")
        for grounded in spec.get("expected_grounded_cells", ()):
            if grounded not in combined_text:
                errors.append(f"Missing grounded table cell: {grounded}")
        if "expected_code_exact" in spec:
            code_chunks = [c for c in value.get("chunks", []) if isinstance(c, dict) and c.get("role") == "preformatted"]
            if not code_chunks or code_chunks[0].get("text") != spec["expected_code_exact"]:
                errors.append("Sanitized code block text differs from AST-clean oracle")
            if spec.get("expected_code_language") and (
                not code_chunks or code_chunks[0].get("code_language") != spec["expected_code_language"]
            ):
                errors.append("Sanitized code block language differs")
        if spec.get("require_lead_in_link"):
            if not any(isinstance(c, dict) and c.get("lead_in_chunk_id") is not None for c in value.get("chunks", [])):
                errors.append("Missing lead_in_chunk_id discourse binding")
        if "expected_top_chunk_contains" in spec:
            top_text = value.get("top_ranked_chunk_text", "")
            if spec["expected_top_chunk_contains"] not in top_text:
                errors.append("Top-ranked chunk failed guarded dual-channel stemmer/compound oracle")
        if spec.get("require_contrastive_records"):
            records = value.get("contrastive_records", [])
            if not isinstance(records, list) or not records:
                errors.append("Missing aae/2 contrastive training records")
            elif spec.get("require_merkle_provenance"):
                prov = records[0].get("provenance", {})
                if not isinstance(prov, dict) or len(str(prov.get("merkle_hash_hex", ""))) != 64:
                    errors.append("Invalid MerkleProvenanceTuple in aae/2 contrastive record")
        if "expected_soft_block_recall" in spec:
            if value.get("soft_block_recall") != spec["expected_soft_block_recall"]:
                errors.append("7-vendor soft-block recall below 1.0")
            if value.get("false_positive_rate") != spec["expected_false_positive_rate"]:
                errors.append("Soft-block false-positive rate above 0.0")
            if value.get("cache_poisoning_rate") != spec["expected_cache_poisoning_rate"]:
                errors.append("Soft-block cache poisoning rate above 0.0")
            if spec.get("require_crn_orthogonality") and value.get("crn_substream_orthogonality") is not True:
                errors.append("Philox CRN substream orthogonality failed")
            if spec.get("require_separate_latency_distributions") and (
                value.get("usable_latency_p99_ms", 0.0) <= 0.0
                or value.get("block_latency_p99_ms", 0.0) <= 0.0
            ):
                errors.append("Separate usable vs block latency p99 distributions missing")
        return errors
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
    if "expected_prepared_top_indices" in fixture:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical prepared-ranking observation"]
        if not isinstance(value, list) or any(not isinstance(run, list) or not run for run in value):
            return ["Malformed prepared-ranking observation"]
        top_indices = [
            run[0][0]
            if isinstance(run[0], list) and run[0]
            else None
            for run in value
        ]
        return [] if top_indices == fixture["expected_prepared_top_indices"] else ["Prepared top-index oracle differs"]
    if "expected_selected" in fixture:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical highlight-selection observation"]
        return [] if value == fixture["expected_selected"] else ["Highlight selection oracle differs"]
    if "expected_rank_order" in fixture:
        try:
            value = strict_json_loads(raw)
        except (ValueError, UnicodeError):
            return ["Expected a canonical ranking observation"]
        if not isinstance(value, list) or any(
            not isinstance(item, list) or len(item) != 2 for item in value
        ):
            return ["Malformed ranking observation"]
        rank_order = [item[0] for item in value]
        return [] if rank_order == fixture["expected_rank_order"] else ["Rank order oracle differs"]
    if "expected_blocks" not in fixture and "expected_links" not in fixture:
        return []
    try:
        frames = [strict_json_loads(line) for line in raw.splitlines()]
        if len(frames) == 1 and isinstance(frames[0], dict) and "chunks" in frames[0] and "links" in frames[0]:
            proj = frames[0]
            blocks = [
                {key: chunk[key] for key in ("role", "text", "links")}
                for chunk in proj["chunks"]
            ]
            links = proj["links"]
            errors = []
            if "expected_blocks" in fixture and blocks != fixture["expected_blocks"]:
                errors.append("Literal role/text/link ownership differs")
            if "expected_links" in fixture and links != fixture["expected_links"]:
                errors.append("Literal resolved link identity differs")
            return errors
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
