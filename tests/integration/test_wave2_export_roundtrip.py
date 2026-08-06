"""End-to-end smoke for the wave-2 upstream ports.

Builds an SVG that exercises three ported code paths at once —
a CSS color-name fill (91a5111b), pt-unit lengths (03ba1957), and a
raster image data URI (6341f04a) — exports it to a native PPTX via
``create_pptx_with_native_svg``, then renders the deck back to SVG with
``tools.render_preview``. The round trip must not raise and must yield
non-empty SVG output.
"""

from __future__ import annotations

import base64
import zipfile
from pathlib import Path

# 1x1 red PNG
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
    "z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

SMOKE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
    '<rect x="0" y="0" width="1280" height="720" fill="white"/>'
    # CSS color name fill + pt-unit geometry
    '<rect x="1in" y="0.5in" width="240pt" height="120pt" fill="navy"/>'
    '<text x="120" y="400" font-size="18pt" fill="orange" letter-spacing="1px">'
    "Wave2 smoke deck</text>"
    # raster data URI image
    f'<image x="800" y="100" width="200" height="200" '
    f'href="data:image/png;base64,{_PNG_B64}"/>'
    "</svg>"
)


def test_export_and_preview_roundtrip(tmp_path: Path):
    from xgen_edit2docs.core.svg_to_pptx.pptx_builder import create_pptx_with_native_svg
    from xgen_edit2docs.tools.render_preview import RenderPreviewRequest, render_preview

    svg = tmp_path / "slide_00.svg"
    svg.write_text(SMOKE_SVG, encoding="utf-8")
    out = tmp_path / "smoke.pptx"

    ok = create_pptx_with_native_svg(
        svg_files=[svg],
        output_path=out,
        verbose=False,
        use_native_shapes=True,
    )
    assert ok and out.exists()

    # Structural sanity: one slide, one embedded raster media file.
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "ppt/slides/slide1.xml" in names
        assert any(n.startswith("ppt/media/") and n.endswith(".png") for n in names)
        slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        # navy resolved through the CSS named-color table
        assert "000080" in slide_xml
        # orange text fill
        assert "FFA500" in slide_xml

    # Round-trip back to SVG.
    response = render_preview(RenderPreviewRequest(pptx=out.read_bytes()))
    assert response.slides, "preview produced no slides"
    first = response.slides[0].svg
    assert first.strip(), "preview SVG is empty"
    assert "<svg" in first
