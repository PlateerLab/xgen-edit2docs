"""Focused tests for the wave-2 upstream ports (export + checker chain).

Covers the fixes ported from ppt-master:
- CSS color names / rgb() in paint parsing (91a5111b)
- SVG length units px/pt/em/% (03ba1957)
- single-quoted viewBox recognition (90dc05de)
- PowerPoint package compatibility: notesMaster part + no dangling rels
  (f43e8644 / 767332d1 / 9aa1c850)
- checker errors on silent-loss hsl() paint (89c75a76)
- letter-spacing in width estimation and rPr@spc emission (b5e9b64f)
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from xgen_edit2docs.core.svg_to_pptx.drawingml_utils import (
    parse_hex_color,
    parse_inline_style,
    parse_svg_length,
)
from xgen_edit2docs.core.svg_to_pptx.drawingml_elements import (
    _build_run_xml,
    _estimate_run_text_width,
    _parse_letter_spacing_px,
)
from xgen_edit2docs.core.svg_to_pptx.pptx_builder import (
    _verify_internal_rels_targets,
    create_pptx_with_native_svg,
)
from xgen_edit2docs.core.svg_to_pptx.pptx_dimensions import get_viewbox_dimensions


# ---------------------------------------------------------------------------
# CSS color names / rgb()
# ---------------------------------------------------------------------------

class TestCssColorParsing:
    def test_named_colors(self):
        assert parse_hex_color("red") == "FF0000"
        assert parse_hex_color("navy") == "000080"
        assert parse_hex_color("White") == "FFFFFF"

    def test_transparent_returns_none(self):
        assert parse_hex_color("transparent") is None

    def test_rgb_functional_notation(self):
        assert parse_hex_color("rgb(255, 0, 0)") == "FF0000"
        assert parse_hex_color("rgb(100%, 0%, 50%)") == "FF0080"

    def test_hex_still_works(self):
        assert parse_hex_color("#1B64DA") == "1B64DA"
        assert parse_hex_color("#abc") == "AABBCC"

    def test_inline_style_parsing(self):
        styles = parse_inline_style("fill: red; stroke-width: 2px;")
        assert styles["fill"] == "red"
        assert styles["stroke-width"] == "2px"


# ---------------------------------------------------------------------------
# SVG length units
# ---------------------------------------------------------------------------

class TestSvgLengthUnits:
    def test_px_and_unitless(self):
        assert parse_svg_length("24px") == 24.0
        assert parse_svg_length("24") == 24.0

    def test_pt_converts_to_px(self):
        # 1pt = 96/72 px
        assert parse_svg_length("12pt") == 16.0
        assert parse_svg_length("72pt") == 96.0

    def test_in_cm_mm(self):
        assert parse_svg_length("1in") == 96.0
        assert abs(parse_svg_length("2.54cm") - 96.0) < 1e-9
        assert abs(parse_svg_length("25.4mm") - 96.0) < 1e-9

    def test_em_uses_font_size(self):
        assert parse_svg_length("2em", font_size=18.0) == 36.0

    def test_percent_needs_base(self):
        assert parse_svg_length("50%", default=7.0) == 7.0  # no base -> default
        assert parse_svg_length("50%", percent_base=1280.0) == 640.0

    def test_garbage_falls_back_to_default(self):
        assert parse_svg_length("bogus", default=3.0) == 3.0


# ---------------------------------------------------------------------------
# Single-quoted viewBox
# ---------------------------------------------------------------------------

class TestSingleQuotedViewBox:
    def test_get_viewbox_dimensions_single_quotes(self, tmp_path: Path):
        svg = tmp_path / "single.svg"
        svg.write_text(
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 1280 720'>"
            "<rect x='0' y='0' width='1280' height='720' fill='#FFFFFF'/></svg>",
            encoding="utf-8",
        )
        assert get_viewbox_dimensions(svg) == (1280, 720)

    def test_get_viewbox_dimensions_double_quotes_unchanged(self, tmp_path: Path):
        svg = tmp_path / "double.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080"></svg>',
            encoding="utf-8",
        )
        assert get_viewbox_dimensions(svg) == (1920, 1080)


# ---------------------------------------------------------------------------
# PowerPoint package compatibility
# ---------------------------------------------------------------------------

SVG_SLIDE = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
    '<rect x="0" y="0" width="1280" height="720" fill="#FFFFFF"/>'
    '<text x="100" y="120" font-size="32" fill="#111111">Compat slide</text>'
    "</svg>"
)


class TestPowerPointCompatPackage:
    def test_notes_master_part_and_no_dangling_rels(self, tmp_path: Path):
        svg = tmp_path / "slide_00.svg"
        svg.write_text(SVG_SLIDE, encoding="utf-8")
        out = tmp_path / "deck.pptx"

        ok = create_pptx_with_native_svg(
            svg_files=[svg],
            output_path=out,
            verbose=False,
            use_native_shapes=True,
            notes={"slide_00": "speaker notes body"},
        )
        assert ok and out.exists()

        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            assert "ppt/notesMasters/notesMaster1.xml" in names
            assert "ppt/notesMasters/_rels/notesMaster1.xml.rels" in names
            # 767332d1: notes theme part referenced by the notes master
            assert "ppt/theme/theme2.xml" in names
            content_types = zf.read("[Content_Types].xml").decode("utf-8")
            assert "/ppt/notesMasters/notesMaster1.xml" in content_types
            assert "/ppt/theme/theme2.xml" in content_types
            # presentation.xml must reference the notes master
            presentation = zf.read("ppt/presentation.xml").decode("utf-8")
            assert "<p:notesMasterIdLst>" in presentation

        # 9aa1c850: every internal rels Target must resolve inside the package
        extract_dir = tmp_path / "extracted"
        with zipfile.ZipFile(out) as zf:
            zf.extractall(extract_dir)
        assert _verify_internal_rels_targets(extract_dir) == []


# ---------------------------------------------------------------------------
# Checker: hsl() silent-loss paint
# ---------------------------------------------------------------------------

class TestCheckerHslPaint:
    def _check(self, tmp_path: Path, svg_text: str):
        from xgen_edit2docs.core.svg_quality_checker import SVGQualityChecker

        svg = tmp_path / "page.svg"
        svg.write_text(svg_text, encoding="utf-8")
        checker = SVGQualityChecker()
        return checker.check_file(str(svg))

    def test_hsl_fill_is_an_error(self, tmp_path: Path):
        result = self._check(
            tmp_path,
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="0" y="0" width="100" height="100" fill="hsl(210, 50%, 40%)"/>'
            "</svg>",
        )
        assert any("hsl()" in err for err in result["errors"])

    def test_hsl_in_inline_style_is_an_error(self, tmp_path: Path):
        result = self._check(
            tmp_path,
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="0" y="0" width="100" height="100" style="fill: hsl(10,10%,10%)"/>'
            "</svg>",
        )
        assert any("hsl()" in err for err in result["errors"])

    def test_plain_hex_is_clean(self, tmp_path: Path):
        result = self._check(
            tmp_path,
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '<rect x="0" y="0" width="100" height="100" fill="#336699"/>'
            "</svg>",
        )
        assert not any("hsl" in err for err in result["errors"])


# ---------------------------------------------------------------------------
# Letter spacing
# ---------------------------------------------------------------------------

class TestLetterSpacing:
    def test_parse_px_pt_em(self):
        assert _parse_letter_spacing_px("2px", font_size=16.0) == 2.0
        assert abs(_parse_letter_spacing_px("3pt", font_size=16.0) - 4.0) < 1e-9
        assert _parse_letter_spacing_px("0.5em", font_size=20.0) == 10.0
        assert _parse_letter_spacing_px("normal", font_size=16.0) == 0.0

    def test_width_estimate_includes_tracking(self):
        base_run = {"text": "TRACKING", "font_size": 16.0, "font_weight": "400"}
        spaced_run = {**base_run, "letter_spacing": 2.0}
        base = _estimate_run_text_width(base_run)
        spaced = _estimate_run_text_width(spaced_run)
        # 7 inter-character gaps x 2px
        assert abs((spaced - base) - 2.0 * (len("TRACKING") - 1)) < 1e-9

    def test_run_xml_emits_spc_attribute(self):
        run = {
            "text": "Spaced",
            "fill": "111111",
            "fill_raw": "#111111",
            "font_size": 16.0,
            "font_weight": "400",
            "letter_spacing": 2.0,
        }
        xml = _build_run_xml(run, {"latin": "Arial", "ea": "Arial"})
        # 2px letter-spacing = 150 hundredths-of-a-point
        assert ' spc="150"' in xml

    def test_run_xml_no_spc_without_letter_spacing(self):
        run = {
            "text": "Plain",
            "fill": "111111",
            "fill_raw": "#111111",
            "font_size": 16.0,
            "font_weight": "400",
        }
        xml = _build_run_xml(run, {"latin": "Arial", "ea": "Arial"})
        assert " spc=" not in xml
