"""Unit tests for core.svg_to_pptx.svg_scale.scale_svg_to_viewbox."""

from __future__ import annotations

from xml.etree import ElementTree as ET

from xgen_edit2docs.core.svg_to_pptx.svg_scale import scale_svg_to_viewbox

SVG_NS = "http://www.w3.org/2000/svg"


def _svg(viewbox: str, body: str = '<rect x="0" y="0" width="10" height="10"/>') -> str:
    return f'<svg xmlns="{SVG_NS}" viewBox="{viewbox}">{body}</svg>'


class TestScaleSvgToViewbox:
    def test_exact_match_passes_through(self):
        svg = _svg("0 0 1280 720")
        assert scale_svg_to_viewbox(svg, 1280, 720) == svg

    def test_downscales_16_9_to_host_dimensions(self):
        # ppt169 canonical -> legacy small 16:9 deck (960x540 px).
        out = scale_svg_to_viewbox(_svg("0 0 1280 720"), 960, 540)
        root = ET.fromstring(out)
        assert root.get("viewBox") == "0 0 960 540"
        wrapper = root.find(f"{{{SVG_NS}}}g")
        assert wrapper is not None
        assert "scale(0.75, 0.75)" in wrapper.get("transform", "")

    def test_upscales_4_3_to_host_dimensions(self):
        # ppt43 canonical (1024x768) -> standard 4:3 deck (960x720 px).
        out = scale_svg_to_viewbox(_svg("0 0 1024 768"), 960, 720)
        root = ET.fromstring(out)
        assert root.get("viewBox") == "0 0 960 720"
        wrapper = root.find(f"{{{SVG_NS}}}g")
        assert wrapper is not None
        assert "scale(0.9375, 0.9375)" in wrapper.get("transform", "")

    def test_aspect_mismatch_passes_through(self):
        svg = _svg("0 0 1024 768")
        assert scale_svg_to_viewbox(svg, 1280, 720) == svg

    def test_defs_stay_outside_the_scale_wrapper(self):
        body = '<defs><linearGradient id="g"/></defs><rect width="10" height="10"/>'
        out = scale_svg_to_viewbox(_svg("0 0 1280 720", body), 960, 540)
        root = ET.fromstring(out)
        top_level_tags = [c.tag.split("}", 1)[-1] for c in root]
        assert "defs" in top_level_tags
        wrapper = root.find(f"{{{SVG_NS}}}g")
        assert wrapper is not None
        wrapped_tags = [c.tag.split("}", 1)[-1] for c in wrapper]
        assert wrapped_tags == ["rect"]

    def test_garbage_input_passes_through(self):
        assert scale_svg_to_viewbox("not svg at all", 1280, 720) == "not svg at all"
        no_viewbox = f'<svg xmlns="{SVG_NS}"><rect/></svg>'
        assert scale_svg_to_viewbox(no_viewbox, 1280, 720) == no_viewbox
