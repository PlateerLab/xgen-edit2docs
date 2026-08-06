"""Unit tests for the deterministic DOCX / XLSX engines."""

from __future__ import annotations

import pytest

from xgen_edit2docs.documents.docx_engine import (
    DocxEdit,
    apply_docx_edits,
    docx_from_markdown,
    docx_outline,
    docx_to_markdown,
)
from xgen_edit2docs.documents.xlsx_engine import (
    XlsxEdit,
    apply_xlsx_edits,
    xlsx_from_spec,
    xlsx_outline,
    xlsx_to_markdown,
)

MD = (
    "# 분기 보고서\n\n"
    "## 요약\n"
    "**매출**이 전년 대비 12% 성장했습니다.\n\n"
    "- 신규 고객 34개사\n"
    "- 갱신율 91%\n\n"
    "| 분기 | 매출 |\n|---|---|\n| 1분기 | 120 |\n| 2분기 | 135 |\n"
)


class TestDocxEngine:
    def test_markdown_round_trip(self):
        content = docx_from_markdown(MD)
        assert content[:4] == b"PK\x03\x04"
        back = docx_to_markdown(content)
        assert "분기 보고서" in back and "신규 고객 34개사" in back

    def test_outline_addresses_paragraphs_and_cells(self):
        outline = docx_outline(docx_from_markdown(MD))
        styles = {e["style"] for e in outline if "para" in e}
        assert any("Heading" in s for s in styles)
        cells = [e for e in outline if "table" in e]
        assert {"table": 0, "row": 1, "col": 1, "text": "120"} in cells

    def test_replace_insert_delete(self):
        content = docx_from_markdown(MD)
        outline = docx_outline(content)
        target = next(e for e in outline if "12%" in e["text"])
        new_content, results = apply_docx_edits(
            content,
            [
                DocxEdit(action="replace", para=target["para"],
                         new_text="매출이 15% 성장했습니다.", old_text=target["text"]),
                DocxEdit(action="insert_after", para=target["para"],
                         markdown="## 신규 섹션\n- 해외 매출 2배"),
            ],
        )
        assert [r.status for r in results] == ["applied", "applied"]
        texts = [e["text"] for e in docx_outline(new_content)]
        assert any("15%" in t for t in texts)
        assert "신규 섹션" in texts and any("해외 매출" in t for t in texts)

        bullet = next(e for e in docx_outline(new_content) if "갱신율" in e["text"])
        deleted, r2 = apply_docx_edits(
            new_content, [DocxEdit(action="delete", para=bullet["para"])]
        )
        assert r2[0].status == "applied"
        assert not any("갱신율" in e["text"] for e in docx_outline(deleted))

    def test_stale_guard_and_not_found(self):
        content = docx_from_markdown(MD)
        _, results = apply_docx_edits(
            content,
            [
                DocxEdit(action="replace", para=0, new_text="x", old_text="다른 텍스트"),
                DocxEdit(action="replace", para=999, new_text="x"),
            ],
        )
        assert [r.status for r in results] == ["stale", "not_found"]

    def test_table_cell_replace(self):
        content = docx_from_markdown(MD)
        new_content, results = apply_docx_edits(
            content,
            [DocxEdit(action="replace", table=0, row=2, col=1, new_text="142")],
        )
        assert results[0].status == "applied"
        cells = [e for e in docx_outline(new_content) if e.get("table") == 0]
        assert any(e["text"] == "142" for e in cells)


SPEC = {
    "sheets": [
        {
            "name": "매출",
            "headers": ["분기", "금액"],
            "rows": [["1분기", 120], ["2분기", 135]],
            "number_formats": {"B": "#,##0"},
        }
    ]
}


class TestXlsxEngine:
    def test_spec_render_and_outline(self):
        content = xlsx_from_spec(SPEC)
        assert content[:4] == b"PK\x03\x04"
        outline = xlsx_outline(content)
        sheet = outline["sheets"][0]
        assert sheet["name"] == "매출" and sheet["rows"] == 3
        assert sheet["sample"][1] == ["1분기", 120]

    def test_markdown_rendering(self):
        md = xlsx_to_markdown(xlsx_from_spec(SPEC))
        assert "## 매출" in md and "| 1분기 | 120 |" in md

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError):
            xlsx_from_spec({"sheets": []})
        with pytest.raises(ValueError):
            xlsx_from_spec({"sheets": [{"name": "x", "rows": ["not-a-list"]}]})

    def test_set_cell_append_add_sheet(self):
        content = xlsx_from_spec(SPEC)
        new_content, results = apply_xlsx_edits(
            content,
            [
                XlsxEdit(action="set_cell", sheet="매출", cell="B3", value=142,
                         old_value="135"),
                XlsxEdit(action="append_rows", sheet="매출", rows=[["3분기", 150]]),
                XlsxEdit(action="add_sheet", sheet="메모", headers=["항목"],
                         rows=[["검토"]]),
            ],
        )
        assert [r.status for r in results] == ["applied"] * 3
        outline = xlsx_outline(new_content)
        assert outline["sheets"][0]["sample"][2][1] == 142
        assert outline["sheets"][0]["rows"] == 4
        assert [s["name"] for s in outline["sheets"]] == ["매출", "메모"]

    def test_guards(self):
        content = xlsx_from_spec(SPEC)
        _, results = apply_xlsx_edits(
            content,
            [
                XlsxEdit(action="set_cell", sheet="매출", cell="B3", value=1,
                         old_value="999"),
                XlsxEdit(action="set_cell", sheet="없는시트", cell="A1", value=1),
                XlsxEdit(action="set_cell", sheet="매출", cell="NOT_A_CELL", value=1),
            ],
        )
        assert [r.status for r in results] == ["stale", "not_found", "invalid"]


