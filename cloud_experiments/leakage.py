"""Benchmark input sanitizers that remove answer leakage before Athena sees a page.

Two leaks are blocked here (dataset plan, "Các bẫy phải chặn"):

1. **WebMainBench `cc-select`.** Human labels are stored as ``cc-select``
   attributes on the main-content elements of the *input* HTML (544/545 pages).
   ``strip_cc_select`` removes every such attribute; a residual regex pass makes
   the result leak-free even where Python's tokenizer and html5ever disagree.
2. **Circular math scoring.** ``src/projection.rs::convert_mathml_element_to_latex``
   returns ``<annotation encoding=*tex*>`` text (or an annotation with no
   encoding), then ``alttext``/``data-latex``/``data-tex``, before its recursive
   MathML converter; the text walker also reads ``data-latex``/``data-tex`` on
   any element and ``alt`` on ``<img>`` whose class contains ``tex``/``math``
   (Wikipedia's ``mwe-math-fallback-image-*``). Parsoid keeps the source TeX in
   ``data-mw`` (``body.extsrc``). ``strip_math_leakage`` removes all of these
   from the input and returns them as gold answers.

``normalize_tex``/``tex_edit_distance`` give a deterministic TeX comparison
(wrapper/spacing/alias/brace normalization, token Levenshtein).

The rewrite is offset-based: only the removed attribute or element bytes
change, every other byte of the page is preserved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
import re

# Attribute tokenizer for the inside of one start tag (mirrors html.parser's tolerant rules).
_TAG_NAME = re.compile(r"<\s*([^\s/>]+)")
_ATTR = re.compile(r"""([^\s/=>][^\s/=>]*)(\s*=+\s*(?:'[^']*'|"[^"]*"|(?!['"])[^>\s]*))?""")
_RESIDUAL_CC_SELECT = re.compile(
    r"""(?i)(?<=[\s"'/])cc-select(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]*))?(?=[\s/>]|$)""")

MATH_LEAK_ATTRIBUTES = frozenset({"alttext", "data-latex", "data-tex", "data-mw"})


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for match in re.finditer("\n", text):
        offsets.append(match.end())
    return offsets


def _rewrite_start_tag(raw: str, remove) -> tuple[str, list[tuple[str, str | None]]]:
    """Return ``raw`` without attributes for which ``remove(name, value)`` is true."""
    name_match = _TAG_NAME.match(raw)
    if name_match is None:
        return raw, []
    position = name_match.end()
    end = len(raw) - 1 if raw.endswith(">") else len(raw)
    body = raw[position:end]
    out = [raw[:position]]
    removed = []
    cursor = 0
    for match in _ATTR.finditer(body):
        attr_name = match.group(1)
        raw_value = match.group(2)
        value = None
        if raw_value is not None:
            value = raw_value.split("=", 1)[1].strip().lstrip("=").strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
        if remove(attr_name.lower(), value):
            # drop the attribute and the whitespace that introduced it
            gap = body[cursor:match.start()]
            out.append(gap.rstrip(" \t\r\n\f") if gap.strip(" \t\r\n\f") else "")
            removed.append((attr_name.lower(), value))
        else:
            out.append(body[cursor:match.end()])
        cursor = match.end()
    out.append(body[cursor:])
    out.append(raw[end:])
    rewritten = "".join(out)
    if removed and not rewritten.startswith(raw[:position]):
        raise AssertionError("tag rewrite changed the tag name")
    return rewritten, removed


class _Locator(HTMLParser):
    """Collects start-tag spans and element spans with absolute character offsets."""

    def __init__(self, text: str, *, element_names=()):
        super().__init__(convert_charrefs=False)
        self.text = text
        self.lines = _line_offsets(text)
        self.element_names = frozenset(element_names)
        self.start_tags = []  # (offset, raw, tag, attrs)
        self.open_elements = []  # stack of (tag, start_offset, data_parts)
        self.elements = []  # (tag, start, end, inner_text, attrs, math_depth)
        self.math_depth = 0

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        offset = self._offset()
        raw = self.get_starttag_text() or ""
        self.start_tags.append((offset, raw, tag, attrs, self.math_depth))
        if tag == "math":
            self.math_depth += 1
        if tag in self.element_names:
            self.open_elements.append((tag, offset, [], dict(attrs), self.math_depth))

    def handle_startendtag(self, tag, attrs):
        offset = self._offset()
        raw = self.get_starttag_text() or ""
        self.start_tags.append((offset, raw, tag, attrs, self.math_depth))

    def handle_endtag(self, tag):
        offset = self._offset()
        close = self.text.find(">", offset)
        end = len(self.text) if close < 0 else close + 1
        if tag in self.element_names:
            for index in range(len(self.open_elements) - 1, -1, -1):
                if self.open_elements[index][0] == tag:
                    name, start, parts, attrs, depth = self.open_elements.pop(index)
                    del self.open_elements[index:]
                    self.elements.append((name, start, end, "".join(parts), attrs, depth))
                    break
        if tag == "math" and self.math_depth:
            self.math_depth -= 1

    def handle_data(self, data):
        for _, _, parts, _, _ in self.open_elements:
            parts.append(data)

    def handle_entityref(self, name):
        from html import unescape
        self.handle_data(unescape(f"&{name};"))

    def handle_charref(self, name):
        from html import unescape
        self.handle_data(unescape(f"&#{name};"))


