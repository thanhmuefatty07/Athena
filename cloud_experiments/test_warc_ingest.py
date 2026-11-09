import gzip
import hashlib
import io
import tempfile
import unittest
import zlib
from pathlib import Path

from cloud_experiments import warc_ingest


def http_response(body: bytes, *, status=200, content_type="text/html; charset=utf-8", extra=b""):
    head = (f"HTTP/1.1 {status} X\r\nContent-Type: {content_type}\r\n".encode() + extra
            + f"Content-Length: {len(body)}\r\n\r\n".encode())
    return head + body


def warc_member(block: bytes, *, warc_type="response", uri="https://example.org/a", extra=b"") -> bytes:
    headers = (b"WARC/1.0\r\nWARC-Type: " + warc_type.encode() + b"\r\n"
               b"WARC-Record-ID: <urn:uuid:" + hashlib.sha1(block + uri.encode()).hexdigest().encode() + b">\r\n"
               b"WARC-Date: 2026-10-15T22:20:31Z\r\nWARC-Target-URI: " + uri.encode() + b"\r\n" + extra
               + (b"Content-Type: application/http; msgtype=response\r\n" if warc_type == "response" else b"")
               + f"Content-Length: {len(block)}\r\n\r\n".encode())
    return gzip.compress(headers + block + b"\r\n\r\n", mtime=0)


UTF8_PAGE = "<html><head><title>Café &amp; crème</title></head><body><p>Héllo wörld</p></body></html>"


def build_warc(path: Path):
    members = [
        warc_member(b"software: test\r\n", warc_type="warcinfo", uri=""),
        warc_member(b"GET / HTTP/1.1\r\n\r\n", warc_type="request"),
        warc_member(http_response(UTF8_PAGE.encode())),
        warc_member(http_response('<meta charset="windows-1252"><p>na\xefve caf\xe9</p>'.encode("cp1252"),
                                  content_type="text/html"), uri="https://example.org/legacy"),
        warc_member(http_response("<p>mislabelled ñ</p>".encode("utf-8"),
                                  content_type="text/html; charset=ISO-8859-1"), uri="https://example.org/mis"),
        warc_member(http_response(b"gone", status=404), uri="https://example.org/404"),
        warc_member(http_response(b"\x89PNG", content_type="image/png"), uri="https://example.org/i.png"),
        warc_member(http_response(b"<p>" + b"x" * 5000 + b"</p>"), uri="https://example.org/big",
                    extra=b"WARC-Truncated: length\r\n"),
        warc_member(http_response(gzip.compress(b"<p>zipped</p>", mtime=0),
                                  extra=b"Content-Encoding: gzip\r\n"), uri="https://example.org/gz"),
        warc_member(http_response(b"<p>" + "\u00e9".encode() * 10 + b"</p>"), uri="https://example.org/nobom"),
    ]
    data = b"".join(members)
    path.write_bytes(data)
    offsets, position = [], 0
    for member in members:
        offsets.append(position)
        position += len(member)
    return data, offsets


