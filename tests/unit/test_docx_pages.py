"""M3 — DOCX page engine (docx_pages.docx_to_page_svgs)."""

from __future__ import annotations

from xgen_edit2docs.documents.docx_engine import docx_from_markdown
from xgen_edit2docs.documents.docx_pages import docx_to_page_svgs


def _doc(md: str) -> bytes:
    return docx_from_markdown(md)


class TestPagination:
    def test_single_page_for_short_doc(self):
        pages = docx_to_page_svgs(_doc("# Title\n\nOne paragraph."))
        assert len(pages) == 1
        assert pages[0].startswith("<svg")

    def test_long_doc_flows_to_multiple_pages(self):
        md = "\n\n".join(f"Paragraph {i}. " + "내용 텍스트 " * 20 for i in range(80))
        pages = docx_to_page_svgs(_doc(md))
        assert len(pages) >= 2

    def test_honors_sectpr_page_size(self):
        # python-docx's default template declares US Letter (12240×15840
        # twips → 816×1056 px) — the engine must read sectPr, not assume A4.
        page = docx_to_page_svgs(_doc("x"))[0]
        assert 'width="816"' in page and 'height="1056"' in page


class TestContent:
    def test_paragraph_addressing_tags(self):
        pages = docx_to_page_svgs(_doc("first\n\nsecond\n\nthird"))
        joined = "".join(pages)
        assert 'data-e2d-para="0"' in joined
        assert 'data-e2d-para="2"' in joined

    def test_table_renders_with_addresses(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |"
        joined = "".join(docx_to_page_svgs(_doc(md)))
        assert 'data-e2d-table="0"' in joined
        assert 'data-e2d-cell="1,1"' in joined
        assert joined.count("<rect") >= 4  # page bg + cells

    def test_heading_is_larger_and_bold(self):
        joined = "".join(docx_to_page_svgs(_doc("# 큰제목\n\n본문")))
        assert 'font-weight="bold"' in joined
        assert 'font-size="26.67"' in joined  # 20pt → 26.67px

    def test_bullets_render(self):
        joined = "".join(docx_to_page_svgs(_doc("- one\n- two")))
        assert "•" in joined

    def test_text_is_escaped(self):
        joined = "".join(docx_to_page_svgs(_doc("a < b & c > d")))
        assert "&lt;" in joined and "&amp;" in joined
        assert "a < b" not in joined
