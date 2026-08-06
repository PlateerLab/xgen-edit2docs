"""Upstream source_to_md ports — DOCX pipe tables, mammoth escapes,
OMML→LaTeX, web charset decoding (upstream 1b22f3f3, 4294adcf, 54ef7c73)."""

from __future__ import annotations

from xml.etree import ElementTree as ET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def _w_tbl(rows: list[list[str]]) -> ET.Element:
    body = "".join(
        "<w:tr>"
        + "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        + "</w:tr>"
        for row in rows
    )
    return ET.fromstring(f'<w:tbl xmlns:w="{W_NS}">{body}</w:tbl>')


class TestDocxPipeTables:
    def test_table_becomes_pipe_markdown(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _docx_table_to_markdown

        md = _docx_table_to_markdown(
            _w_tbl([["Name", "Qty"], ["apple", "3"], ["pear", "7"]])
        )
        assert md.splitlines() == [
            "| Name | Qty |",
            "| --- | --- |",
            "| apple | 3 |",
            "| pear | 7 |",
        ]

    def test_pipe_in_cell_is_escaped(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _docx_table_to_markdown

        md = _docx_table_to_markdown(_w_tbl([["a | b"], ["c"]]))
        assert "a \\| b" in md.splitlines()[0]

    def test_ragged_rows_are_padded(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _docx_table_to_markdown

        md = _docx_table_to_markdown(_w_tbl([["h1", "h2"], ["only-one"]]))
        assert md.splitlines()[-1] == "| only-one |  |"


class TestMammothEscapeTrim:
    def test_safe_punctuation_unescaped(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _clean_mammoth_markdown

        cleaned = _clean_mammoth_markdown(
            r"Version 2\.0 \(beta\), see notes\: done\."
        )
        assert cleaned == "Version 2.0 (beta), see notes: done."

    def test_ordered_list_dot_stays_escaped(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _clean_mammoth_markdown

        # "1\." at line start would otherwise turn into an ordered list item.
        assert _clean_mammoth_markdown("1\\. First") == "1\\. First"


def _omml(inner: str) -> ET.Element:
    return ET.fromstring(f'<m:oMath xmlns:m="{M_NS}">{inner}</m:oMath>')


class TestOmmlToLatex:
    def test_fraction(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _omml_to_latex

        elem = _omml(
            "<m:f>"
            "<m:num><m:r><m:t>a</m:t></m:r></m:num>"
            "<m:den><m:r><m:t>b</m:t></m:r></m:den>"
            "</m:f>"
        )
        assert _omml_to_latex(elem) == r"\frac{a}{b}"

    def test_superscript(self):
        from xgen_edit2docs.core.source_to_md.doc_to_md import _omml_to_latex

        elem = _omml(
            "<m:sSup>"
            "<m:e><m:r><m:t>x</m:t></m:r></m:e>"
            "<m:sup><m:r><m:t>2</m:t></m:r></m:sup>"
            "</m:sSup>"
        )
        assert _omml_to_latex(elem) == "x^2"


class _FakeResponse:
    def __init__(self, content: bytes, headers: dict | None = None,
                 encoding: str | None = None, apparent_encoding: str | None = None):
        self.content = content
        self.headers = headers or {}
        self.encoding = encoding
        self.apparent_encoding = apparent_encoding


class TestWebCharsetDecoding:
    def test_header_charset_wins(self):
        from xgen_edit2docs.core.source_to_md.web_to_md import _decode_response_text

        text = "中文页面"
        resp = _FakeResponse(
            text.encode("gbk"),
            headers={"Content-Type": "text/html; charset=gbk"},
            apparent_encoding="ISO-8859-1",
        )
        assert _decode_response_text(resp) == text

    def test_meta_charset_fallback(self):
        from xgen_edit2docs.core.source_to_md.web_to_md import _decode_response_text

        html = '<html><head><meta charset="gb2312"></head><body>你好世界</body></html>'
        resp = _FakeResponse(html.encode("gb2312"))
        assert "你好世界" in _decode_response_text(resp)

    def test_utf8_bom_detected(self):
        from xgen_edit2docs.core.source_to_md.web_to_md import _decode_response_text

        resp = _FakeResponse("﻿plain".encode("utf-8-sig") or b"")
        resp.content = b"\xef\xbb\xbfplain"
        assert _decode_response_text(resp) == "plain"

    def test_guessed_encoding_does_not_override_valid_utf8(self):
        from xgen_edit2docs.core.source_to_md.web_to_md import _decode_response_text

        # A bogus cp1252 guess would turn curly quotes into "â€œ" mojibake;
        # the decode-quality score must prefer the clean UTF-8 reading.
        text = "Curly “quotes” – and dash"
        resp = _FakeResponse(text.encode("utf-8"), encoding="cp1252")
        assert _decode_response_text(resp) == text
