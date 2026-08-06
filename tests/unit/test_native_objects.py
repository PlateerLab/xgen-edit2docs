"""Wave-3 port: native table/chart export via data-pptx-native markers.

Covers the vendored ``svg_to_pptx.native_objects`` subpackage (upstream
cd6d91f8 + follow-ups) and its integration hooks:

- ``<g data-pptx-native="table">`` exports a real ``<a:tbl>`` graphicFrame,
  not flattened shapes (opt-in via ``native_objects=True``).
- ``<g data-pptx-native="chart">`` exports a chart part + embedded XLSX
  workbook + a slide relationship of type chart.
- The default stays OFF: marked groups fall back to their SVG children.
- The quality checker flags malformed marker payloads (restored wave-2 hook).
- ``ExportRequest(native_objects=True)`` threads end-to-end through
  ``tools.export.export_pptx``.
- flatten_tspan paragraph annotation (Task B): a multi-paragraph text block
  converts to ONE txBody with multiple ``<a:p>``.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

CHART_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart"
)

TABLE_MARKER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>
  <g id="demo_table" data-pptx-native="table">
    <metadata data-pptx-native="table">
      {
        "name": "demo_table",
        "x": 80, "y": 150, "width": 1120, "height": 300,
        "columns": [
          {"text": "Region", "bold": true},
          {"text": "Sales", "align": "r", "bold": true}
        ],
        "rows": [
          [{"text": "East"}, {"text": "185", "align": "r"}],
          [{"text": "West"}, {"text": "142", "align": "r"}]
        ]
      }
    </metadata>
    <rect x="80" y="150" width="1120" height="300" fill="#F8FAFC" stroke="#E2E8F0"/>
    <text x="100" y="180" font-size="15" fill="#0F172A">East 185</text>
  </g>
</svg>"""

CHART_MARKER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>
  <g id="demo_chart" data-pptx-native="chart">
    <metadata data-pptx-native="chart">
      {
        "name": "demo_chart",
        "x": 125, "y": 141, "width": 1000, "height": 440,
        "type": "bar",
        "categories": ["East", "South", "North"],
        "series": [{"name": "Sales", "values": [185, 142, 128]}],
        "style": {"colors": ["#3B82F6", "#10B981", "#F59E0B"]}
      }
    </metadata>
    <rect x="125" y="141" width="1000" height="440" fill="#EFF6FF"/>
    <text x="140" y="170" font-size="15" fill="#0F172A">Sales chart fallback</text>
  </g>
</svg>"""

# metadata JSON is intentionally broken (trailing comma)
MALFORMED_MARKER_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
  <rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>
  <g id="broken_table" data-pptx-native="table">
    <metadata data-pptx-native="table">
      {"name": "broken_table", "x": 80, "y": 150, "width": 1120,}
    </metadata>
    <rect x="80" y="150" width="1120" height="300" fill="#F8FAFC"/>
    <text x="100" y="180" font-size="15" fill="#0F172A">fallback</text>
  </g>
</svg>"""


def _build_deck(tmp_path: Path, svg_markup: str, **builder_kwargs) -> Path:
    from xgen_edit2docs.core.svg_to_pptx.pptx_builder import create_pptx_with_native_svg

    svg = tmp_path / "slide_00.svg"
    svg.write_text(svg_markup, encoding="utf-8")
    out = tmp_path / "deck.pptx"
    ok = create_pptx_with_native_svg(
        svg_files=[svg],
        output_path=out,
        verbose=False,
        use_native_shapes=True,
        enable_notes=False,
        **builder_kwargs,
    )
    assert ok and out.exists()
    return out


class TestNativeTableExport:
    def test_table_marker_exports_native_tbl(self, tmp_path: Path):
        out = _build_deck(tmp_path, TABLE_MARKER_SVG, native_objects=True)
        with zipfile.ZipFile(out) as zf:
            slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<p:graphicFrame>" in slide_xml
        assert "<a:tbl>" in slide_xml, "expected a native table, got shapes"
        assert "drawingml/2006/table" in slide_xml
        # Cell text made it into the native table
        assert "East" in slide_xml and "185" in slide_xml
        # The SVG fallback children were replaced, not duplicated
        assert "East 185" not in slide_xml

    def test_table_marker_defaults_to_svg_fallback(self, tmp_path: Path):
        out = _build_deck(tmp_path, TABLE_MARKER_SVG)  # native_objects off
        with zipfile.ZipFile(out) as zf:
            slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
        assert "<a:tbl>" not in slide_xml
        # Fallback children exported as plain shapes/text
        assert "East 185" in slide_xml


