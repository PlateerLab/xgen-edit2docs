"""Color quantization at the Executor boundary.

The LLM tends to invent slight variants of declared palette colors —
a tweaked shadow, an alpha-blended overlay flattened to a near-but-
not-identical hex. Each variant inflates the per-slide palette
beyond what spec_lock declared and breaks the deck's typography
discipline. The quantizer snaps each invented color to the nearest
spec_lock palette entry whenever the RGB distance is below threshold.

`deck_3.pptx` showed exactly this pattern: spec_lock declared ~5-6
colors but each slide rendered 9-12, with the extras being near
duplicates of the canonical palette.
"""

from __future__ import annotations

from xgen_edit2docs.tools.execute import (
    _hex_to_rgb,
    _palette_from_spec_lock,
    _quantize_colors_to_palette,
)


# ---------------------------------------------------------------------------
# Palette extraction
# ---------------------------------------------------------------------------


def test_palette_from_yaml_spec_lock():
    spec = """
    colors:
      primary: #0A1628
      accent: #00D9FF
      surface: #141A2E
    """
    palette = _palette_from_spec_lock(spec)
    hexes = [p[0] for p in palette]
    assert hexes == ["#0A1628", "#00D9FF", "#141A2E"]


def test_palette_from_markdown_spec_lock():
    spec = """
    ## colors
    - bg: #0A1628
    - accent: #00D9FF
    """
    palette = _palette_from_spec_lock(spec)
    assert ("#0A1628", (10, 22, 40)) in palette


def test_palette_deduplicates_preserving_order():
    """First occurrence wins. Later mentions of the same hex don't
    re-add to the palette."""
    spec = "primary: #ABCDEF\naccent: #abcdef\n"
    palette = _palette_from_spec_lock(spec)
    assert len(palette) == 1
    assert palette[0][0] == "#ABCDEF"


def test_palette_empty_spec_returns_empty():
    assert _palette_from_spec_lock("") == []
    assert _palette_from_spec_lock("no hex here") == []


# ---------------------------------------------------------------------------
# Hex conversion helpers
# ---------------------------------------------------------------------------


def test_hex_to_rgb_long():
    assert _hex_to_rgb("#FF8800") == (255, 136, 0)


def test_hex_to_rgb_short_expands():
    assert _hex_to_rgb("#F80") == (255, 136, 0)


def test_hex_to_rgb_malformed_returns_none():
    assert _hex_to_rgb("#zzz") is None
    assert _hex_to_rgb("#12") is None


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------


def test_near_duplicate_snaps_to_palette_color():
    """The LLM emits `#0a1628` (the declared color) plus `#0A1729`
    (one channel off by one). Both should map to the declared color
    in the final SVG."""
    spec = "primary: #0A1628\n"
    svg = (
        '<svg><rect fill="#0a1628"/><rect fill="#0a1729"/></svg>'
    )
    out = _quantize_colors_to_palette(svg, spec)
    # Both rects now use the canonical palette entry.
    assert out.count("#0A1628") == 2
    assert "#0a1729" not in out
    assert "#0A1729" not in out


def test_far_color_left_unchanged():
    """A genuinely different color (red vs blue palette) must not snap
    — the operator should see the drift."""
    spec = "primary: #0A1628\n"
    svg = '<svg><rect fill="#FF0000"/></svg>'
    out = _quantize_colors_to_palette(svg, spec)
    assert "#FF0000" in out


def test_quantization_chooses_nearest_palette_entry():
    """Two palette entries; an emitted color halfway between but
    closer to one of them snaps to the closer one."""
    spec = """
    colors:
      bg: #000000
      fg: #FFFFFF
    """
    svg = '<svg><rect fill="#181818"/></svg>'  # very close to #000000 (24/channel)
    out = _quantize_colors_to_palette(svg, spec)
    assert "#000000" in out
    assert "#181818" not in out


def test_no_palette_passes_through_unchanged():
    """When spec_lock has no hex colors, the SVG is returned as-is."""
    svg = '<svg><rect fill="#ABCDEF"/></svg>'
    out = _quantize_colors_to_palette(svg, "no colors declared")
    assert out == svg


def test_empty_inputs_pass_through():
    assert _quantize_colors_to_palette("", "primary: #000") == ""
    assert _quantize_colors_to_palette("<svg/>", "") == "<svg/>"


def test_real_world_deck_3_dark_blue_cluster():
    """deck_3.pptx slide 2 emitted #0A0E1A / #141A2E / #1C2440 /
    #2A3454 as dark-blue layering variants. If spec_lock declared
    #0A0E1A and #2A3454 as the canonical pair, the middle two should
    snap to one of them (probably #141A2E → #0A0E1A and
    #1C2440 → #2A3454)."""
    spec = """
    colors:
      bg: #0A0E1A
      surface_2: #2A3454
    """
    svg = (
        '<svg>'
        '<rect fill="#0A0E1A"/>'
        '<rect fill="#141A2E"/>'  # closer to bg #0A0E1A
        '<rect fill="#1C2440"/>'  # closer to surface_2 #2A3454
        '<rect fill="#2A3454"/>'
        '</svg>'
    )
    out = _quantize_colors_to_palette(svg, spec)
    # The intermediate variants are gone; only the canonical pair
    # remains in the document.
    assert "#141A2E" not in out
    assert "#1C2440" not in out
    assert out.count("#0A0E1A") + out.count("#2A3454") == 4
