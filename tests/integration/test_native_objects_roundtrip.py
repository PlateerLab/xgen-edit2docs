"""End-to-end smoke for wave-3 native chart export.

Exports a ``data-pptx-native="chart"``-marked SVG through the public
``tools.export.export_pptx`` surface with ``native_objects=True`` (real
PowerPoint chart part + embedded workbook), then feeds the deck back
through ``tools.render_preview``. The chart preview may fall back to the
fork's chart_to_svg renderer — the contract here is that the roundtrip
does not raise and yields non-empty SVG.
"""

from __future__ import annotations

import io
import zipfile

CHART_MARKER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>
  <text x="80" y="90" font-size="28" font-weight="700" fill="#0F172A">Quarterly Sales</text>
  <g id="sales_chart" data-pptx-native="chart">
    <metadata data-pptx-native="chart">
      {
        "name": "sales_chart",
        "x": 125, "y": 141, "width": 1000, "height": 440,
        "type": "bar",
        "categories": ["East", "South", "North", "West"],
        "series": [{"name": "Sales", "values": [185, 142, 128, 96]}],
        "style": {"colors": ["#3B82F6", "#10B981", "#F59E0B"]}
      }
    </metadata>
    <rect x="125" y="141" width="1000" height="440" fill="#EFF6FF"/>
    <text x="140" y="170" font-size="15" fill="#0F172A">Sales chart fallback</text>
  </g>
</svg>"""


def test_native_chart_export_preview_roundtrip():
    from xgen_edit2docs.tools.export import ExportRequest, SlideInput, export_pptx
    from xgen_edit2docs.tools.render_preview import RenderPreviewRequest, render_preview

    resp = export_pptx(
        ExportRequest(
            slides=[SlideInput(index=0, name="p01", svg=CHART_MARKER_SVG)],
            native_objects=True,
            enable_notes=False,
        )
    )
    assert resp.page_count == 1

    # Structural sanity: the deck really contains native chart machinery.
    with zipfile.ZipFile(io.BytesIO(resp.pptx)) as zf:
        names = zf.namelist()
        assert any(n.startswith("ppt/charts/chart") for n in names)
        assert any(
            n.startswith("ppt/embeddings/") and n.endswith(".xlsx") for n in names
        )
        slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<p:graphicFrame>" in slide_xml

    # Roundtrip: preview must not raise even though the slide's main visual
    # is a chart graphicFrame (renderer may use the chart_to_svg fallback).
    preview = render_preview(RenderPreviewRequest(pptx=resp.pptx))
    assert preview.slides, "preview produced no slides"
    first = preview.slides[0].svg
    assert first.strip() and "<svg" in first
