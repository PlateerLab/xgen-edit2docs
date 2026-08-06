"""Deterministic layout repair tests.

Each test corresponds to a real failure mode observed in deck_2.pptx.
The repair pass runs immediately after the Executor's SVG is normalised
(auto-id / image href / weight strip done) and before the SVG flows
into quality / convert / export. Its job is to catch the layout
problems the LLM consistently produces despite prompting — overlapping
hero/caption boxes, footer text spilling out of its container, shapes
drifting off-canvas, leftover empty decoration boxes.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from xgen_edit2docs.core.svg_to_pptx.layout_repair import (
    DEFAULT_CANVAS,
    LayoutViolation,
    repair_layout,
)


SVG = "http://www.w3.org/2000/svg"


def _kinds(result) -> list[str]:
    return [v.kind for v in result.violations]


def _parse(svg: str) -> ET.Element:
    return ET.fromstring(svg)


# ---------------------------------------------------------------------------
# Overlap (caption inside hero)
# ---------------------------------------------------------------------------


def test_caption_inside_hero_number_gets_shifted_below():
    """Reproduces deck_2.pptx slide 3: a 165pt `41` at (42, 243, 270×352)
    with two captions at y=461 / 491 (both well inside the hero box).
    The repair pass shifts the smaller boxes below the hero's bottom."""
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720" viewBox="0 0 1280 720">
      <rect width="1280" height="720" fill="#fff"/>
      <text id="hero_41" x="42" y="540" font-size="220" font-weight="900">41</text>
      <text id="cap_a"   x="62" y="475" font-size="22" font-weight="400">GitHub 저장소 분석 기준</text>
      <text id="cap_b"   x="62" y="505" font-size="22" font-weight="400">Copilot이 작성한 코드 비중</text>
    </svg>"""
    result = repair_layout(svg)
    assert "overlap" in _kinds(result)
    # Both captions ought to have been moved below the hero's bottom.
    # We don't pin the exact y but assert it landed past the hero box
    # bottom (220×0.85 = ~187 above y=540 → top ~353, bottom ~593).
    root = _parse(result.repaired_svg)
    captions = [t for t in root.iter(f"{{{SVG}}}text") if t.get("id", "").startswith("cap_")]
    for c in captions:
        # text `y` is the baseline; we shifted to land below ~593 + the
        # 8px gap baked into the repair pass.
        assert float(c.get("y")) > 540, f"caption {c.get('id')} didn't shift"


def test_intentional_containment_is_not_flagged_as_overlap():
    """A foreground text label SITTING on a background card is the
    canonical SVG layering pattern; it must not register as an overlap
    even though one box fully contains the other."""
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <rect id="card" x="100" y="100" width="600" height="400" fill="#e0e0ff"/>
      <text id="label" x="120" y="160" font-size="40">제목</text>
    </svg>"""
    result = repair_layout(svg)
    assert "overlap" not in _kinds(result)


# ---------------------------------------------------------------------------
# Off-canvas
# ---------------------------------------------------------------------------


def test_offcanvas_right_is_clamped():
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <rect id="strip" x="1240" y="100" width="100" height="50" fill="#888"/>
    </svg>"""
    result = repair_layout(svg)
    assert "off_canvas" in _kinds(result)
    root = _parse(result.repaired_svg)
    strip = root.find(f".//{{{SVG}}}rect[@id='strip']")
    assert strip is not None
    new_right = float(strip.get("x")) + float(strip.get("width"))
    assert new_right <= 1280


def test_offcanvas_oversize_gets_clamped_to_canvas():
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <rect id="huge" x="0" y="0" width="2000" height="2000" fill="#000"/>
    </svg>"""
    result = repair_layout(svg)
    # The slide background and the `huge` rect both fit / overflow;
    # at least one off_canvas should fire and the bounds should land
    # back inside.
    root = _parse(result.repaired_svg)
    huge = root.find(f".//{{{SVG}}}rect[@id='huge']")
    assert huge is not None
    assert int(huge.get("width")) <= 1280
    assert int(huge.get("height")) <= 720


