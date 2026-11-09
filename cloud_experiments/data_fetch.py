"""Pinned, resumable, rate-limited and verified research-data downloader.

Release policy (owner decision Q1, 09/11/2026): every source here is for
**internal research use only**. Each receipt carries a per-source license tag;
Common Crawl and derived records are never releasable, CC-BY-SA sources carry
ShareAlike if anything is ever released. ``CORPUS_NOT_ADMITTED`` (§19.101)
still applies to Cloud staging: this tool writes only to a local persistent
data root and never uploads anything.

Guarantees (§19.101 downloader requirements):
- the byte limit is enforced while receiving, never only from Content-Length;
- a resumed transfer requires ``206`` with a matching ``Content-Range`` and is
  guarded by the pinned validator (``If-Range``/ETag);
- the final size and SHA-256 are verified before the ``.part`` file is renamed;
- unexpected status, size, ETag or hash stops the transfer (no silent retry
  into a different object);
- transfers are throttled (token bucket) and the process lowers its own
  CPU/IO priority so a co-located pump is not disturbed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import time
import urllib.error
import urllib.request

USER_AGENT = "athena-research-fetch/1 (internal research; rate-limited)"
DEFAULT_RATE_BYTES_PER_S = 4 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

CC_TERMS = "commoncrawl-terms-of-use-2024-03-07; third-party publisher rights reserved; §9 indemnity on user"
LICENSES = {
    "commoncrawl": {"license": CC_TERMS, "release": "never", "sharealike": False},
    "wcxb": {"license": "CC-BY-4.0 (collection); page rights not verified", "release": "internal-only",
             "sharealike": False},
    "webmainbench": {"license": "Apache-2.0 (collection); page rights not verified",
                     "release": "internal-only", "sharealike": False},
    "natural-questions": {"license": "CC-BY-SA-3.0", "release": "internal-only", "sharealike": True},
}


@dataclass(frozen=True)
class Source:
    source_id: str
    url: str
    relpath: str
    family: str
    expected_bytes: int | None = None
    max_bytes: int | None = None
    etag: str | None = None
    sha256: str | None = None

    def cap(self) -> int:
        if self.expected_bytes is not None:
            return self.expected_bytes
        if self.max_bytes is None:
            raise ValueError(f"{self.source_id}: an unpinned size needs an explicit max_bytes cap")
        return self.max_bytes

    def validate(self):
        if self.family not in LICENSES:
            raise ValueError(f"{self.source_id}: unknown license family")
        if not self.url.startswith("https://"):
            raise ValueError(f"{self.source_id}: only HTTPS sources are admitted")
        parts = PurePosixPath(self.relpath).parts
        if not parts or any(part in ("", ".", "..") for part in parts) or self.relpath.startswith("/"):
            raise ValueError(f"{self.source_id}: unsafe relative destination")
        if self.sha256 is not None and not _SHA256.fullmatch(self.sha256):
            raise ValueError(f"{self.source_id}: malformed pinned SHA-256")
        self.cap()


_CC = "https://data.commoncrawl.org/"
_CC_SEGMENTS = (  # CORE_RESEARCH_PLAN.md §19.101 (HEAD sizes and ETags, 25/10/2026)
    ("cc-2026-43-00106",
     "crawl-data/CC-MAIN-2026-43/segments/1791170344035.85/warc/CC-MAIN-20261015222031-20261016012031-00106.warc.gz",
     939_895_851, '"c5645bd24a72492fc1e564f0df374433-15"'),
    ("cc-2026-43-00920",
     "crawl-data/CC-MAIN-2026-43/segments/1791170054662.4/warc/CC-MAIN-20261012021225-20261012051225-00920.warc.gz",
     903_887_184, '"bc7a42d68770f481fe970f665f9b0b30-14"'),
    ("cc-2026-43-00730",
     "crawl-data/CC-MAIN-2026-43/segments/1791170054319.12/warc/CC-MAIN-20261008152407-20261008182407-00730.warc.gz",
     949_533_711, '"d3c59fa24e670f68237593c3f0ca5692-15"'),
)
_WMB_REV = "5da0972e9b58d0c7891ae75053ced97c268f52e3"
_NQ_REV = "e8103d566bef4154c2c12b17c6095ec5275840cc"
_NQ_DEV = (  # HF LFS oids are the SHA-256 of the file
    (185_560_593, "348fb27f787e68e1c6389d0fdc3453a41fc66321ec30a2f3c451f1ef82d07c20"),
    (197_099_510, "5be65bc20b859c5ca6041043507fbc8f34224192a70e5e767dcb2876f04468a4"),
    (190_921_063, "d639409b817ceceacf3b2538fed27065ff296793c158707e9e07f3ab2f737a9d"),
    (191_104_999, "4d1df3bea63e9bb567bdf3b87e8955b947f8c6f998bded0840c01bb9a13968bc"),
    (192_525_840, "40781760176ff93afc84fb5dba1239f46a082aa2edc0e7dd4fad1a34f38435e0"),
    (190_204_549, "3988c0d16f6b014348be130ef20ce324cf2b9bbafbddee96ad78a37e52e8903f"),
    (189_709_804, "b3153de2b44df02e0f0a5eac8d82499ef175f99026e8e4dc30e55f4e3b22e725"),
)

SOURCES: dict[str, Source] = {}
for _sid, _path, _bytes, _etag in _CC_SEGMENTS:
    SOURCES[_sid] = Source(_sid, _CC + _path, "cc/" + _path, "commoncrawl", _bytes, etag=_etag)
SOURCES["wcxb-v1.0"] = Source(
    "wcxb-v1.0",
    "https://codeload.github.com/Murrough-Foley/web-content-extraction-benchmark/tar.gz/"
    "62ff86d12ea72c80c31fb810ff1a724fad687bea",
    "wcxb/wcxb-v1.0-62ff86d.tar.gz", "wcxb", max_bytes=160 * 1024 * 1024)
SOURCES["webmainbench-545"] = Source(
    "webmainbench-545",
    f"https://huggingface.co/datasets/opendatalab/WebMainBench/resolve/{_WMB_REV}/WebMainBench_545.jsonl",
    f"webmainbench/{_WMB_REV[:12]}/WebMainBench_545.jsonl", "webmainbench", 109_097_918,
    sha256="0efaa4b49a45e320a27fe6e5a0b6aad5b57259fc3321ac3448519cacc74c537e")
for _index, (_bytes, _sha) in enumerate(_NQ_DEV):
    _name = f"validation-{_index:05d}-of-00007.parquet"
    SOURCES[f"nq-dev-{_index}"] = Source(
        f"nq-dev-{_index}",
        f"https://huggingface.co/datasets/google-research-datasets/natural_questions/resolve/{_NQ_REV}/dev/{_name}",
        f"nq/{_NQ_REV[:12]}/dev/{_name}", "natural-questions", _bytes, sha256=_sha)

GROUPS = {
    "cc-first": ["cc-2026-43-00106"],
    "cc-pinned": [sid for sid, *_ in _CC_SEGMENTS],
    "tier-a": ["wcxb-v1.0", "webmainbench-545"] + [f"nq-dev-{i}" for i in range(len(_NQ_DEV))],
}


class FetchError(RuntimeError):
    """A transfer stopped on an integrity or protocol violation (operator review)."""


class TokenBucket:
    def __init__(self, rate_bytes_per_s: int, *, clock=time.monotonic, sleep=time.sleep):
        if type(rate_bytes_per_s) is not int or rate_bytes_per_s < 1:
            raise ValueError("rate must be a positive integer of bytes/s")
        self.rate = rate_bytes_per_s
        self.clock, self.sleep = clock, sleep
        self.started = clock()
        self.consumed = 0

    def consume(self, count: int):
        self.consumed += count
        ahead = self.consumed / self.rate - (self.clock() - self.started)
        if ahead > 0:
            self.sleep(ahead)


def lower_priority():
    """Best-effort: lower CPU priority and, on macOS, mark the process as background IO."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    if sys.platform == "darwin":
        try:
            import ctypes
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            # setiopolicy_np(IOPOL_TYPE_DISK=0, IOPOL_SCOPE_PROCESS=0, IOPOL_THROTTLE=3)
            libc.setiopolicy_np(0, 0, 3)
        except Exception:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request(url: str, method: str = "GET", headers: dict | None = None):
    request = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=60)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_range_start(value: str | None, expected_total: int | None) -> int:
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", (value or "").strip())
    if match is None:
        raise FetchError(f"206 response without a valid Content-Range: {value!r}")
    if expected_total is not None and match.group(3) != str(expected_total):
        raise FetchError(f"Content-Range total {match.group(3)} differs from pinned {expected_total}")
    return int(match.group(1))