class TestReviewRegressions:
    """Regressions for the 2026-07-02 full-format review findings."""

    def test_docx_results_follow_input_order(self):
        # Edits deliberately NOT in ascending-para order; the stale one must
        # be reported at ITS index, not sorted position.
        content = docx_from_markdown("첫째\n\n둘째\n\n셋째")
        _, results = apply_docx_edits(
            content,
            [
                DocxEdit(action="replace", para=2, new_text="x", old_text="틀린값"),
                DocxEdit(action="replace", para=0, new_text="바뀐 첫째", old_text="첫째"),
            ],
        )
        assert [r.status for r in results] == ["stale", "applied"]

    def test_docx_negative_para_rejected(self):
        content = docx_from_markdown("하나\n\n둘")
        _, results = apply_docx_edits(
            content,
            [
                DocxEdit(action="replace", para=-1, new_text="x"),
                DocxEdit(action="delete", para=-2),
                DocxEdit(action="insert_after", para=-1, markdown="시작 문단"),
            ],
        )
        assert [r.status for r in results] == ["not_found", "not_found", "applied"]

    def test_docx_hyperlink_paragraph_replace_clears_link_text(self):
        import io as _io

        from docx import Document as _Doc
        from docx.oxml.ns import qn as _qn
        from docx.oxml import OxmlElement as _El

        doc = _Doc()
        p = doc.add_paragraph()
        hyperlink = _El("w:hyperlink")
        run = _El("w:r")
        text_el = _El("w:t")
        text_el.text = "옛날링크"
        run.append(text_el)
        hyperlink.append(run)
        p._p.append(hyperlink)
        buf = _io.BytesIO()
        doc.save(buf)

        new_content, results = apply_docx_edits(
            buf.getvalue(), [DocxEdit(action="replace", para=0, new_text="새 텍스트")]
        )
        assert results[0].status == "applied"
        texts = [e["text"] for e in docx_outline(new_content)]
        assert texts == ["새 텍스트"]  # old link text must NOT survive

    def test_docx_dash_data_rows_survive(self):
        content = docx_from_markdown("| a | b |\n|---|---|\n| - | - |\n| 1 | 2 |")
        cells = [e["text"] for e in docx_outline(content) if "table" in e]
        assert cells.count("-") == 2  # the '| - | - |' DATA row is kept

    def test_docx_insert_after_carries_tables(self):
        content = docx_from_markdown("문단 하나")
        new_content, results = apply_docx_edits(
            content,
            [DocxEdit(action="insert_after", para=0,
                      markdown="추가 문단\n\n| h |\n|---|\n| v |")],
        )
        assert results[0].status == "applied"
        outline = docx_outline(new_content)
        assert any(e.get("table") == 0 and e["text"] == "v" for e in outline)

    def test_docx_to_html_neutralizes_javascript_hrefs(self):
        from xgen_edit2docs.documents.docx_engine import _sanitize_preview_html

        dirty = '<p><a href="javascript:alert(1)">x</a> <a href="https://ok.com">y</a></p>'
        clean = _sanitize_preview_html(dirty)
        assert "javascript:" not in clean
        assert 'href="https://ok.com"' in clean and 'rel="noopener' in clean

    def test_xlsx_duplicate_sheet_names_rejected(self):
        with pytest.raises(ValueError, match="duplicate|중복"):
            xlsx_from_spec({"sheets": [
                {"name": "같음", "headers": ["a"], "rows": [[1]]},
                {"name": "같음", "headers": ["b"], "rows": [[2]]},
            ]})

    def test_xlsx_number_formats_validation(self):
        with pytest.raises(ValueError, match="column letters|열 문자"):
            xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]],
                                        "number_formats": {"1": "#,##0"}}]})
        # Valid letter beyond data: skipped, no phantom columns.
        content = xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]],
                                              "number_formats": {"Z": "#,##0"}}]})
        assert xlsx_outline(content)["sheets"][0]["columns"] == 1

    def test_xlsx_widths_must_be_numeric(self):
        with pytest.raises(ValueError, match="numbers|숫자"):
            xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]],
                                        "widths": ["wide"]}]})

    def test_xlsx_cell_beyond_excel_limit_invalid(self):
        content = xlsx_from_spec({"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]]}]})
        _, results = apply_xlsx_edits(
            content, [XlsxEdit(action="set_cell", sheet="S", cell="ZZZ1", value=1)]
        )
        assert results[0].status == "invalid"