def _apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    edits = sorted(edits)
    out, cursor = [], 0
    for start, end, replacement in edits:
        if start < cursor:  # nested inside an already removed element
            continue
        out.append(text[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


@dataclass
class CcSelectResult:
    html: str
    attributes_removed: int
    tags_touched: int
    residual_regex_removed: int
    label_class_tokens_removed: int = 0


_CLASS_ATTR = re.compile(r"""(?is)(\sclass\s*=\s*)("[^"]*"|'[^']*'|[^\s>"']+)""")
LABEL_CLASS_TOKENS = frozenset({"mark-selected"})


def _drop_label_classes(raw: str) -> tuple[str, int]:
    """Remove annotation-tool class tokens; drop the class attribute if it becomes empty."""
    removed = 0

    def replace(match):
        nonlocal removed
        quoted = match.group(2)
        quote = quoted[0] if quoted[0] in "'\"" else ""
        value = quoted[1:-1] if quote else quoted
        tokens = value.split()
        kept = [token for token in tokens if token.lower() not in LABEL_CLASS_TOKENS]
        if len(kept) == len(tokens):
            return match.group(0)
        removed += len(tokens) - len(kept)
        if not kept:
            return ""
        return f"{match.group(1)}{quote or chr(34)}{' '.join(kept)}{quote or chr(34)}"

    return _CLASS_ATTR.sub(replace, raw), removed


def _label_class_count(start_tags) -> int:
    return sum(1 for _, _, _, attrs, _ in start_tags for name, value in attrs
               if name == "class" and value and any(token.lower() in LABEL_CLASS_TOKENS
                                                    for token in value.split()))


def strip_cc_select(html: str) -> CcSelectResult:
    """Remove WebMainBench label channels: every ``cc-select`` attribute (any case/value)
    and the annotation tool's ``mark-selected`` class token (present on exactly the
    labelled tags in WebMainBench_545)."""
    if not isinstance(html, str):
        raise TypeError("HTML must be text")
    locator = _Locator(html)
    locator.feed(html)
    locator.close()
    edits, removed_total, touched, class_tokens = [], 0, 0, 0
    for offset, raw, _tag, _attrs, _depth in locator.start_tags:
        lowered = raw.lower()
        if ("cc-select" not in lowered and "mark-selected" not in lowered) \
                or html[offset:offset + len(raw)] != raw:
            continue
        rewritten, removed = _rewrite_start_tag(raw, lambda name, _value: name == "cc-select")
        rewritten, dropped = _drop_label_classes(rewritten)
        if removed or dropped:
            edits.append((offset, offset + len(raw), rewritten))
            removed_total += len(removed)
            class_tokens += dropped
            touched += 1
    cleaned = _apply_edits(html, edits)
    residual = len(_RESIDUAL_CC_SELECT.findall(cleaned))
    if residual:
        cleaned = _RESIDUAL_CC_SELECT.sub("", cleaned)
    if _RESIDUAL_CC_SELECT.search(cleaned):
        raise AssertionError("cc-select attribute survived stripping")
    audit = _Locator(cleaned)
    audit.feed(cleaned)
    audit.close()
    if _label_class_count(audit.start_tags):
        raise AssertionError("annotation-tool label class survived stripping")
    return CcSelectResult(cleaned, removed_total, touched, residual, class_tokens)


def cc_select_attribute_count(html: str) -> int:
    """Independent count of ``cc-select`` attributes as seen by Python's HTML tokenizer."""
    locator = _Locator(html)
    locator.feed(html)
    locator.close()
    return sum(1 for _, _, _, attrs, _ in locator.start_tags for name, _ in attrs if name == "cc-select")


def label_class_count(html: str) -> int:
    """Independent count of start tags carrying a ``mark-selected`` class token."""
    locator = _Locator(html)
    locator.feed(html)
    locator.close()
    return _label_class_count(locator.start_tags)


@dataclass
class MathGold:
    index: int
    offset: int
    display: str | None
    annotation_tex: str | None = None
    alttext: str | None = None
    data_mw_tex: str | None = None

    @property
    def gold_tex(self) -> str | None:
        return self.annotation_tex or self.alttext or self.data_mw_tex


@dataclass
class MathStripResult:
    html: str
    gold: list[MathGold]
    removed: dict = field(default_factory=dict)
    unpaired_data_mw_tex: list[str] = field(default_factory=list)
    math_tex_scripts: int = 0


def _data_mw_tex(value: str | None) -> str | None:
    if not value:
        return None
    try:
        payload = json.loads(value)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    body = payload.get("body")
    if name in ("math", "chem", "ce") and isinstance(body, dict) and isinstance(body.get("extsrc"), str):
        return body["extsrc"]
    return None


def _is_math_image(attrs: dict) -> bool:
    classes = (attrs.get("class") or "").lower()
    return "tex" in classes or "math" in classes


def strip_math_leakage(html: str) -> MathStripResult:
    """Remove TeX-bearing annotations/attributes from the input and return them as gold."""
    if not isinstance(html, str):
        raise TypeError("HTML must be text")
    locator = _Locator(html, element_names=("annotation", "annotation-xml", "script"))
    locator.feed(html)
    locator.close()
    counts = {"annotation": 0, "annotation_xml": 0, "alttext": 0, "data_latex": 0, "data_tex": 0,
              "data_mw": 0, "math_img_alt": 0}
    edits = []
    gold: list[MathGold] = []
    data_mw_queue: list[tuple[int, str]] = []
    for offset, raw, tag, attrs, _depth in locator.start_tags:
        attr_map = {name: value for name, value in attrs}
        if tag == "math":
            gold.append(MathGold(index=len(gold), offset=offset, display=attr_map.get("display"),
                                 alttext=attr_map.get("alttext") or None))
        extsrc = _data_mw_tex(attr_map.get("data-mw"))
        if extsrc is not None:
            data_mw_queue.append((offset, extsrc))
        image = tag == "img" and _is_math_image(attr_map)

        def remove(name, _value, image=image):
            return name in MATH_LEAK_ATTRIBUTES or (image and name == "alt")

        if html[offset:offset + len(raw)] != raw:
            raise AssertionError("start-tag offset mapping failed")
        rewritten, removed = _rewrite_start_tag(raw, remove)
        if removed:
            edits.append((offset, offset + len(raw), rewritten))
            for name, _ in removed:
                key = "math_img_alt" if name == "alt" else name.replace("-", "_")
                counts[key] += 1
    for name, start, end, inner, attrs, depth in locator.elements:
        if name == "script":
            if (attrs.get("type") or "").lower().startswith("math/tex"):
                pass  # counted below; script bodies are not rendered by Athena
            continue
        if depth < 1:
            continue  # annotation outside <math> is not a MathML annotation
        edits.append((start, end, ""))
        if name == "annotation-xml":
            counts["annotation_xml"] += 1
            continue
        counts["annotation"] += 1
        encoding = (attrs.get("encoding") or "").lower()
        if "tex" in encoding or not encoding:
            owner = None
            for item in gold:
                if item.offset <= start:
                    owner = item
            if owner is not None and owner.annotation_tex is None and inner.strip():
                owner.annotation_tex = inner.strip()
    scripts = sum(1 for name, _, _, _, attrs, _ in locator.elements
                  if name == "script" and (attrs.get("type") or "").lower().startswith("math/tex"))
    # Pair Parsoid data-mw TeX with the first following <math> lacking another gold source.
    unpaired = []
    math_positions = sorted(gold, key=lambda item: item.offset)
    for offset, extsrc in data_mw_queue:
        target = next((item for item in math_positions
                       if item.offset >= offset and item.data_mw_tex is None), None)
        if target is None:
            unpaired.append(extsrc)
        else:
            target.data_mw_tex = extsrc
    cleaned = _apply_edits(html, edits)
    return MathStripResult(cleaned, gold, counts, unpaired, scripts)


def math_leak_report(html: str) -> dict:
    """Independent post-strip audit: any surviving TeX channel Athena could copy."""
    locator = _Locator(html, element_names=("annotation", "annotation-xml"))
    locator.feed(html)
    locator.close()
    report = {"annotation_in_math": 0, "leak_attributes": 0, "math_img_alt": 0}
    for _, _, tag, attrs, _ in locator.start_tags:
        attr_map = dict(attrs)
        report["leak_attributes"] += sum(1 for name in attr_map if name in MATH_LEAK_ATTRIBUTES)
        if tag == "img" and _is_math_image(attr_map) and "alt" in attr_map:
            report["math_img_alt"] += 1
    report["annotation_in_math"] = sum(1 for _, _, _, _, _, depth in locator.elements if depth >= 1)
    report["clean"] = not any(report.values())
    return report


# ----------------------------------------------------------------------------- TeX normalization

_TEX_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|\s+|.", re.DOTALL)
_DROP = frozenset({
    r"\,", r"\;", r"\:", r"\!", r"\ ", r"\quad", r"\qquad", r"\thinspace", r"\medspace",
    r"\thickspace", r"\negthinspace", r"\enspace", r"\displaystyle", r"\textstyle",
    r"\scriptstyle", r"\scriptscriptstyle", r"\limits", r"\nolimits", r"\left", r"\right",
    r"\big", r"\Big", r"\bigg", r"\Bigg", r"\bigl", r"\bigr", r"\Bigl", r"\Bigr", r"\biggl",
    r"\biggr", r"\Biggl", r"\Biggr", r"\bigm", r"\Bigm", r"\middle",
})
_ALIASES = {
    r"\dfrac": r"\frac", r"\tfrac": r"\frac", r"\le": r"\leq", r"\ge": r"\geq", r"\ne": r"\neq",
    r"\to": r"\rightarrow", r"\gets": r"\leftarrow", r"\lbrace": r"\{", r"\rbrace": r"\}",
    r"\lvert": "|", r"\rvert": "|", r"\vert": "|", r"\lVert": r"\|", r"\rVert": r"\|",
    r"\Vert": r"\|", r"\land": r"\wedge", r"\lor": r"\vee", r"\lnot": r"\neg",
    r"\lbrack": "[", r"\rbrack": "]", r"\langle": r"\langle", r"\mid": "|", r"\colon": ":",
}
_WRAPPERS = (r"\displaystyle", r"\textstyle", r"\scriptstyle")


def _strip_delimiters(tex: str) -> str:
    tex = tex.strip()
    for left, right in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if len(tex) >= len(left) + len(right) and tex.startswith(left) and tex.endswith(right):
            return tex[len(left):len(tex) - len(right)].strip()
    return tex


def _unwrap_style_group(tex: str) -> str:
    """``{\\displaystyle X}`` (one balanced group spanning the whole string) -> ``X``."""
    while tex.startswith("{") and tex.endswith("}"):
        depth = 0
        for index, char in enumerate(tex):
            if char == "\\":
                continue
            if char == "{" and (index == 0 or tex[index - 1] != "\\"):
                depth += 1
            elif char == "}" and tex[index - 1] != "\\":
                depth -= 1
                if depth == 0 and index != len(tex) - 1:
                    return tex
        inner = tex[1:-1].strip()
        if not any(inner.startswith(wrapper) for wrapper in _WRAPPERS):
            return tex
        tex = inner
    return tex


def tex_tokens(tex: str) -> list[str]:
    """Deterministic normalized token sequence for comparing TeX strings."""
    if not isinstance(tex, str):
        raise TypeError("TeX must be text")
    text = _unwrap_style_group(_strip_delimiters(tex))
    tokens = []
    for token in _TEX_TOKEN.findall(text):
        if token.isspace() or token in _DROP or token == "~":
            continue
        tokens.append(_ALIASES.get(token, token))
    # Collapse brace groups that contain zero or one token: {}^{14} -> ^14, x^{2} -> x^2.
    changed = True
    while changed:
        changed = False
        out = []
        index = 0
        while index < len(tokens):
            if tokens[index] == "{" and index + 1 < len(tokens) and tokens[index + 1] == "}":
                index += 2
                changed = True
                continue
            if (tokens[index] == "{" and index + 2 < len(tokens) and tokens[index + 2] == "}"
                    and tokens[index + 1] not in ("{", "}")):
                out.append(tokens[index + 1])
                index += 3
                changed = True
                continue
            out.append(tokens[index])
            index += 1
        tokens = out
    return tokens


def normalize_tex(tex: str) -> str:
    return " ".join(tex_tokens(tex))


def tex_edit_distance(predicted: str, reference: str) -> int:
    """Levenshtein distance over normalized TeX tokens."""
    a, b = tex_tokens(predicted), tex_tokens(reference)
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, 1):
        current = [i]
        for j, token_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (token_a != token_b)))
        previous = current
    return previous[-1]


def tex_similarity(predicted: str, reference: str) -> float:
    """``1 - LED / max(len)`` over normalized tokens; 1.0 when both are empty."""
    length = max(len(tex_tokens(predicted)), len(tex_tokens(reference)))
    if length == 0:
        return 1.0
    return 1.0 - tex_edit_distance(predicted, reference) / length
