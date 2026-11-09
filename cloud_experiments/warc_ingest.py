"""Streaming Common Crawl WARC intake: gzip-member reader, HTTP parsing and UTF-8 transcoding.

Nothing is unpacked to disk. Each WARC record is one gzip member in Common Crawl
files; the reader tracks every member's compressed offset so a record can be
re-read later (resume cursor, determinism canary) without rescanning the file.

Engine constraints handled here (dataset plan, "Điều kiện kỹ thuật"):
- ``orchestrator.rs`` rejects non-UTF-8 input: pages are transcoded to UTF-8 and
  the original charset, its evidence source and the decoder errors are recorded.
- ``DEFAULT_MAX_HTML_BYTES`` is 4 MiB: pages over it after transcoding, and
  WARC-truncated payloads, are reported explicitly (never silently dropped).

Charset resolution follows the WHATWG order (BOM, HTTP ``charset``, ``<meta>``
prescan, then a deterministic sniff), with WHATWG label aliases (e.g.
``iso-8859-1``/``ascii`` -> windows-1252). One documented deviation: a declared
single-byte legacy charset is overridden by UTF-8 when the bytes are valid UTF-8
and contain multibyte sequences (``utf8_override_of_declared``), because that is
overwhelmingly a mislabelled UTF-8 page; the decision is recorded per page.
"""
from __future__ import annotations

import argparse
import codecs
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import html as html_lib
import json
from pathlib import Path
import re
import sys
import time
import zlib

MAX_HTML_BYTES = 4 * 1024 * 1024  # src/projection.rs DEFAULT_MAX_HTML_BYTES
MAX_MEMBER_BYTES = 64 * 1024 * 1024
READ_CHUNK = 1024 * 1024
HTML_TYPES = ("text/html", "application/xhtml+xml")


class WarcFormatError(ValueError):
    pass


# ----------------------------------------------------------------------------- gzip members

@dataclass
class Member:
    offset: int
    length: int
    data: bytes
    oversize: bool = False
    decompressed_bytes: int = 0


def iter_gzip_members(stream, *, start_offset: int = 0, max_member_bytes: int = MAX_MEMBER_BYTES,
                      max_members: int | None = None, sha256=None):
    """Yield every gzip member of a concatenated-member file with its compressed offset.

    A member whose decompressed size exceeds ``max_member_bytes`` is yielded with
    ``oversize=True`` and only its first ``max_member_bytes`` bytes.
    ``sha256`` (a hashlib object) is updated with every compressed byte read, which
    gives the whole-file digest during a full scan from offset 0.
    """
    stream.seek(start_offset)
    offset = start_offset
    pending = b""
    yielded = 0
    while max_members is None or yielded < max_members:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out = bytearray()
        total_out = 0
        consumed = 0
        oversize = False
        data = pending
        pending = b""
        started = False
        while True:
            if not data:
                data = stream.read(READ_CHUNK)
                if sha256 is not None and data:
                    sha256.update(data)
                if not data:
                    if not started:
                        return
                    raise WarcFormatError(f"truncated gzip member at offset {offset}")
            started = True
            chunk_in = data
            data = b""
            while chunk_in:
                piece = decoder.decompress(chunk_in, READ_CHUNK)
                total_out += len(piece)
                if not oversize:
                    room = max_member_bytes - len(out)
                    if len(piece) > room:
                        out += piece[:room]
                        oversize = True
                    else:
                        out += piece
                if decoder.eof:
                    unused = decoder.unused_data
                    consumed += len(chunk_in) - len(unused)
                    pending = unused
                    chunk_in = b""
                    break
                tail = decoder.unconsumed_tail
                consumed += len(chunk_in) - len(tail)
                chunk_in = tail
            if decoder.eof:
                break
        yield Member(offset, consumed, bytes(out), oversize, total_out)
        yielded += 1
        offset += consumed


def read_member_at(path, offset: int, *, max_member_bytes: int = MAX_MEMBER_BYTES) -> Member:
    with open(path, "rb") as stream:
        for member in iter_gzip_members(stream, start_offset=offset, max_member_bytes=max_member_bytes,
                                        max_members=1):
            return member
    raise WarcFormatError(f"no gzip member at offset {offset}")


# ----------------------------------------------------------------------------- WARC + HTTP

def _parse_header_block(raw: bytes) -> dict:
    headers: dict[str, str] = {}
    last = None
    for line in raw.split(b"\r\n"):
        if not line:
            continue
        if line[:1] in (b" ", b"\t") and last is not None:
            headers[last] += " " + line.strip().decode("latin-1")
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            continue
        last = name.strip().decode("latin-1").lower()
        text = value.strip().decode("latin-1")
        headers[last] = text if last not in headers else headers[last] + ", " + text
    return headers