# ---------------------------------------------------------------------------
# Empty decoration
# ---------------------------------------------------------------------------


def test_empty_g_no_fill_no_children_gets_removed():
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <g id="ghost"></g>
      <text x="10" y="10">retained</text>
    </svg>"""
    result = repair_layout(svg)
    assert "empty_decoration" in _kinds(result)
    assert "ghost" not in result.repaired_svg
    assert "retained" in result.repaired_svg


def test_empty_rect_with_fill_is_kept():
    """A rect with a fill is intentional (background, accent strip); the
    pass must leave it alone."""
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#000"/>
      <rect id="accent" x="0" y="0" width="1280" height="4" fill="#00d9ff"/>
    </svg>"""
    result = repair_layout(svg)
    assert "empty_decoration" not in _kinds(result)
    assert "accent" in result.repaired_svg


# ---------------------------------------------------------------------------
# Text overflow (info-only — current detector doesn't always rewrite)
# ---------------------------------------------------------------------------


def test_text_in_too_narrow_group_box_surfaces_overflow():
    """deck_2.pptx chapter-label box: 170px holding 'CHAPTER 01 · THE
    NUMBERS' (24 chars at 12pt → ~190px). The detector must surface
    the overflow even when it can't always widen the box automatically."""
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <g id="chapter_label" data-x="60" data-y="40" data-w="80" data-h="20">
        <text x="60" y="55" font-size="12">CHAPTER 01 · THE NUMBERS</text>
      </g>
    </svg>"""
    result = repair_layout(svg)
    assert "text_overflow_x" in _kinds(result), [v.kind for v in result.violations]
    overflow = next(v for v in result.violations if v.kind == "text_overflow_x")
    assert overflow.actual["required_w"] > 80
    assert overflow.actual["box_w"] == 80


# ---------------------------------------------------------------------------
# Pass-through / safety
# ---------------------------------------------------------------------------


def test_clean_svg_has_no_violations():
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <text id="title" x="60" y="120" font-size="60">제목</text>
      <text id="body" x="60" y="300" font-size="20">본문</text>
    </svg>"""
    result = repair_layout(svg)
    assert result.violations == []
    # Content survives.
    assert "제목" in result.repaired_svg
    assert "본문" in result.repaired_svg


def test_malformed_svg_passes_through_unchanged():
    bad = "<svg><unclosed>"
    result = repair_layout(bad)
    assert result.repaired_svg == bad
    assert result.violations == []


def test_default_canvas_is_ppt_169():
    assert DEFAULT_CANVAS == (1280, 720)


def test_viewbox_overrides_default_canvas():
    """If the SVG declares its own viewBox, repair uses that instead of
    the default 1280×720 — needed for vertical / 4:3 decks."""
    svg = f"""<svg xmlns="{SVG}" viewBox="0 0 1080 1920" width="1080" height="1920">
      <rect width="1080" height="1920" fill="#fff"/>
      <rect id="ok" x="0" y="0" width="1080" height="1920" fill="#888"/>
    </svg>"""
    result = repair_layout(svg)
    # The fullbleed rect matches the canvas exactly; no clamp.
    assert "off_canvas" not in _kinds(result)


def test_violation_record_carries_fix_state():
    """Every violation reports whether the repair pass actually
    rewrote the SVG — callers (quality, retry hint) need this so they
    can decide whether to keep nudging the model."""
    svg = f"""<svg xmlns="{SVG}" width="1280" height="720">
      <rect width="1280" height="720" fill="#fff"/>
      <rect id="oob" x="1300" y="0" width="100" height="100" fill="#000"/>
    </svg>"""
    result = repair_layout(svg)
    assert all(isinstance(v, LayoutViolation) for v in result.violations)
    oob_v = next((v for v in result.violations if v.kind == "off_canvas"), None)
    assert oob_v is not None
    assert oob_v.fix_applied is True