class TestNativeChartExport:
    def test_chart_marker_exports_chart_part_workbook_and_rel(self, tmp_path: Path):
        out = _build_deck(tmp_path, CHART_MARKER_SVG, native_objects=True)
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            chart_parts = [
                n for n in names
                if n.startswith("ppt/charts/chart") and n.endswith(".xml")
                and "_rels" not in n
            ]
            workbook_parts = [
                n for n in names
                if n.startswith("ppt/embeddings/") and n.endswith(".xlsx")
            ]
            assert chart_parts, f"no chart part in {names}"
            assert workbook_parts, f"no embedded workbook in {names}"

            slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
            assert "<p:graphicFrame>" in slide_xml
            assert "drawingml/2006/chart" in slide_xml

            rels = zf.read("ppt/slides/_rels/slide1.xml.rels").decode("utf-8")
            assert CHART_REL_TYPE in rels

            # The chart part itself must be a category bar chart with our data
            chart_xml = zf.read(chart_parts[0]).decode("utf-8")
            assert "<c:barChart>" in chart_xml
            assert "Sales" in chart_xml

            # Chart part rels reference the embedded workbook
            chart_rels = zf.read(
                f"ppt/charts/_rels/{chart_parts[0].rsplit('/', 1)[1]}.rels"
            ).decode("utf-8")
            assert "embeddings" in chart_rels

            # Content types: chart override + xlsx default
            content_types = zf.read("[Content_Types].xml").decode("utf-8")
            assert "chart+xml" in content_types
            assert "spreadsheetml.sheet" in content_types

            # Embedded workbook is a readable XLSX zip
            import io

            with zipfile.ZipFile(io.BytesIO(zf.read(workbook_parts[0]))) as wb:
                assert any(n.startswith("xl/") for n in wb.namelist())

    def test_chart_marker_defaults_to_svg_fallback(self, tmp_path: Path):
        out = _build_deck(tmp_path, CHART_MARKER_SVG)  # native_objects off
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert not any(n.startswith("ppt/charts/") for n in names)
            assert not any(n.startswith("ppt/embeddings/") for n in names)
            slide_xml = zf.read("ppt/slides/slide1.xml").decode("utf-8")
            assert "Sales chart fallback" in slide_xml


class TestMarkerValidation:
    def test_checker_flags_malformed_marker_payload(self):
        from xgen_edit2docs.tools.quality import (
            QualityCheckRequest,
            QualitySlide,
            check_svg_quality,
        )

        resp = check_svg_quality(
            QualityCheckRequest(
                slides=[QualitySlide(index=0, name="p01", svg=MALFORMED_MARKER_SVG)],
            )
        )
        native_errors = [
            i for i in resp.issues
            if i.severity == "error" and "data-pptx-native" in i.message
        ]
        assert native_errors, (
            "checker did not flag the malformed marker: "
            f"{[i.message for i in resp.issues]}"
        )
        assert not resp.passed

    def test_checker_accepts_valid_marker(self):
        from xgen_edit2docs.tools.quality import (
            QualityCheckRequest,
            QualitySlide,
            check_svg_quality,
        )

        resp = check_svg_quality(
            QualityCheckRequest(
                slides=[QualitySlide(index=0, name="p01", svg=TABLE_MARKER_SVG)],
            )
        )
        native_errors = [
            i for i in resp.issues
            if i.severity == "error" and "data-pptx-native" in i.message
        ]
        assert not native_errors, [i.message for i in native_errors]

    def test_validate_native_object_marker_raises_on_bad_payload(self):
        from xml.etree import ElementTree as ET

        import pytest

        from xgen_edit2docs.core.svg_to_pptx.native_objects import (
            validate_native_object_marker,
        )

        root = ET.fromstring(MALFORMED_MARKER_SVG)
        marker = next(
            el for el in root.iter() if el.get("data-pptx-native") == "table"
            and el.tag.rsplit("}", 1)[-1] == "g"
        )
        with pytest.raises(RuntimeError, match="not valid JSON"):
            validate_native_object_marker(marker)


