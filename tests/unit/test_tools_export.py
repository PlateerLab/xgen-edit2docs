"""Integration test: export tool renders a Korean SVG into a PPTX whose OOXML
text runs are tagged with lang="ko-KR".

This is the M1 capstone: the G1/G2/G3 patches threading all the way through
the public tool layer and out as a real .pptx file. Runs without LLM access.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from xgen_edit2docs.tools import ExportRequest, SlideInput, export_pptx

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg"


class TestExportKorean:
    def test_korean_pptx_round_trip(self):
        svg = FIXTURE.read_text(encoding="utf-8")
        result = export_pptx(
            ExportRequest(
                slides=[
                    SlideInput(
                        index=0,
                        name="slide_00_korean",
                        svg=svg,
                        notes="이 슬라이드는 한국어 패치 회귀 테스트입니다.",
                    )
                ],
                lang="ko-KR",
            )
        )
        assert result.page_count == 1
        assert result.pptx[:4] == b"PK\x03\x04", "Result is not a valid ZIP/PPTX"

        with zipfile.ZipFile(io.BytesIO(result.pptx), "r") as zf:
            names = zf.namelist()
            # Find the first slide XML.
            slide_xmls = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            assert slide_xmls, f"No slide XML found in PPTX. Members: {names[:10]}..."

            xml = zf.read(slide_xmls[0]).decode("utf-8")
            assert 'lang="ko-KR"' in xml, (
                "Korean slide must contain at least one run with lang=\"ko-KR\". "
                f"Slide XML head: {xml[:500]}"
            )
            assert 'lang="zh-CN"' not in xml, (
                "Korean slide must not contain any zh-CN runs (G2 regression)."
            )

    def test_export_detected_langs_includes_korean(self):
        svg = FIXTURE.read_text(encoding="utf-8")
        result = export_pptx(
            ExportRequest(
                slides=[SlideInput(index=0, name="slide_00", svg=svg)],
                lang="ko-KR",
            )
        )
        assert result.detected_langs == ["ko-KR"]

    def test_export_speaker_notes_korean_lang(self):
        svg = FIXTURE.read_text(encoding="utf-8")
        result = export_pptx(
            ExportRequest(
                slides=[
                    SlideInput(
                        index=0,
                        name="slide_00",
                        svg=svg,
                        notes="발표자 노트도 ko-KR 로 마킹되어야 합니다.",
                    )
                ],
                lang="ko-KR",
            )
        )
        with zipfile.ZipFile(io.BytesIO(result.pptx), "r") as zf:
            notes_xmls = [
                n for n in zf.namelist()
                if n.startswith("ppt/notesSlides/notesSlide") and n.endswith(".xml")
            ]
            assert notes_xmls, "PPTX should contain a notesSlide entry"
            xml = zf.read(notes_xmls[0]).decode("utf-8")
            assert 'lang="ko-KR"' in xml
            assert 'lang="zh-CN"' not in xml

    def test_export_empty_slides_raises(self):
        import pytest

        with pytest.raises(ValueError, match="at least one slide"):
            export_pptx(ExportRequest(slides=[], lang="ko-KR"))
