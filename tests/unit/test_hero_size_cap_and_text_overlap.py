"""Regressions from deck_4.pptx.

Two failure modes captured in the screenshots the user shared:

1. Slide 9: the model emitted "76%" as a hero number at 255 pt
   (340 px SVG), taking up half the canvas and overlapping the
   actual title, hero `44%`, and caption. The converter now caps
   font-size at 180 pt (240 px SVG) so the rest of the slide stays
   readable.
2. Slide 10: the chapter label `CHAPTER 02 · AI 코딩의 진화` was
   positioned INSIDE the title `5년 만에 일어난 일` (same x, y inside
   the title's box). The previous layout-repair skipped this as
   "intentional containment" because one element contained the
   other. We now treat text-on-text containment as overlap (the only
   real intentional-containment pattern is text on a non-text card).
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

SVG = "http://www.w3.org/2000/svg"


def _build_slide_xml(svg: str, tmp_path: Path) -> str:
    from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
        convert_svg_to_slide_shapes,
    )

    svg_path = tmp_path / "slide.svg"
    svg_path.write_text(svg, encoding="utf-8")
    xml, _media, _rels, _anim, _pkg, _cto = convert_svg_to_slide_shapes(svg_path)
    return xml


def _sizes_in(xml: str) -> list[int]:
    return [int(m) for m in re.findall(r'\bsz="(\d+)"', xml)]


# ---------------------------------------------------------------------------
# Font-size ceiling (regression for slide 9's giant "76")
# ---------------------------------------------------------------------------


def test_font_size_capped_at_180pt(tmp_path):
    """A model-emitted 340px SVG font (= 255pt at 0.75 ratio) must
    cap at 18000 (180pt) so it doesn't take over the slide."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" '
        'viewBox="0 0 1280 720">'
        '<text x="100" y="500" font-size="340" font-family="Pretendard">76</text>'
        '</svg>'
    )
    xml = _build_slide_xml(svg, tmp_path)
    sizes = _sizes_in(xml)
    assert max(sizes) <= 18000, f"max size was {max(sizes)} (expected ≤ 18000)"


def test_within_ceiling_text_unchanged(tmp_path):
    """A 100 pt hero number (133px SVG) should round-trip untouched."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" '
        'viewBox="0 0 1280 720">'
        '<text x="100" y="500" font-size="133" font-family="Pretendard">44%</text>'
        '</svg>'
    )
    xml = _build_slide_xml(svg, tmp_path)
    sizes = _sizes_in(xml)
    # 133 × 75 / 100 = 99.75 → rounds to 9975 (just under 100pt).
    assert 9000 <= max(sizes) <= 10500


@pytest.mark.parametrize("svg_px", [250, 300, 400, 600])
def test_extreme_sizes_all_capped(tmp_path, svg_px):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        f'<text x="0" y="500" font-size="{svg_px}" font-family="Pretendard">X</text>'
        f'</svg>'
    )
    xml = _build_slide_xml(svg, tmp_path)
    sizes = _sizes_in(xml)
    assert max(sizes) <= 18000


# ---------------------------------------------------------------------------
# Text-in-text overlap (regression for slide 10's chapter overlap)
# ---------------------------------------------------------------------------


def test_chapter_label_inside_title_box_now_flagged_and_shifted():
    """deck_4.pptx slide 10: the chapter label was at (79, 129) inside
    the title box (74, 92, 583×90). Both are <text>; layout_repair
    must shift the smaller text away from the larger."""
    from xgen_edit2docs.core.svg_to_pptx.layout_repair import repair_layout

    svg = f"""<svg xmlns="{SVG}" width="1280" height="720" viewBox="0 0 1280 720">
      <rect width="1280" height="720" fill="#fff"/>
      <text id="title" x="74" y="170" font-size="56" font-weight="900">5년 만에 일어난 일</text>
      <text id="chapter" x="79" y="150" font-size="16" font-weight="700">CHAPTER 02 — AI 코딩의 진화</text>
    </svg>"""
    result = repair_layout(svg)
    kinds = [v.kind for v in result.violations]
    assert "overlap" in kinds
    # The chapter label is the smaller text → should shift below the title.
    root = ET.fromstring(result.repaired_svg)
    chapter = root.find(f".//{{{SVG}}}text[@id='chapter']")
    assert chapter is not None
    # Original chapter y=150 sat inside title box (top ~122). After
    # the fix, chapter y should land below title bottom (~178).
    assert float(chapter.get("y")) > 178


def test_intentional_text_on_card_still_not_flagged():
    """Text contained inside a non-text container (a background card)
    is the legitimate layering pattern — must continue to pass."""
    from xgen_edit2docs.core.svg_to_pptx.layout_repair import repair_layout

    svg = f"""<svg xmlns="{SVG}" width="1280" height="720" viewBox="0 0 1280 720">
      <rect width="1280" height="720" fill="#fff"/>
      <rect id="card" x="100" y="100" width="600" height="400" fill="#e0e0ff"/>
      <text id="label" x="120" y="160" font-size="40">제목</text>
    </svg>"""
    result = repair_layout(svg)
    assert "overlap" not in [v.kind for v in result.violations]


def test_text_in_text_with_no_overlap_not_flagged():
    """Two text elements with disjoint boxes still don't flag — only
    containment between text bodies fires the rule."""
    from xgen_edit2docs.core.svg_to_pptx.layout_repair import repair_layout

    svg = f"""<svg xmlns="{SVG}" width="1280" height="720" viewBox="0 0 1280 720">
      <rect width="1280" height="720" fill="#fff"/>
      <text id="title" x="60" y="100" font-size="40">제목</text>
      <text id="body" x="60" y="400" font-size="20">본문</text>
    </svg>"""
    result = repair_layout(svg)
    assert "overlap" not in [v.kind for v in result.violations]
