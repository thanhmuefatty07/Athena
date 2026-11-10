import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import unittest.mock

from cloud_experiments import data_fetch, leakage, tier_b


def build_wikimedia_tar_gz(path: Path, articles: list[dict]):
    """Build a synthetic Wikimedia Enterprise `.json.tar.gz` with `.ndjson` member(s)."""
    lines = [json.dumps(a, ensure_ascii=False).encode("utf-8") + b"\n" for a in articles]
    ndjson_bytes = b"".join(lines)
    with tarfile.open(path, mode="w:gz") as tar:
        # Non-ndjson member must be ignored by streaming reader
        readme = b"Wikimedia Enterprise HTML dump test"
        info_txt = tarfile.TarInfo(name="README.txt")
        info_txt.size = len(readme)
        tar.addfile(info_txt, io.BytesIO(readme))

        info = tarfile.TarInfo(name="enwikibooks_namespace_0_0.ndjson")
        info.size = len(ndjson_bytes)
        tar.addfile(info, io.BytesIO(ndjson_bytes))


SAMPLE_MATH_HTML = """<!DOCTYPE html>
<html><head><title>Calculus</title></head><body>
<section data-mw-section-id="0">
<p>The quadratic roots are given by
<math xmlns="http://www.w3.org/1998/Math/MathML" typeof="mw:Extension/math"
 data-mw='{"name":"math","attrs":{},"body":{"extsrc":"\\\\frac{-b \\\\pm \\\\sqrt{b^2 - 4ac}}{2a}"}}'>
<mrow><mfrac><mrow><mo>-</mo><mi>b</mi><mo>±</mo><msqrt><mrow><msup><mi>b</mi><mn>2</mn></msup><mo>-</mo><mn>4</mn><mi>a</mi><mi>c</mi></mrow></msqrt></mrow><mrow><mn>2</mn><mi>a</mi></mrow></mfrac></mrow>
</math>
and the Gaussian integral is
<math xmlns="http://www.w3.org/1998/Math/MathML" display="block" typeof="mw:Extension/math"
 data-mw='{"name":"math","attrs":{"display":"block"},"body":{"extsrc":"\\\\int_{-\\\\infty}^{\\\\infty} e^{-x^2} dx = \\\\sqrt{\\\\pi}"}}'>
<mrow><msubsup><mo>∫</mo><mrow><mo>-</mo><mi>∞</mi></mrow><mi>∞</mi></msubsup><msup><mi>e</mi><mrow><mo>-</mo><msup><mi>x</mi><mn>2</mn></msup></mrow></msup><mi>d</mi><mi>x</mi><mo>=</mo><msqrt><mi>π</mi></msqrt></mrow>
</math>
and matrix identity
<math xmlns="http://www.w3.org/1998/Math/MathML" typeof="mw:Extension/math"
 data-mw='{"name":"math","attrs":{},"body":{"extsrc":"\\\\begin{matrix} a &amp; b \\\\\\\\ c &amp; d \\\\end{matrix}"}}'>
<mtable><mtr><mtd><mi>a</mi></mtd><mtd><mi>b</mi></mtd></mtr><mtr><mtd><mi>c</mi></mtd><mtd><mi>d</mi></mtd></mtr></mtable>
</math>
with atomic variable
<math xmlns="http://www.w3.org/1998/Math/MathML" typeof="mw:Extension/math"
 data-mw='{"name":"math","attrs":{},"body":{"extsrc":"k"}}'>
<mrow><mi>k</mi></mrow>
</math>.</p>
</section></body></html>
"""

SAMPLE_TABLE_HTML = """<!DOCTYPE html>
<html><head><title>Planet Data</title></head><body>
<table class="infobox"><tr><th>Ignored Infobox</th><td>1</td></tr><tr><th>B</th><td>2</td></tr></table>
<table role="presentation"><tr><th>Ignored Layout</th><td>1</td></tr><tr><th>B</th><td>2</td></tr></table>
<table class="wikitable">
<caption>Inner &amp; Outer Planets</caption>
<thead>
<tr><th rowspan="2">Group</th><th rowspan="2">Planet</th><th colspan="2">Physical</th></tr>
<tr><th>Mass (Earth=1)</th><th>Moons</th></tr>
</thead>
<tbody>
<tr><th rowspan="2">Terrestrial</th><td>Earth</td><td>1.00</td><td>1</td></tr>
<tr><td>Mars</td><td>0.107</td><td>2</td></tr>
</tbody>
</table>
<table class="wikitable">
<thead><tr><th>Language</th><th>Paradigm</th></tr></thead>
<tbody>
<tr><td>Rust</td><td>Systems</td></tr>
<tr><td>Python</td><td>Multi-paradigm</td></tr>
</tbody>
</table>
</body></html>
"""