@dataclass
class WarcRecord:
    offset: int
    length: int
    version: str
    headers: dict
    block: bytes
    block_truncated_by_reader: bool = False

    @property
    def type(self) -> str:
        return self.headers.get("warc-type", "")


def parse_warc_record(member: Member) -> WarcRecord:
    data = member.data
    end = data.find(b"\r\n\r\n")
    if end < 0 or not data.startswith(b"WARC/"):
        raise WarcFormatError(f"member at {member.offset} is not a WARC record")
    first, _, rest = data[:end].partition(b"\r\n")
    headers = _parse_header_block(rest)
    try:
        length = int(headers.get("content-length", ""))
    except ValueError as error:
        raise WarcFormatError(f"record at {member.offset} lacks Content-Length") from error
    block = data[end + 4:end + 4 + length]
    short = len(block) < length
    if short and not member.oversize:
        raise WarcFormatError(f"record at {member.offset} block shorter than Content-Length")
    return WarcRecord(member.offset, member.length, first.decode("latin-1"), headers, block, short)


@dataclass
class HttpResponse:
    status: int | None
    headers: dict
    payload: bytes


def parse_http_response(block: bytes) -> HttpResponse:
    end = block.find(b"\r\n\r\n")
    sep = 4
    if end < 0:
        end = block.find(b"\n\n")
        sep = 2
    if end < 0:
        head, payload = block, b""
    else:
        head, payload = block[:end], block[end + sep:]
    first, _, rest = head.replace(b"\r\n", b"\n").partition(b"\n")
    match = re.match(rb"HTTP/\d(?:\.\d)?\s+(\d{3})", first)
    status = int(match.group(1)) if match else None
    headers = _parse_header_block(rest.replace(b"\n", b"\r\n"))
    return HttpResponse(status, headers, payload)


def _dechunk(payload: bytes) -> bytes | None:
    out = bytearray()
    position = 0
    while True:
        line_end = payload.find(b"\r\n", position)
        if line_end < 0:
            return None
        try:
            size = int(payload[position:line_end].split(b";")[0].strip(), 16)
        except ValueError:
            return None
        position = line_end + 2
        if size == 0:
            return bytes(out)
        out += payload[position:position + size]
        position += size + 2


def decode_content(response: HttpResponse) -> tuple[bytes, str | None]:
    """Undo transfer/content encodings that were stored un-decoded (CC normally decodes)."""
    payload = response.payload
    note = None
    if "chunked" in response.headers.get("transfer-encoding", "").lower():
        dechunked = _dechunk(payload)
        if dechunked is not None:
            payload, note = dechunked, "dechunked"
    encoding = response.headers.get("content-encoding", "").lower().strip()
    if encoding in ("gzip", "x-gzip", "deflate"):
        try:
            wbits = 16 + zlib.MAX_WBITS if "gzip" in encoding else zlib.MAX_WBITS
            payload = zlib.decompressobj(wbits).decompress(payload, MAX_MEMBER_BYTES)
            note = (note + "+" if note else "") + encoding
        except zlib.error:
            note = (note + "+" if note else "") + f"{encoding}-undecodable"
    elif encoding and encoding != "identity":
        note = (note + "+" if note else "") + f"{encoding}-unsupported"
    return payload, note


# ----------------------------------------------------------------------------- charset

_WHATWG = {}
for _labels, _codec in (
    (("unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8", "utf-8", "utf8", "x-unicode20utf8"), "utf-8"),
    (("ansi_x3.4-1968", "ascii", "cp1252", "cp819", "csisolatin1", "ibm819", "iso-8859-1", "iso-ir-100",
      "iso8859-1", "iso88591", "iso_8859-1", "iso_8859-1:1987", "l1", "latin1", "us-ascii",
      "windows-1252", "x-cp1252"), "cp1252"),
    (("chinese", "csgb2312", "csiso58gb231280", "gb2312", "gb_2312", "gb_2312-80", "gbk", "iso-ir-58",
      "x-gbk"), "gbk"),
    (("gb18030",), "gb18030"),
    (("big5", "big5-hkscs", "cn-big5", "csbig5", "x-x-big5"), "big5hkscs"),
    (("csshiftjis", "ms932", "ms_kanji", "shift-jis", "shift_jis", "sjis", "windows-31j", "x-sjis"), "cp932"),
    (("cseucpkdfmtjapanese", "euc-jp", "x-euc-jp"), "euc_jp"),
    (("csiso2022jp", "iso-2022-jp"), "iso2022_jp"),
    (("cseuckr", "csksc56011987", "euc-kr", "iso-ir-149", "korean", "ks_c_5601-1987", "ks_c_5601-1989",
      "ksc5601", "ksc_5601", "windows-949"), "cp949"),
    (("dos-874", "iso-8859-11", "iso8859-11", "iso885911", "tis-620", "windows-874"), "cp874"),
    (("csisolatin5", "iso-8859-9", "iso-ir-148", "iso8859-9", "iso88599", "iso_8859-9", "l5", "latin5",
      "windows-1254", "x-cp1254"), "cp1254"),
    (("cskoi8r", "koi", "koi8", "koi8-r", "koi8_r"), "koi8_r"),
    (("koi8-ru", "koi8-u"), "koi8_u"),
    (("utf-16", "utf-16le"), "utf-16-le"),
    (("utf-16be",), "utf-16-be"),
):
    for _label in _labels:
        _WHATWG[_label] = _codec
