"""Upstream preview-fidelity ports — hslClr hue scale, pPr/defRPr inheritance,
tracked-text width (upstream 4fd3b39d, 03c0d320, 48b52140)."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _hsl_elem(hue: str, sat: str = "100000", lum: str = "50000") -> ET.Element:
    return ET.fromstring(
        f'<a:hslClr xmlns:a="{A_NS}" hue="{hue}" sat="{sat}" lum="{lum}"/>'
    )


class TestHslClrHueScale:
    """<a:hslClr> hue is in 1/60000 deg — 21_600_000 spans the full wheel."""

    def test_120_degrees_is_green(self):
        from xgen_edit2docs.core.pptx_to_svg.color_resolver import resolve_color

        hex_color, alpha = resolve_color(_hsl_elem(hue="7200000"), None)
        assert hex_color == "#00FF00"
        assert alpha == 1.0

    def test_240_degrees_is_blue(self):
        from xgen_edit2docs.core.pptx_to_svg.color_resolver import resolve_color

        hex_color, _ = resolve_color(_hsl_elem(hue="14400000"), None)
        assert hex_color == "#0000FF"

    def test_zero_hue_is_red(self):
        from xgen_edit2docs.core.pptx_to_svg.color_resolver import resolve_color

        hex_color, _ = resolve_color(_hsl_elem(hue="0"), None)
        assert hex_color == "#FF0000"


def _tx_body(inner: str) -> ET.Element:
    return ET.fromstring(
        f'<p:txBody xmlns:p="{P_NS}" xmlns:a="{A_NS}"><a:bodyPr/>{inner}</p:txBody>'
    )


class TestDefRPrInheritance:
    """Runs without rPr@sz must fall back to pPr/defRPr before the default."""

    def test_font_size_from_def_rpr(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import _parse_paragraphs

        body = _tx_body(
            "<a:p>"
            '<a:pPr><a:defRPr sz="3200"/></a:pPr>'
            "<a:r><a:t>Hello</a:t></a:r>"
            "</a:p>"
        )
        paras = _parse_paragraphs(body, None, {})
        assert paras[0].runs[0].font_size_px == pytest.approx(3200 / 100 * 4 / 3)

    def test_rpr_sz_beats_def_rpr(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import _parse_paragraphs

        body = _tx_body(
            "<a:p>"
            '<a:pPr><a:defRPr sz="3200"/></a:pPr>'
            '<a:r><a:rPr sz="1400"/><a:t>small</a:t></a:r>'
            "</a:p>"
        )
        paras = _parse_paragraphs(body, None, {})
        assert paras[0].runs[0].font_size_px == pytest.approx(1400 / 100 * 4 / 3)

    def test_bold_and_typeface_from_def_rpr(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import _parse_paragraphs

        body = _tx_body(
            "<a:p>"
            '<a:pPr><a:defRPr b="1"><a:latin typeface="Georgia"/></a:defRPr></a:pPr>'
            "<a:r><a:t>Hi</a:t></a:r>"
            "</a:p>"
        )
        run = _parse_paragraphs(body, None, {})[0].runs[0]
        assert run.bold is True
        assert "Georgia" in run.font_family

    def test_default_used_when_no_def_rpr(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import (
            DEFAULT_FONT_SIZE_PX,
            _parse_paragraphs,
        )

        body = _tx_body("<a:p><a:r><a:t>plain</a:t></a:r></a:p>")
        run = _parse_paragraphs(body, None, {})[0].runs[0]
        assert run.font_size_px == pytest.approx(DEFAULT_FONT_SIZE_PX)


class TestTrackedTextWidth:
    """Letter-spacing (rPr@spc) must widen estimated and advance widths."""

    def _run(self, spacing: float):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import TextRun

        return TextRun(
            text="tracked",
            font_size_px=24.0,
            font_family="sans-serif",
            fill="#000000",
            letter_spacing_px=spacing,
        )

    def test_estimate_run_width_includes_tracking(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import _estimate_run_width

        plain = _estimate_run_width("tracked", self._run(0.0))
        tracked = _estimate_run_width("tracked", self._run(5.0))
        # 6 inter-character gaps x 5px x 1.05 fudge
        assert tracked == pytest.approx(plain + 6 * 5.0 * 1.05)

    def test_advance_width_skips_tracking_on_first_char(self):
        from xgen_edit2docs.core.pptx_to_svg.txbody_to_svg import (
            _advance_width,
            _char_width,
        )

        run = self._run(5.0)
        base = _char_width("t", run.font_size_px, run.bold, run.font_family)
        assert _advance_width("t", 0, run) == pytest.approx(base)
        assert _advance_width("t", 3, run) == pytest.approx(base + 5.0)