class TierBUnitTests(unittest.TestCase):
    def test_classify_math_constructs_and_tier1_enrichment(self):
        self.assertEqual(
            tier_b.classify_math_constructs(r"\frac{-b \pm \sqrt{b^2 - 4ac}}{2a}"),
            ["frac", "scripts", "sqrt", "operators_greek"],
        )
        self.assertEqual(
            tier_b.classify_math_constructs(r"\begin{pmatrix} 1 & 0 \\ 0 & 1 \end{pmatrix}"),
            ["matrix"],
        )
        self.assertEqual(
            tier_b.classify_math_constructs(r"\alpha + \beta = \gamma"),
            ["operators_greek"],
        )
        self.assertEqual(tier_b.classify_math_constructs("x + y = z"), ["atomic_or_other"])

        enriched = tier_b.enrich_math_tier1_annotations(SAMPLE_MATH_HTML)
        self.assertIn("alttext=", enriched)
        # Stripping the enriched HTML must still remove all TeX channels cleanly
        stripped = leakage.strip_math_leakage(enriched)
        self.assertEqual(len(stripped.gold), 4)
        self.assertTrue(leakage.math_leak_report(stripped.html)["clean"])

    def test_align_gold_and_predicted_tex_resists_spurious_prose_dollar_spans(self):
        golds = [
            r"\frac{a}{b}",
            r"x^{2} + y^{2}",
            r"\sqrt{\pi}",
        ]
        # Suppose prose had a spurious "$5 to $10" span before the real formulas
        preds = [
            "5 to ",
            r"\frac{a}{b}",
            r"x^{2} + y^{2}",
            r"\sqrt{\pi}",
        ]
        aligned = tier_b.align_gold_and_predicted_tex(golds, preds)
        self.assertEqual(len(aligned), 3)
        for _, _, sim, dist in aligned:
            self.assertAlmostEqual(sim, 1.0, places=6)
            self.assertEqual(dist, 0)

        # Empty predictions return 0 similarity for every gold formula
        empty_aligned = tier_b.align_gold_and_predicted_tex(golds, [])
        self.assertEqual([sim for _, _, sim, _ in empty_aligned], [0.0, 0.0, 0.0])

    def test_oracle_table_extraction_filters_chrome_and_expands_rowspan_colspan(self):
        tables = tier_b.extract_oracle_data_tables(SAMPLE_TABLE_HTML)
        # Infobox and presentation tables must be excluded; the 2 wikitables remain
        self.assertEqual(len(tables), 2)

        spanned, simple = tables[0], tables[1]
        self.assertTrue(spanned.has_span)
        self.assertEqual((spanned.height, spanned.width), (4, 4))
        self.assertEqual((spanned.max_rowspan, spanned.max_colspan), (2, 2))
        self.assertEqual(
            spanned.slots,
            [
                [0, 1, 2, 2],
                [0, 1, 3, 4],
                [5, 6, 7, 8],
                [5, 9, 10, 11],
            ],
        )
        # Check Mars (row 3, col 1) and its Mass 0.107 (row 3, col 2)
        mars_mass = spanned.cells[10]
        self.assertEqual(mars_mass.text, "0.107")
        self.assertEqual(mars_mass.col_headers, ["Physical", "Mass (Earth=1)"])
        self.assertIn("Terrestrial", mars_mass.row_headers)
        self.assertIn("[Group = Terrestrial | Physical / Mass (Earth=1)]: 0.107", mars_mass.grounded_context)

        self.assertFalse(simple.has_span)
        self.assertEqual((simple.height, simple.width), (3, 2))
        rust_paradigm = simple.cells[3]
        self.assertEqual(rust_paradigm.text, "Systems")
        self.assertEqual(rust_paradigm.col_headers, ["Paradigm"])
        self.assertEqual(rust_paradigm.row_headers, ["Rust"])
        self.assertEqual(
            rust_paradigm.grounded_context,
            "[Language = Rust | Paradigm]: Systems",
        )

    def test_streaming_tar_gz_and_end_to_end_evaluate_tier_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "sample-enterprise.json.tar.gz"
            articles = [
                {
                    "identifier": 101,
                    "name": "Calculus",
                    "url": "https://en.wikibooks.org/wiki/Calculus",
                    "in_language": {"identifier": "en"},
                    "version": {"identifier": 1001},
                    "article_body": {"html": SAMPLE_MATH_HTML},
                },
                {
                    "identifier": 102,
                    "name": "Planet_Data",
                    "url": "https://en.wikibooks.org/wiki/Planet_Data",
                    "in_language": {"identifier": "en"},
                    "version": {"identifier": 1002},
                    "article_body": {"html": SAMPLE_TABLE_HTML},
                },
                {
                    "identifier": 103,
                    "name": "Empty_Stub",
                    "url": "https://en.wikibooks.org/wiki/Empty_Stub",
                    "in_language": {"identifier": "en"},
                    "version": {"identifier": 1003},
                    "article_body": {"html": ""},
                },
            ]
            build_wikimedia_tar_gz(archive, articles)

            streamed = list(tier_b.iter_wikimedia_articles(archive, source_id="test-wm"))
            self.assertEqual(len(streamed), 2)
            self.assertEqual([a.name for a in streamed], ["Calculus", "Planet_Data"])

            def fake_probe_runner(argv, payload, timeout):
                stage = argv[1]
                req = json.loads(payload.decode("utf-8"))
                html = req["html"]
                if stage in ("html-table", "html_table"):
                    otables = tier_b.extract_oracle_data_tables(html)
                    return (
                        0,
                        json.dumps({
                            "schema": "athena-html-table-candidate/2",
                            "tables": [
                                {"grid": {"width": t.width, "slots": t.slots}}
                                for t in otables
                            ],
                        }).encode("utf-8"),
                        b"",
                    )
                chunks = []
                if "<math" in html:
                    chunks.append({
                        "chunk_id": 0,
                        "role": "paragraph",
                        "dom_path_slug": "html[0]/body[0]/p[0]",
                        "text": (
                            r"The quadratic roots are given by $\frac{- b \pm \sqrt{b^{2} - 4 a c}}{2 a}$ "
                            r"and the Gaussian integral is $$\int_{- \infty}^{\infty} e^{- x^{2}} d x = \sqrt{\pi}$$ "
                            r"and matrix identity $\begin{matrix} a & b \\ c & d \end{matrix}$ "
                            r"with atomic variable $k$."
                        ),
                    })
                for t_idx, otable in enumerate(tier_b.extract_oracle_data_tables(html)):
                    for c in otable.cells:
                        chunks.append({
                            "chunk_id": len(chunks),
                            "role": "table_header" if c.is_header else "table_cell",
                            "dom_path_slug": f"html[0]/body[0]/table[{t_idx}]/cell[{c.cell_id}]",
                            "text": c.grounded_context,
                        })
                return (
                    0,
                    json.dumps({
                        "schema": "athena-cloud-024-breakthrough/1",
                        "chunks": chunks,
                        "parse_warnings": 0,
                        "top_ranked_chunk_text": chunks[0]["text"] if chunks else "",
                        "contrastive_records": [],
                    }).encode("utf-8"),
                    b"",
                )

            report = tier_b.evaluate_tier_b(
                archive_paths=(archive,),
                core_probe_bin=root / "core_probe",
                runner=fake_probe_runner,
            )
            self.assertEqual(report["schema"], "athena-tier-b-eval/1")
            math_d2 = report["math_d2"]
            self.assertEqual(math_d2["pages_evaluated"], 1)
            self.assertEqual(math_d2["leak_verified_clean_pages"], 1)
            self.assertEqual(math_d2["total_formulas_evaluated"], 4)
            stripped_overall = math_d2["stripped_tier2_tree_walk"]["overall"]
            self.assertAlmostEqual(stripped_overall["formula_similarity"]["mean"], 1.0, places=5)
            self.assertEqual(stripped_overall["formula_hit_at_0_9"]["hits"], 4)
            self.assertEqual(stripped_overall["exact_match_rate"]["hits"], 4)
            by_c = math_d2["stripped_tier2_tree_walk"]["by_construct"]
            self.assertEqual(by_c["frac"]["n"], 1)
            self.assertEqual(by_c["scripts"]["n"], 2)
            self.assertEqual(by_c["sqrt"]["n"], 2)
            self.assertEqual(by_c["matrix"]["n"], 1)
            self.assertEqual(by_c["operators_greek"]["n"], 2)
            self.assertEqual(by_c["atomic_or_other"]["n"], 1)

            tables_d4 = report["tables_d4_d11"]
            self.assertEqual(tables_d4["pages_evaluated"], 1)
            self.assertEqual(tables_d4["total_tables_evaluated"], 2)
            self.assertEqual(tables_d4["spanned_tables_evaluated"], 1)
            self.assertEqual(tables_d4["simple_tables_evaluated"], 1)
            all_t = tables_d4["summary"]["all_data_tables"]
            self.assertAlmostEqual(all_t["table_macro_cell_recall"]["mean"], 1.0, places=5)
            self.assertAlmostEqual(all_t["table_macro_header_binding_recall"]["mean"], 1.0, places=5)
            self.assertAlmostEqual(all_t["grid_slot_fidelity"]["mean"], 1.0, places=5)

    def test_verify_wikimedia_source_checks_size_and_receipt(self):
        import dataclasses
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = "simplewikibooks-ns0-20250320"
            stub_src = dataclasses.replace(data_fetch.SOURCES[sid], expected_bytes=5)
            with unittest.mock.patch.dict(data_fetch.SOURCES, {sid: stub_src}):
                dest = root / stub_src.relpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"hello")
                receipt = dest.with_name(dest.name + ".receipt.json")
                receipt.write_text(
                    json.dumps({"source_id": sid, "bytes": 5, "sha256": "a" * 64}),
                    encoding="utf-8",
                )
                path, meta = tier_b.verify_wikimedia_source(root, sid)
                self.assertEqual(path, dest.resolve())
                self.assertEqual(meta["bytes"], 5)
                self.assertEqual(meta["sha256"], "a" * 64)

                # Tamper with receipt bytes -> ValueError
                receipt.write_text(
                    json.dumps({"source_id": sid, "bytes": 999}),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError):
                    tier_b.verify_wikimedia_source(root, sid)


if __name__ == "__main__":
    unittest.main()
