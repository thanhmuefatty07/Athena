import gzip
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_experiments import tier_a

FAKE_CORE = textwrap.dedent(r'''
    import hashlib, json, re, sys
    fixture = json.loads(sys.stdin.buffer.read())
    texts = [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<p[^>]*>(.*?)</p>", fixture["html"], re.S)]
    query = set(re.findall(r"\w+", fixture.get("query", "").lower()))
    k = fixture.get("highlight_limit", 5)
    scored = sorted(range(len(texts)), key=lambda i: -len(query & set(re.findall(r"\w+", texts[i].lower()))))
    top = [i for i in scored if query & set(re.findall(r"\w+", texts[i].lower()))][:k] if query else []
    order = top + [i for i in range(len(texts)) if i not in top]
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()
    out = [{"type": "meta", "version": "aae/1", "source_url": fixture["url"], "fetched_at_unix_ms": 0,
            "content_hash": h("".join(texts)), "total_blocks": len(texts), "parse_warnings": 0}]
    for i in order:
        out.append({"type": "block", "id": h(str(i) + texts[i]), "role": "paragraph", "text": texts[i],
                    "links": [], "highlight": i in top})
    out.append({"type": "end", "block_count": len(texts)})
    sys.stdout.write("".join(json.dumps(f) + "\n" for f in out))
''')


class StatisticsTests(unittest.TestCase):
    def test_bootstrap_is_deterministic_and_brackets_mean(self):
        values = [0.1, 0.5, 0.9, 0.4, 0.6, 0.7, 0.2]
        first = tier_a.bootstrap_ci(values)
        self.assertEqual(first, tier_a.bootstrap_ci(values))
        self.assertLessEqual(first[0], tier_a.mean(values))
        self.assertGreaterEqual(first[1], tier_a.mean(values))
        self.assertIsNone(tier_a.bootstrap_ci([0.5]))

    def test_cluster_bootstrap_resamples_whole_clusters(self):
        # one cluster dominates the page count; a clustered CI must stay wide
        values = [1.0] * 50 + [0.0, 0.0]
        clusters = ["a"] * 50 + ["b", "c"]
        clustered = tier_a.bootstrap_ci(values, clusters)
        page = tier_a.bootstrap_ci(values)
        self.assertGreater(clustered[1] - clustered[0], page[1] - page[0])

    def test_wilson(self):
        low, high = tier_a.wilson(50, 100)
        self.assertAlmostEqual(low, 0.4038, places=3)
        self.assertAlmostEqual(high, 0.5962, places=3)
        self.assertIsNone(tier_a.wilson(0, 0))

    def test_summarize_groups_list_valued_types_and_skips_none(self):
        rows = [{"f1": 1.0, "types": ["table", "table:data"], "domain": "a"},
                {"f1": 0.0, "types": ["plain"], "domain": "b"},
                {"f1": None, "types": ["plain"], "domain": "c"}]
        summary = tier_a.summarize(rows, "f1", cluster_key="domain", by="types")
        self.assertEqual(summary["all"]["n"], 2)
        self.assertEqual(summary["by_types"]["table"]["n"], 1)
        self.assertEqual(summary["by_types"]["plain"]["n"], 1)


class ScoringTests(unittest.TestCase):
    def test_markdown_render_and_strip(self):
        md = tier_a.render_markdown([{"role": "heading", "text": "Title"},
                                     {"role": "list_item", "text": "one"},
                                     {"role": "preformatted", "text": "x = 1"},
                                     {"role": "paragraph", "text": " "}])
        self.assertEqual(md, "## Title\n\n- one\n\n```\nx = 1\n```")
        self.assertEqual(tier_a.markdown_text(md).split(), ["Title", "one", "x", "=", "1"])

    def test_formula_and_code_scores(self):
        mean, hit, n = tier_a.formula_scores("so $a^{2}+b^2$ holds", "Here $a^2 + b^2$ and $$c$$")
        self.assertEqual(n, 2)
        self.assertEqual(hit, 0.5)
        self.assertIsNone(tier_a.formula_scores("x", "no formulas")[0])
        self.assertEqual(tier_a.code_score("```\nprint(1)\n```", "```py\nprint(1)\n```"), 1.0)
        self.assertIsNone(tier_a.code_score("", "plain"))

    def test_nq_span_text_and_match_rule(self):
        html = "<p>The <b>Eiffel</b> tower is in Paris &amp; France.</p>".encode()
        gold = tier_a.span_text(html, 0, len(html))
        self.assertEqual(gold, "The Eiffel tower is in Paris & France.")
        self.assertTrue(tier_a.block_matches_gold("The Eiffel tower is in Paris", [gold]))
        self.assertFalse(tier_a.block_matches_gold("Berlin is in Germany", [gold]))
        long_gold = " ".join(f"w{i}" for i in range(100))
        part = " ".join(f"w{i}" for i in range(10))
        self.assertTrue(tier_a.block_matches_gold(part, [long_gold]))  # split chunk of the answer
        self.assertFalse(tier_a.block_matches_gold("w1 w2", [long_gold]))

    def test_isolation_argv(self):
        argv = tier_a.core_argv("/bin/core_probe")
        self.assertEqual(argv[-4:], ["/bin/core_probe", "pipeline", "-", "0"])
        if sys.platform == "darwin":
            self.assertEqual(argv[0], "/usr/bin/sandbox-exec")


class EndToEndFakeCoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.fake = self.root / "fake_core.py"
        self.fake.write_text(FAKE_CORE, encoding="utf-8")
        self.patch = patch.object(tier_a, "core_argv", lambda binary: [sys.executable, str(self.fake)])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_webmainbench_strips_labels_and_scores(self):
        record = {"track_id": "t1", "url": "https://example.org/a",
                  "html": '<div cc-select="true" class="mark-selected"><p cc-select="true">alpha beta</p></div>'
                          '<p>nav menu</p>',
                  "groundtruth_content": "alpha beta", "meta": {"language": "en", "equation": [],
                                                                "table": [], "code": [], "level": "easy"}}
        path = self.root / "wmb.jsonl"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        report = tier_a.run_webmainbench(str(path), "unused", self.root, policies=["full_dom"], workers=2)
        result = report["policies"]["full_dom"]
        self.assertEqual(result["labels_removed_total"], 3)
        self.assertEqual(result["labels_after_strip_total"], 0)
        self.assertEqual(result["states"], {"OK": 1})
        self.assertAlmostEqual(result["recall"]["all"]["mean"], 1.0)
        self.assertAlmostEqual(result["precision"]["all"]["mean"], 0.5)

    def test_nq_hit_rank_and_unanswerable(self):
        html = "<p>Paris is the capital of France.</p><p>Bananas are yellow fruit.</p>"
        start = html.index("<p>Bananas")
        end = len(html)
        rows = [{"id": "1", "title": "t", "url": "https://en.wikipedia.org/wiki/X",
                 "question": "what colour are bananas", "html": html, "long_answer_spans": [[start, end]]},
                {"id": "2", "title": "t", "url": "https://en.wikipedia.org/wiki/Y",
                 "question": "anything", "html": html, "long_answer_spans": []}]
        path = self.root / "nq.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        report = tier_a.run_nq(str(path), "unused", self.root, policies=["full_dom"], workers=1)
        result = report["policies"]["full_dom"]
        self.assertEqual(result["answerable"], 1)
        self.assertEqual(result["no_long_answer"], 1)
        self.assertEqual(result["hit_at_1"]["value"], 1.0)
        self.assertEqual(result["answer_extracted_any_block"]["value"], 1.0)


if __name__ == "__main__":
    unittest.main()
