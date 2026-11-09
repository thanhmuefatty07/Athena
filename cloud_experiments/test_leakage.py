import unittest

from cloud_experiments import leakage


class _TextCollector(leakage.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_text(html):
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)


class CcSelectStripTests(unittest.TestCase):
    def test_removes_every_label_form_and_preserves_text_and_other_attributes(self):
        html = ('<html><body><div class="nav">Menu</div>'
                '<div cc-select="true" id="main"><p CC-SELECT=\'true\'>Alpha &amp; beta</p>'
                '<p class="x" cc-select=true>Gamma</p><span cc-select>Delta</span>'
                '<img src="a.png" cc-select="true"/><td\ncc-select="true"\nclass="c">Eps</td></div>'
                '<script>var s = "<div cc-select=\\"true\\">";</script></body></html>')
        self.assertEqual(leakage.cc_select_attribute_count(html), 6)
        result = leakage.strip_cc_select(html)
        self.assertEqual(result.attributes_removed, 6)
        self.assertEqual(leakage.cc_select_attribute_count(result.html), 0)
        self.assertNotRegex(result.html.split("<script>")[0], r"(?i)cc-select")
        self.assertIn('<div id="main">', result.html)
        self.assertIn('<p class="x">', result.html)
        self.assertIn('class="c"', result.html)
        self.assertIn('<img src="a.png"/>', result.html)
        self.assertEqual(visible_text(result.html.split("<script>")[0]),
                         visible_text(html.split("<script>")[0]))

    def test_residual_pass_catches_tags_python_tokenizer_misses(self):
        # html5ever sees an attribute inside <textarea>-less malformed markup that
        # the stdlib tokenizer reads as text; the residual pass must still remove it.
        html = '<p>ok</p><div<div cc-select="true">x</div>'
        result = leakage.strip_cc_select(html)
        self.assertNotRegex(result.html, r"(?i)cc-select\s*=")

    def test_no_label_is_a_byte_identical_noop(self):
        html = "<html><body><p data-x='1'>Hello <b>world</b></p></body></html>"
        result = leakage.strip_cc_select(html)
        self.assertEqual(result.html, html)
        self.assertEqual(result.attributes_removed, 0)

    def test_annotation_tool_mark_selected_class_is_removed(self):
        html = ('<div class="mark-selected" cc-select="true">A</div>'
                '<p class="intro mark-selected big" cc-select="true">B</p>'
                "<li class='mark-selected'>C</li><span class=mark-selected>D</span>"
                '<em class="marked selected">E</em>')
        self.assertEqual(leakage.label_class_count(html), 4)
        result = leakage.strip_cc_select(html)
        self.assertEqual(leakage.label_class_count(result.html), 0)
        self.assertEqual(result.label_class_tokens_removed, 4)
        self.assertNotIn("mark-selected", result.html)
        self.assertIn('<div>A</div>', result.html)
        self.assertIn('<p class="intro big">B</p>', result.html)
        self.assertIn('<li>C</li><span>D</span>', result.html)
        self.assertIn('<em class="marked selected">E</em>', result.html)

    def test_rejects_non_text(self):
        with self.assertRaises(TypeError):
            leakage.strip_cc_select(b"<p cc-select>")


AR5IV = ('<p>Let <math id="m1" class="ltx_Math" alttext="E=mc^{2}" display="inline"><semantics>'
         '<mrow><mi>E</mi><mo>=</mo><mrow><mi>m</mi><msup><mi>c</mi><mn>2</mn></msup></mrow></mrow>'
         '<annotation-xml encoding="MathML-Content"><apply><eq/></apply></annotation-xml>'
         '<annotation encoding="application/x-tex">E=mc^{2}</annotation></semantics></math> hold.</p>')

WIKI_2025 = ('<span class="mwe-math-element" data-mw=\'{"name":"math","attrs":{},'
             '"body":{"extsrc":"a^2+b^2=c^2"}}\'><span class="mwe-math-mathml-inline" '
             'style="display: none;"><math xmlns="http://www.w3.org/1998/Math/MathML" '
             'alttext="{\\displaystyle a^{2}+b^{2}=c^{2}}"><semantics><mrow><msup><mi>a</mi><mn>2</mn>'
             '</msup></mrow><annotation encoding="application/x-tex">{\\displaystyle a^{2}+b^{2}=c^{2}}'
             '</annotation></semantics></math></span><img src="x.svg" class="mwe-math-fallback-image-inline"'
             ' alt="{\\displaystyle a^{2}+b^{2}=c^{2}}"></span>')

PARSOID_NOW = ('<span typeof="mw:Extension/math" data-mw=\'{"name":"math","attrs":{},'
               '"body":{"extsrc":"\\\\frac{1}{2}"}}\'><math><mfrac><mn>1</mn><mn>2</mn></mfrac></math></span>')