for _n in range(1250, 1259):
    _WHATWG.setdefault(f"windows-{_n}", f"cp{_n}")
    _WHATWG.setdefault(f"x-cp{_n}", f"cp{_n}")
for _n in (2, 3, 4, 5, 6, 7, 8, 10, 13, 14, 15, 16):
    for _label in (f"iso-8859-{_n}", f"iso8859-{_n}", f"iso8859{_n}", f"iso_8859-{_n}"):
        _WHATWG.setdefault(_label, f"iso8859_{_n}")
_WHATWG.setdefault("iso-8859-8-i", "iso8859_8")
_WHATWG.setdefault("x-mac-cyrillic", "mac_cyrillic")
_WHATWG.setdefault("macintosh", "mac_roman")
_SINGLE_BYTE = {codec for codec in _WHATWG.values()
                if codec.startswith(("cp12", "iso8859", "koi8", "mac_", "cp874"))}

_META_CHARSET = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_:.+-]{1,40})""", re.I)
_CT_CHARSET = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9_:.+-]{1,40})""", re.I)
_DECODE_ERRORS = [0]


def _count_replace(error):
    _DECODE_ERRORS[0] += 1
    return "\ufffd", error.end


codecs.register_error("athena-count-replace", _count_replace)


def whatwg_codec(label: str | None) -> str | None:
    if not label:
        return None
    return _WHATWG.get(label.strip().strip("'\"").lower())


@dataclass
class Transcoded:
    text: str
    utf8_bytes: int
    codec: str
    source: str
    declared_http: str | None
    declared_meta: str | None
    decode_errors: int
    decision: str


def _is_valid_utf8(payload: bytes) -> bool:
    try:
        payload.decode("utf-8", "strict")
        return True
    except UnicodeDecodeError:
        return False


def transcode_to_utf8(payload: bytes, content_type: str | None) -> Transcoded:
    """Decode an HTML payload to text following the documented charset policy."""
    http_label = None
    if content_type:
        match = _CT_CHARSET.search(content_type)
        http_label = match.group(1) if match else None
    meta_match = _META_CHARSET.search(payload[:4096])
    meta_label = meta_match.group(1).decode("ascii", "replace") if meta_match else None
    decision = "declared"
    if payload.startswith(codecs.BOM_UTF8):
        codec, source, payload = "utf-8", "bom", payload[3:]
    elif payload.startswith(codecs.BOM_UTF16_LE):
        codec, source, payload = "utf-16-le", "bom", payload[2:]
    elif payload.startswith(codecs.BOM_UTF16_BE):
        codec, source, payload = "utf-16-be", "bom", payload[2:]
    elif whatwg_codec(http_label):
        codec, source = whatwg_codec(http_label), "http"
    elif whatwg_codec(meta_label):
        codec, source = whatwg_codec(meta_label), "meta"
        if codec.startswith("utf-16"):  # WHATWG: a meta-declared UTF-16 means UTF-8
            codec = "utf-8"
    elif _is_valid_utf8(payload):
        codec, source, decision = "utf-8", "sniff", "sniffed_valid_utf8"
    else:
        codec, source, decision = "cp1252", "fallback", "fallback_windows_1252"
    if source in ("http", "meta") and codec in _SINGLE_BYTE and _is_valid_utf8(payload) \
            and any(byte >= 0x80 for byte in payload):
        codec, decision = "utf-8", "utf8_override_of_declared"
    _DECODE_ERRORS[0] = 0
    text = payload.decode(codec, "athena-count-replace")
    errors = _DECODE_ERRORS[0]
    return Transcoded(text, len(text.encode("utf-8", "surrogatepass")), codec, source, http_label,
                      meta_label, errors, decision)


