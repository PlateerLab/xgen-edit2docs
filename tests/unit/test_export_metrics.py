"""Post-export structural metrics tests.

The metrics block is a diagnostic, not a contract — it must degrade
gracefully (return defaults) when the PPTX is malformed and surface
useful structural numbers when it's healthy.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from xgen_edit2docs.tools._export_metrics import (
    ExportMetrics,
    compute_export_metrics,
)


def _build_test_pptx(
    *,
    slides: list[str],
    media_files: dict[str, bytes] | None = None,
) -> bytes:
    """Build the minimum viable .pptx structure for metric collection.

    Real PPTX zips carry dozens of relationship / content-type files;
    `compute_export_metrics` only reads slide XML + counts `ppt/media/`
    entries, so we get away with a stripped-down archive.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, xml in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
        for name, data in (media_files or {}).items():
            zf.writestr(f"ppt/media/{name}", data)
    return buf.getvalue()


_GOOD_SLIDE = """<?xml version="1.0"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree>
  <p:sp>
    <p:nvSpPr><p:cNvPr id="2" name="Title"/></p:nvSpPr>
    <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="500000"/></a:xfrm></p:spPr>
    <p:txBody><a:p><a:r>
      <a:rPr sz="2400"><a:latin typeface="Pretendard"/><a:ea typeface="Malgun Gothic"/></a:rPr>
      <a:t>제목</a:t>
    </a:r></a:p></p:txBody>
  </p:sp>
  <p:sp>
    <p:nvSpPr><p:cNvPr id="3" name="Body"/></p:nvSpPr>
    <p:spPr><a:xfrm><a:off x="0" y="600000"/><a:ext cx="2000000" cy="500000"/></a:xfrm>
      <a:solidFill><a:srgbClr val="0A1628"/></a:solidFill>
    </p:spPr>
    <p:txBody><a:p><a:r>
      <a:rPr sz="1800"><a:latin typeface="Pretendard"/></a:rPr>
      <a:t>본문</a:t>
    </a:r></a:p></p:txBody>
  </p:sp>
</p:spTree></p:cSld></p:sld>
"""

_PLACEHOLDER_SLIDE = """<?xml version="1.0"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree>
  <p:sp>
    <p:nvSpPr><p:cNvPr id="2" name="placeholder"/></p:nvSpPr>
    <p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12192000" cy="6858000"/></a:xfrm></p:spPr>
    <p:txBody><a:p><a:r>
      <a:rPr sz="2800"><a:latin typeface="Malgun Gothic"/></a:rPr>
      <a:t>슬라이드 3 렌더링 실패</a:t>
    </a:r></a:p>
    <a:p><a:r>
      <a:rPr sz="1400"><a:latin typeface="Malgun Gothic"/></a:rPr>
      <a:t>Slide 3 could not be rendered.</a:t>
    </a:r></a:p></p:txBody>
  </p:sp>
</p:spTree></p:cSld></p:sld>
"""


def test_basic_slide_counts():
    pptx = _build_test_pptx(slides=[_GOOD_SLIDE, _GOOD_SLIDE, _GOOD_SLIDE])
    m = compute_export_metrics(pptx)
    assert m.total_slides == 3
    assert m.embedded_images == 0
    assert m.avg_shapes_per_slide == 2.0  # title + body per slide
    assert m.placeholder_slides == 0


def test_embedded_image_count():
    pptx = _build_test_pptx(
        slides=[_GOOD_SLIDE],
        media_files={"image1.png": b"PNGdata", "image2.jpg": b"JPGdata"},
    )
    m = compute_export_metrics(pptx)
    assert m.embedded_images == 2


def test_palette_extracted():
    pptx = _build_test_pptx(slides=[_GOOD_SLIDE])
    m = compute_export_metrics(pptx)
    # Only the body shape declared srgbClr=0A1628.
    assert m.color_palette_size == 1


def test_fonts_collected():
    pptx = _build_test_pptx(slides=[_GOOD_SLIDE])
    m = compute_export_metrics(pptx)
    # `latin=Pretendard` (both shapes) + `ea=Malgun Gothic` (title only)
    assert sorted(m.fonts_used) == ["Malgun Gothic", "Pretendard"]


def test_placeholder_slide_detected_korean():
    pptx = _build_test_pptx(slides=[_GOOD_SLIDE, _PLACEHOLDER_SLIDE])
    m = compute_export_metrics(pptx)
    assert m.placeholder_slides == 1


def test_canvas_fill_ratio_nonzero_when_content_present():
    pptx = _build_test_pptx(slides=[_GOOD_SLIDE])
    m = compute_export_metrics(pptx)
    # Total non-background area = 1M × 0.5M + 2M × 0.5M = 1.5e12
    # Canvas = 12.192M × 6.858M = ~8.36e13
    # Ratio ~ 0.018
    assert 0.01 < m.canvas_fill_ratio < 0.05


def test_malformed_zip_returns_empty_metrics():
    m = compute_export_metrics(b"not a zip at all")
    assert m.total_slides == 0
    assert m.embedded_images == 0
    assert m.fonts_used == []


def test_empty_zip_returns_zero():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dummy", "")
    m = compute_export_metrics(buf.getvalue())
    assert m.total_slides == 0


def test_to_dict_shape():
    m = ExportMetrics(
        total_slides=10,
        placeholder_slides=1,
        embedded_images=2,
        avg_shapes_per_slide=12.4,
        color_palette_size=7,
        fonts_used=["Pretendard", "Malgun Gothic"],
        canvas_fill_ratio=0.523456,
    )
    d = m.to_dict()
    assert d["total_slides"] == 10
    assert d["avg_shapes_per_slide"] == 12.4
    assert d["canvas_fill_ratio"] == 0.523  # rounded
    assert d["fonts_used"] == ["Pretendard", "Malgun Gothic"]


def test_real_world_deck_2_pptx_baseline():
    """Smoke-test against the deck_2.pptx baseline we checked into
    ppt-master-analysis/. Verifies the placeholder detector finds slide
    10 (the one that failed conversion in the production run)."""
    pptx_path = (
        Path(__file__).resolve().parents[2]
        / "ppt-master-analysis" / "deck_2.pptx"
    )
    if not pptx_path.exists():
        pytest.skip("baseline deck not checked in")
    m = compute_export_metrics(pptx_path.read_bytes())
    assert m.total_slides == 10
    assert m.placeholder_slides == 1  # slide 10 was the failure
    # Heavy fonts / colors as observed in our earlier analysis.
    assert m.color_palette_size >= 5
    assert "Malgun Gothic" in m.fonts_used