def _log(message: str):
    print(message, flush=True)


def fetch(source: Source, data_root: Path, *, rate_bytes_per_s: int = DEFAULT_RATE_BYTES_PER_S,
          opener=_request, max_attempts: int = 6, log=_log, sleep=time.sleep) -> dict:
    """Download one pinned source into ``data_root`` and return its verified receipt."""
    source.validate()
    data_root = Path(data_root).expanduser().resolve()
    destination = data_root / source.relpath
    receipt_path = destination.with_name(destination.name + ".receipt.json")
    if destination.is_file() and receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("bytes") == destination.stat().st_size and receipt.get("source_id") == source.source_id:
            log(json.dumps({"event": "already_verified", "source": source.source_id}))
            return receipt
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")
    cap = source.cap()

    head = opener(source.url, "HEAD")
    with head:
        status = getattr(head, "status", 200)
        if status != 200:
            raise FetchError(f"{source.source_id}: HEAD returned {status}")
        head_length = head.headers.get("Content-Length")
        head_etag = head.headers.get("ETag")
        last_modified = head.headers.get("Last-Modified")
    if source.expected_bytes is not None and head_length is not None \
            and int(head_length) != source.expected_bytes:
        raise FetchError(f"{source.source_id}: HEAD size {head_length} != pinned {source.expected_bytes}")
    if source.etag is not None and head_etag != source.etag:
        raise FetchError(f"{source.source_id}: HEAD ETag {head_etag} != pinned {source.etag}")

    bucket = TokenBucket(rate_bytes_per_s, sleep=sleep)
    attempt = 0
    while True:
        attempt += 1
        offset = part.stat().st_size if part.is_file() else 0
        if offset > cap:
            raise FetchError(f"{source.source_id}: partial file exceeds the byte cap")
        if source.expected_bytes is not None and offset == source.expected_bytes:
            break
        headers = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = source.etag or head_etag
            if validator:
                headers["If-Range"] = validator
            else:  # cannot prove the object is unchanged; restart from zero
                offset = 0
                part.unlink(missing_ok=True)
                headers = {}
        try:
            response = opener(source.url, "GET", headers)
            with response:
                status = getattr(response, "status", 200)
                if offset and status == 200:
                    log(json.dumps({"event": "range_not_honoured_restart", "source": source.source_id}))
                    offset = 0
                    part.unlink(missing_ok=True)
                elif offset and status == 206:
                    start = _content_range_start(response.headers.get("Content-Range"), source.expected_bytes)
                    if start != offset:
                        raise FetchError(f"{source.source_id}: Content-Range start {start} != {offset}")
                elif status != 200:
                    raise FetchError(f"{source.source_id}: GET returned {status}")
                if source.etag is not None and response.headers.get("ETag") not in (None, source.etag):
                    raise FetchError(f"{source.source_id}: GET ETag changed")
                received = offset
                with part.open("ab" if offset else "wb") as stream:
                    while True:
                        block = response.read(min(CHUNK_BYTES, cap + 1 - received))
                        if not block:
                            break
                        received += len(block)
                        if received > cap:
                            raise FetchError(f"{source.source_id}: received bytes exceed the cap {cap}")
                        stream.write(block)
                        bucket.consume(len(block))
            if source.expected_bytes is None or received == source.expected_bytes:
                break
            raise ConnectionError(f"short read at {received}/{source.expected_bytes}")
        except FetchError:
            raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise FetchError(f"{source.source_id}: HTTP {error.code}") from error
            if attempt >= max_attempts:
                raise FetchError(f"{source.source_id}: gave up after {attempt} attempts: {error}") from error
            delay = min(300.0, 5.0 * 2 ** (attempt - 1))
            log(json.dumps({"event": "retry", "source": source.source_id, "attempt": attempt,
                            "delay_s": delay, "error": f"{type(error).__name__}: {error}"[:300]}))
            sleep(delay)

    size = part.stat().st_size
    if source.expected_bytes is not None and size != source.expected_bytes:
        raise FetchError(f"{source.source_id}: final size {size} != pinned {source.expected_bytes}")
    if size < 1 or size > cap:
        raise FetchError(f"{source.source_id}: final size {size} outside admission")
    sha256 = _sha256_file(part)
    if source.sha256 is not None and sha256 != source.sha256:
        part.unlink(missing_ok=True)
        raise FetchError(f"{source.source_id}: SHA-256 {sha256} != pinned {source.sha256}")
    os.replace(part, destination)
    receipt = {
        "schema": "athena-data-fetch-receipt/1", "source_id": source.source_id, "url": source.url,
        "relpath": source.relpath, "bytes": size, "sha256": sha256,
        "sha256_pinned": source.sha256 is not None, "etag": head_etag, "etag_pinned": source.etag,
        "last_modified": last_modified, "fetched_at": _utc_now(),
        "rate_limit_bytes_per_s": rate_bytes_per_s, **LICENSES[source.family],
        "corpus_status": "CORPUS_NOT_ADMITTED (local internal research only; no cloud staging)",
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log(json.dumps({"event": "verified", "source": source.source_id, "bytes": size, "sha256": sha256}))
    return receipt


_WCXB_MEMBER = re.compile(r"[^/]+/(metadata\.json|(?:dev|test)/html/[0-9]{4}\.html\.gz"
                          r"|(?:dev|test)/ground-truth/[0-9]{4}\.json|LICENSE|README\.md)\Z")


def extract_wcxb(archive: Path, destination: Path, *, max_member_bytes: int = 4 * 1024 * 1024) -> dict:
    """Stream-extract only the pinned WCXB dataset members; wceb.load_wceb verifies every blob."""
    destination = Path(destination).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    written = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            match = _WCXB_MEMBER.fullmatch(member.name)
            if match is None or not member.isfile():
                continue
            if member.size > max_member_bytes:
                raise FetchError(f"WCXB member {member.name} exceeds {max_member_bytes} bytes")
            target = destination / match.group(1)
            if not target.resolve().is_relative_to(destination):
                raise FetchError("WCXB member escapes the destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            data = source.read(max_member_bytes + 1)
            if len(data) != member.size:
                raise FetchError(f"WCXB member {member.name} size changed while reading")
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
            written += 1
    return {"members_written": written, "destination": str(destination)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    get = sub.add_parser("fetch")
    get.add_argument("names", nargs="+", help="source ids or groups: " + ", ".join(GROUPS))
    get.add_argument("--data-root", default="~/athena_data")
    get.add_argument("--rate", type=int, default=DEFAULT_RATE_BYTES_PER_S, help="bytes per second")
    sub.add_parser("list")
    wcxb = sub.add_parser("extract-wcxb")
    wcxb.add_argument("--data-root", default="~/athena_data")
    args = parser.parse_args(argv)
    if args.command == "list":
        for source in SOURCES.values():
            print(json.dumps({"id": source.source_id, "bytes": source.expected_bytes,
                              "family": source.family, **LICENSES[source.family]}))
        return 0
    data_root = Path(args.data_root).expanduser()
    if args.command == "extract-wcxb":
        archive = data_root / SOURCES["wcxb-v1.0"].relpath
        print(json.dumps(extract_wcxb(archive, data_root / "wcxb" / "v1.0")))
        return 0
    lower_priority()
    ids = []
    for name in args.names:
        ids.extend(GROUPS.get(name, [name]))
    unknown = [name for name in ids if name not in SOURCES]
    if unknown:
        parser.error(f"unknown sources: {unknown}")
    failures = 0
    for source_id in ids:
        try:
            fetch(SOURCES[source_id], data_root, rate_bytes_per_s=args.rate)
        except FetchError as error:
            failures += 1
            print(json.dumps({"event": "hold", "source": source_id, "error": str(error)}), flush=True)
    return 78 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