# ----------------------------------------------------------------------------- page iteration

_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)


def page_title(text: str, limit: int = 256) -> str:
    match = _TITLE.search(text[:262144])
    if match is None:
        return ""
    title = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]*>", " ", match.group(1)))).strip()
    return title[:limit]


@dataclass
class Page:
    """One WARC response record classified for intake."""
    offset: int
    length: int
    record_id: str
    target_uri: str
    warc_date: str
    http_status: int | None
    content_type: str
    identified_type: str
    payload_bytes: int
    payload_sha1: str | None
    truncated: str | None
    status: str  # OK | NON_HTML | NON_200 | OVERSIZE | ENCODING_UNSUPPORTED | READER_OVERSIZE
    transcoded: Transcoded | None = None
    content_note: str | None = None

    def source_metadata(self, warc_relpath: str) -> dict:
        meta = {
            "warc": warc_relpath, "member_offset": self.offset, "member_length": self.length,
            "warc_record_id": self.record_id, "target_uri": self.target_uri, "warc_date": self.warc_date,
            "http_status": self.http_status, "content_type": self.content_type,
            "identified_payload_type": self.identified_type, "payload_bytes": self.payload_bytes,
            "payload_digest": self.payload_sha1, "warc_truncated": self.truncated,
            "content_note": self.content_note,
        }
        if self.transcoded is not None:
            meta["charset"] = {
                "codec": self.transcoded.codec, "source": self.transcoded.source,
                "declared_http": self.transcoded.declared_http, "declared_meta": self.transcoded.declared_meta,
                "decode_errors": self.transcoded.decode_errors, "decision": self.transcoded.decision,
                "utf8_bytes": self.transcoded.utf8_bytes,
            }
        return meta


def _is_html(content_type: str, identified: str) -> bool:
    mime = content_type.split(";")[0].strip().lower()
    return mime in HTML_TYPES or identified.split(";")[0].strip().lower() in HTML_TYPES


def classify_record(record: WarcRecord, *, max_html_bytes: int = MAX_HTML_BYTES) -> Page | None:
    """Return a Page for response records, None for warcinfo/request/metadata records."""
    if record.type != "response":
        return None
    response = parse_http_response(record.block)
    content_type = response.headers.get("content-type", "")
    identified = record.headers.get("warc-identified-payload-type", "")
    payload, note = decode_content(response)
    page = Page(record.offset, record.length, record.headers.get("warc-record-id", ""),
                record.headers.get("warc-target-uri", ""), record.headers.get("warc-date", ""),
                response.status, content_type, identified, len(payload),
                record.headers.get("warc-payload-digest"), record.headers.get("warc-truncated"),
                "OK", None, note)
    if record.block_truncated_by_reader:
        page.status = "READER_OVERSIZE"
    elif response.status != 200:
        page.status = "NON_200"
    elif not _is_html(content_type, identified):
        page.status = "NON_HTML"
    elif note and ("unsupported" in note or "undecodable" in note):
        page.status = "ENCODING_UNSUPPORTED"
    elif len(payload) > max_html_bytes * 4:
        page.status = "OVERSIZE"
    else:
        page.transcoded = transcode_to_utf8(payload, content_type)
        if page.transcoded.utf8_bytes > max_html_bytes:
            page.status = "OVERSIZE"
    return page


def iter_pages(path, *, start_offset: int = 0, max_html_bytes: int = MAX_HTML_BYTES, sha256=None,
               on_record=None):
    """Yield ``(member, Page)`` for each response record from ``start_offset``."""
    with open(path, "rb") as stream:
        for member in iter_gzip_members(stream, start_offset=start_offset, sha256=sha256):
            record = parse_warc_record(member)
            if on_record is not None:
                on_record(record)
            page = classify_record(record, max_html_bytes=max_html_bytes)
            if page is not None:
                yield member, page


def warc_date_unix_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


# ----------------------------------------------------------------------------- Step 2 measurement

_SIZE_BUCKETS = ((16 << 10, "<16KiB"), (64 << 10, "16-64KiB"), (256 << 10, "64-256KiB"),
                 (1 << 20, "256KiB-1MiB"), (4 << 20, "1-4MiB"))


def _bucket(size: int) -> str:
    for limit, name in _SIZE_BUCKETS:
        if size < limit:
            return name
    return ">=4MiB"