class GzipMemberTests(unittest.TestCase):
    def test_offsets_lengths_and_random_access(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "t.warc.gz"
            data, offsets = build_warc(path)
            with path.open("rb") as stream:
                members = list(warc_ingest.iter_gzip_members(stream))
            self.assertEqual([m.offset for m in members], offsets)
            self.assertEqual(sum(m.length for m in members), len(data))
            for member in members:
                again = warc_ingest.read_member_at(path, member.offset)
                self.assertEqual(again.data, member.data)
                self.assertEqual(again.length, member.length)

    def test_small_read_chunks_cross_member_boundaries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "t.warc.gz"
            data, offsets = build_warc(path)
            original = warc_ingest.READ_CHUNK
            try:
                warc_ingest.READ_CHUNK = 7
                with path.open("rb") as stream:
                    members = list(warc_ingest.iter_gzip_members(stream))
            finally:
                warc_ingest.READ_CHUNK = original
            self.assertEqual([m.offset for m in members], offsets)

    def test_truncated_member_is_an_error_not_silent_eof(self):
        stream = io.BytesIO(gzip.compress(b"WARC/1.0\r\n" * 100)[:-10])
        with self.assertRaises(warc_ingest.WarcFormatError):
            list(warc_ingest.iter_gzip_members(stream))

    def test_member_over_cap_is_flagged(self):
        stream = io.BytesIO(gzip.compress(b"a" * 5000))
        member = next(warc_ingest.iter_gzip_members(stream, max_member_bytes=100))
        self.assertTrue(member.oversize)
        self.assertEqual(len(member.data), 100)
        self.assertEqual(member.decompressed_bytes, 5000)

    def test_whole_file_digest_during_scan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "t.warc.gz"
            data, _ = build_warc(path)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                list(warc_ingest.iter_gzip_members(stream, sha256=digest))
            self.assertEqual(digest.hexdigest(), hashlib.sha256(data).hexdigest())


class ClassificationTests(unittest.TestCase):
    def pages(self, **kwargs):
        root = tempfile.mkdtemp()
        path = Path(root) / "t.warc.gz"
        build_warc(path)
        return {page.target_uri: page for _, page in warc_ingest.iter_pages(path, **kwargs)}

    def test_statuses_and_transcoding(self):
        pages = self.pages()
        self.assertEqual(len(pages), 8)  # warcinfo + request skipped
        ok = pages["https://example.org/a"]
        self.assertEqual(ok.status, "OK")
        self.assertEqual(ok.transcoded.codec, "utf-8")
        self.assertEqual(ok.transcoded.source, "http")
        legacy = pages["https://example.org/legacy"]
        self.assertEqual(legacy.transcoded.codec, "cp1252")
        self.assertEqual(legacy.transcoded.source, "meta")
        self.assertIn("naïve café", legacy.transcoded.text)
        self.assertEqual(legacy.transcoded.decode_errors, 0)
        mislabelled = pages["https://example.org/mis"]
        self.assertEqual(mislabelled.transcoded.decision, "utf8_override_of_declared")
        self.assertIn("ñ", mislabelled.transcoded.text)
        self.assertEqual(pages["https://example.org/404"].status, "NON_200")
        self.assertEqual(pages["https://example.org/i.png"].status, "NON_HTML")
        truncated = pages["https://example.org/big"]
        self.assertEqual(truncated.truncated, "length")
        self.assertEqual(truncated.status, "OK")  # processed, but flagged
        zipped = pages["https://example.org/gz"]
        self.assertEqual(zipped.transcoded.text, "<p>zipped</p>")
        self.assertEqual(zipped.content_note, "gzip")
        sniffed = pages["https://example.org/nobom"]
        self.assertEqual(sniffed.transcoded.codec, "utf-8")

    def test_oversize_after_transcoding_is_reported(self):
        pages = self.pages(max_html_bytes=1000)
        self.assertEqual(pages["https://example.org/big"].status, "OVERSIZE")
        self.assertEqual(pages["https://example.org/a"].status, "OK")

    def test_whatwg_aliases_and_fallback(self):
        self.assertEqual(warc_ingest.whatwg_codec("ISO-8859-1"), "cp1252")
        self.assertEqual(warc_ingest.whatwg_codec("us-ascii"), "cp1252")
        self.assertEqual(warc_ingest.whatwg_codec("Shift_JIS"), "cp932")
        self.assertEqual(warc_ingest.whatwg_codec("gb2312"), "gbk")
        self.assertEqual(warc_ingest.whatwg_codec("windows-1258"), "cp1258")
        self.assertIsNone(warc_ingest.whatwg_codec("x-unknown"))
        result = warc_ingest.transcode_to_utf8(b"<p>\x81\xe9</p>", None)
        self.assertEqual(result.codec, "cp1252")
        self.assertEqual(result.decision, "fallback_windows_1252")
        self.assertEqual(result.decode_errors, 1)  # 0x81 is unmapped in cp1252
        bom = warc_ingest.transcode_to_utf8(b"\xef\xbb\xbf<p>x</p>", "text/html; charset=iso-8859-1")
        self.assertEqual((bom.codec, bom.source, bom.text), ("utf-8", "bom", "<p>x</p>"))

    def test_title_extraction(self):
        self.assertEqual(warc_ingest.page_title(UTF8_PAGE), "Café & crème")
        self.assertEqual(warc_ingest.page_title("<p>none</p>"), "")


class MeasureTests(unittest.TestCase):
    def test_measure_report(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "t.warc.gz"
            data, _ = build_warc(path)
            report = warc_ingest.measure(path, expected_bytes=len(data))
            self.assertTrue(report["size_matches_pin"])
            self.assertEqual(report["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(report["warc_records"], 10)
            self.assertEqual(report["record_types"], {"warcinfo": 1, "request": 1, "response": 8})
            self.assertEqual(report["html_200_pages"], 6)
            self.assertEqual(report["charset"]["final_codec"], {"utf-8": 5, "cp1252": 1})
            self.assertEqual(report["warc_truncated"], {"length": 1, "html_200:length": 1})


if __name__ == "__main__":
    unittest.main()