class TestParagraphMerge:
    """Task B: flatten_tspan paragraph annotation drives multi-<a:p> output."""

    PARAGRAPH_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">
<text x="100" y="200" font-size="18" fill="#0F172A">
<tspan x="100">First paragraph line one</tspan>
<tspan x="100" dy="24">still first paragraph wrapped</tspan>
<tspan x="100" dy="48">Second paragraph starts here</tspan>
<tspan x="100" dy="24">second paragraph wrapped line</tspan>
</text>
</svg>"""

    def test_flatten_annotates_paragraph_block(self):
        from xml.etree import ElementTree as ET

        from xgen_edit2docs.core.svg_to_pptx.tspan_flattener import (
            flatten_positional_tspans,
        )

        tree = ET.ElementTree(ET.fromstring(self.PARAGRAPH_SVG))
        assert flatten_positional_tspans(tree, merge_paragraphs=True)
        out = ET.tostring(tree.getroot(), encoding="unicode")
        assert "data-paragraph-line-height" in out
        assert "data-paragraph-soft-break" in out
        assert "data-paragraph-space-before" in out
        # Merge mode keeps the block as ONE <text>
        assert out.count("<text") == 1

    def test_merge_mode_produces_one_txbody_multiple_paragraphs(self, tmp_path: Path):
        from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
            convert_svg_to_slide_shapes,
        )

        svg = tmp_path / "para.svg"
        svg.write_text(self.PARAGRAPH_SVG, encoding="utf-8")
        slide_xml, _media, _rels, _anim, _pkg, _cto = convert_svg_to_slide_shapes(
            svg, merge_paragraphs=True
        )
        # ONE editable text frame ...
        assert slide_xml.count("<p:txBody>") == 1
        # ... with one <a:p> per paragraph (soft-wrapped lines merge into
        # their paragraph rather than opening a new one).
        assert slide_xml.count("<a:p>") == 2

        # Each paragraph carries its soft-wrapped line as extra runs joined
        # with a space, so the per-<a:p> text reads as one flowing paragraph.
        from xml.etree import ElementTree as ET

        A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
        root = ET.fromstring(slide_xml)
        paragraphs = [
            "".join(t.text or "" for t in p.iter(f"{{{A_NS}}}t"))
            for p in root.iter(f"{{{A_NS}}}p")
        ]
        assert paragraphs == [
            "First paragraph line one still first paragraph wrapped",
            "Second paragraph starts here second paragraph wrapped line",
        ]

    def test_no_merge_mode_preserves_line_layout(self, tmp_path: Path):
        from xgen_edit2docs.core.svg_to_pptx.drawingml_converter import (
            convert_svg_to_slide_shapes,
        )

        svg = tmp_path / "para.svg"
        svg.write_text(self.PARAGRAPH_SVG, encoding="utf-8")
        slide_xml, _media, _rels, _anim, _pkg, _cto = convert_svg_to_slide_shapes(
            svg, merge_paragraphs=False
        )
        # Strict line fidelity: one textbox per visual line
        assert slide_xml.count("<p:txBody>") == 4


class TestExportToolThreading:
    def test_export_request_native_objects_threads_end_to_end(self):
        from xgen_edit2docs.tools.export import ExportRequest, SlideInput, export_pptx

        resp = export_pptx(
            ExportRequest(
                slides=[SlideInput(index=0, name="p01", svg=CHART_MARKER_SVG)],
                native_objects=True,
                enable_notes=False,
            )
        )
        import io

        with zipfile.ZipFile(io.BytesIO(resp.pptx)) as zf:
            names = zf.namelist()
            assert any(n.startswith("ppt/charts/chart") for n in names)
            assert any(n.startswith("ppt/embeddings/") for n in names)

    def test_export_request_defaults_off(self):
        from xgen_edit2docs.tools.export import ExportRequest, SlideInput, export_pptx

        req = ExportRequest(
            slides=[SlideInput(index=0, name="p01", svg=CHART_MARKER_SVG)],
            enable_notes=False,
        )
        assert req.native_objects is False
        resp = export_pptx(req)
        import io

        with zipfile.ZipFile(io.BytesIO(resp.pptx)) as zf:
            assert not any(n.startswith("ppt/charts/") for n in zf.namelist())