def _percentiles(values: list[int]) -> dict:
    if not values:
        return {}
    ordered = sorted(values)

    def pick(q):
        return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]

    return {"p50": pick(0.5), "p90": pick(0.9), "p99": pick(0.99), "p999": pick(0.999), "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 1)}


def measure(path, *, expected_bytes: int | None = None, max_records: int | None = None,
            progress_every: int = 0) -> dict:
    """Charset and size distribution of one WARC file (dataset plan step 2)."""
    path = Path(path)
    started = time.monotonic()
    file_bytes = path.stat().st_size
    digest = hashlib.sha256()
    record_types, statuses, page_status, mime = Counter(), Counter(), Counter(), Counter()
    codec_counter, source_counter, decision_counter, declared = Counter(), Counter(), Counter(), Counter()
    raw_buckets, utf8_buckets, truncated_reasons, notes = Counter(), Counter(), Counter(), Counter()
    raw_sizes, utf8_sizes, examples = [], [], {}
    pages_with_errors = 0
    utf8_growth = 0
    records = 0

    def on_record(record):
        nonlocal records
        records += 1
        record_types[record.type] += 1

    for _member, page in iter_pages(path, sha256=digest if max_records is None else None,
                                    on_record=on_record):
        statuses[str(page.http_status)] += 1
        if page.http_status == 200:
            mime[(page.content_type.split(";")[0].strip().lower() or "(none)")[:60]] += 1
        page_status[page.status] += 1
        if page.content_note:
            notes[page.content_note] += 1
        is_html_200 = page.status in ("OK", "OVERSIZE") and page.transcoded is not None
        if page.truncated:
            truncated_reasons[page.truncated] += 1
            if is_html_200:
                truncated_reasons["html_200:" + page.truncated] += 1
        if is_html_200:
            raw_sizes.append(page.payload_bytes)
            raw_buckets[_bucket(page.payload_bytes)] += 1
            t = page.transcoded
            utf8_sizes.append(t.utf8_bytes)
            utf8_buckets[_bucket(t.utf8_bytes)] += 1
            utf8_growth += t.utf8_bytes > page.payload_bytes
            codec_counter[t.codec] += 1
            source_counter[t.source] += 1
            decision_counter[t.decision] += 1
            declared[(t.declared_http or t.declared_meta or "(none)").lower()[:40]] += 1
            pages_with_errors += t.decode_errors > 0
            if t.codec != "utf-8" and t.codec not in examples:
                examples[t.codec] = page.target_uri[:200]
        if progress_every and records % progress_every == 0:
            print(json.dumps({"progress_records": records, "elapsed_s": round(time.monotonic() - started)}),
                  file=sys.stderr, flush=True)
        if max_records is not None and records >= max_records:
            break
    html_pages = len(raw_sizes)
    report = {
        "schema": "athena-warc-charset-size/1", "warc": str(path.name), "file_bytes": file_bytes,
        "expected_bytes": expected_bytes,
        "size_matches_pin": None if expected_bytes is None else file_bytes == expected_bytes,
        "sha256": digest.hexdigest() if max_records is None else None,
        "complete_scan": max_records is None, "warc_records": records,
        "record_types": dict(record_types), "response_http_status": dict(statuses.most_common()),
        "response_mime_top": dict(mime.most_common(15)), "page_status": dict(page_status),
        "content_notes": dict(notes),
        "html_200_pages": html_pages,
        "charset": {
            "final_codec": dict(codec_counter.most_common()),
            "evidence_source": dict(source_counter.most_common()),
            "decision": dict(decision_counter.most_common()),
            "declared_label_top": dict(declared.most_common(20)),
            "non_utf8_pages": html_pages - codec_counter.get("utf-8", 0),
            "non_utf8_share": round((html_pages - codec_counter.get("utf-8", 0)) / html_pages, 6) if html_pages else None,
            "pages_with_decode_errors": pages_with_errors,
            "examples_non_utf8": examples,
        },
        "size": {
            "raw_payload_bytes": _percentiles(raw_sizes), "utf8_bytes": _percentiles(utf8_sizes),
            "raw_buckets": dict(raw_buckets), "utf8_buckets": dict(utf8_buckets),
            "over_4mib_utf8": sum(size > MAX_HTML_BYTES for size in utf8_sizes),
            "pages_grown_by_transcoding": utf8_growth,
        },
        "warc_truncated": dict(truncated_reasons),
        "elapsed_s": round(time.monotonic() - started, 1),
        "python": sys.version.split()[0],
    }
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure")
    m.add_argument("warc")
    m.add_argument("--expected-bytes", type=int)
    m.add_argument("--max-records", type=int)
    m.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        import os
        os.nice(10)
    except (AttributeError, OSError):
        pass
    report = measure(args.warc, expected_bytes=args.expected_bytes, max_records=args.max_records,
                     progress_every=20000)
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