class MathStripTests(unittest.TestCase):
    def test_ar5iv_annotation_and_alttext_are_removed_and_kept_as_gold(self):
        result = leakage.strip_math_leakage(AR5IV)
        self.assertNotIn("annotation", result.html)
        self.assertNotIn("alttext", result.html)
        self.assertNotIn("E=mc^{2}", result.html)
        self.assertIn("<msup><mi>c</mi><mn>2</mn></msup>", result.html)
        self.assertIn(" hold.</p>", result.html)
        self.assertEqual(len(result.gold), 1)
        self.assertEqual(result.gold[0].annotation_tex, "E=mc^{2}")
        self.assertEqual(result.gold[0].alttext, "E=mc^{2}")
        self.assertEqual(result.removed["annotation"], 1)
        self.assertEqual(result.removed["annotation_xml"], 1)
        self.assertTrue(leakage.math_leak_report(result.html)["clean"])
        self.assertFalse(leakage.math_leak_report(AR5IV)["clean"])

    def test_wikipedia_2025_strips_fallback_image_alt_and_data_mw(self):
        result = leakage.strip_math_leakage(WIKI_2025)
        self.assertNotIn("a^{2}", result.html)
        self.assertNotIn("a^2+b^2", result.html)
        self.assertIn('class="mwe-math-fallback-image-inline"', result.html)
        self.assertIn('src="x.svg"', result.html)
        self.assertEqual(result.removed["math_img_alt"], 1)
        self.assertEqual(result.removed["data_mw"], 1)
        gold = result.gold[0]
        self.assertEqual(gold.data_mw_tex, "a^2+b^2=c^2")
        self.assertEqual(leakage.normalize_tex(gold.annotation_tex), leakage.normalize_tex(gold.data_mw_tex))
        self.assertTrue(leakage.math_leak_report(result.html)["clean"])

    def test_current_parsoid_math_gets_data_mw_gold(self):
        result = leakage.strip_math_leakage(PARSOID_NOW)
        self.assertNotIn("data-mw", result.html)
        self.assertEqual(result.gold[0].gold_tex, "\\frac{1}{2}")
        self.assertEqual(result.unpaired_data_mw_tex, [])

    def test_untouched_when_no_math_leak(self):
        html = '<p>Plain <img src="a.png" alt="a cat"> text</p><annotation>not math</annotation>'
        result = leakage.strip_math_leakage(html)
        self.assertEqual(result.html, html)
        self.assertEqual(result.gold, [])

    def test_empty_encoding_annotation_is_treated_as_tex_like_projection_rs(self):
        html = '<math><semantics><mi>x</mi><annotation>x^2</annotation></semantics></math>'
        result = leakage.strip_math_leakage(html)
        self.assertNotIn("x^2", result.html)
        self.assertEqual(result.gold[0].annotation_tex, "x^2")

    def test_data_latex_on_any_element_is_removed(self):
        html = '<span class="katex" data-latex="\\alpha">α</span><div data-tex="y">y</div>'
        result = leakage.strip_math_leakage(html)
        self.assertNotIn("data-latex", result.html)
        self.assertNotIn("data-tex", result.html)
        self.assertIn(">α</span>", result.html)


class TexNormalizationTests(unittest.TestCase):
    def test_wrapper_spacing_and_braces(self):
        self.assertEqual(leakage.normalize_tex("{\\displaystyle a^{2}+b^{2}=c^{2}}"),
                         leakage.normalize_tex("a^2 + b^2 = c^2"))
        self.assertEqual(leakage.normalize_tex("$\\frac{1}{2}\\,x$"), leakage.normalize_tex("\\frac12x"))
        self.assertEqual(leakage.normalize_tex("\\left( x \\right)"), "( x )")
        self.assertEqual(leakage.normalize_tex("\\dfrac{a}{b} \\le c"), leakage.normalize_tex("\\frac ab\\leq c"))
        self.assertEqual(leakage.normalize_tex("{}^{14}C"), "^ { 1 4 } C")  # empty group dropped only

    def test_does_not_conflate_different_formulas(self):
        self.assertNotEqual(leakage.normalize_tex("x^{2y}"), leakage.normalize_tex("x^2y"))
        self.assertNotEqual(leakage.normalize_tex("a-b"), leakage.normalize_tex("a+b"))

    def test_edit_distance_and_similarity(self):
        self.assertEqual(leakage.tex_edit_distance("a+b", "a+b"), 0)
        self.assertEqual(leakage.tex_edit_distance("a+b", "a-b"), 1)
        self.assertEqual(leakage.tex_edit_distance("", "abc"), 3)
        self.assertEqual(leakage.tex_similarity("", ""), 1.0)
        self.assertAlmostEqual(leakage.tex_similarity("a+b", "a-b"), 2 / 3)


if __name__ == "__main__":
    unittest.main()
