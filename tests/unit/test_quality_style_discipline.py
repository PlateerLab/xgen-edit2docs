"""Per-slide stylistic-drift checks in the quality stage.

Two warnings — palette inflation and font-family proliferation —
that don't gate the build but tell the operator the LLM is drifting
away from the spec_lock conventions. Errors trigger retry; these are
warnings only.
"""

from __future__ import annotations

from xgen_edit2docs.tools.quality import (
    QualityCheckRequest,
    QualitySlide,
    check_svg_quality,
    _style_discipline_issues,
)


def _resp(svg: str):
    return check_svg_quality(
        QualityCheckRequest(
            slides=[QualitySlide(index=0, name="slide_00", svg=svg)],
        )
    )


def _codes(resp) -> list[str]:
    return [i.code for i in resp.issues]


def test_within_threshold_no_warning():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">'
        '<rect width="1280" height="720" fill="#0A1628"/>'
        '<text x="10" y="100" fill="#E6EDF3" font-family="Pretendard">제목</text>'
        '<text x="10" y="200" fill="#00E5FF" font-family="Pretendard">강조</text>'
        '<text x="10" y="300" fill="#9AA3B8" font-family="Malgun Gothic">본문</text>'
        '</svg>'
    )
    resp = _resp(svg)
    assert "style_palette_too_large" not in _codes(resp)
    assert "style_font_diversity_high" not in _codes(resp)


def test_palette_inflation_warned():
    """16 hex colors on one slide → palette warning (threshold > 14)."""
    colors = [f"#{i:02x}0000" for i in range(0x10, 0x80, 0x07)]  # 16 reds
    rects = "".join(
        f'<rect x="0" y="{i*10}" width="10" height="10" fill="{c}"/>'
        for i, c in enumerate(colors)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">'
        f'{rects}'
        '</svg>'
    )
    resp = _resp(svg)
    assert "style_palette_too_large" in _codes(resp)


def test_disciplined_10_color_palette_does_not_warn():
    """A real disciplined deck (deck_3.pptx slide 2) uses 10 colors:
    1 bg + 4 layering variants + 3 grays + 2 accents. Must not flag."""
    colors = ["#00D9FF", "#0A0E1A", "#141A2E", "#1C2440", "#2A3454",
              "#6B7280", "#7B61FF", "#9CA3AF", "#F5F7FA", "#FF6B35"]
    rects = "".join(
        f'<rect x="0" y="{i*10}" width="10" height="10" fill="{c}"/>'
        for i, c in enumerate(colors)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">'
        f'{rects}'
        '</svg>'
    )
    resp = _resp(svg)
    assert "style_palette_too_large" not in _codes(resp)


def test_short_hex_counted_alongside_long_hex():
    """`#abc` is the same color as `#aabbcc` — count them once."""
    issues = _style_discipline_issues(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect fill="#abc"/><rect fill="#AABBCC"/>'
        '</svg>'
    )
    palette_issues = [i for i in issues if i[0] == "style_palette_too_large"]
    # Only 1 unique color; under threshold.
    assert palette_issues == []


def test_font_diversity_warned_when_above_three():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text font-family="Pretendard">a</text>'
        '<text font-family="Malgun Gothic">b</text>'
        '<text font-family="Arial Black">c</text>'
        '<text font-family="Nanum Myeongjo">d</text>'
        '<text font-family="D2 Coding">e</text>'
        '</svg>'
    )
    resp = _resp(svg)
    assert "style_font_diversity_high" in _codes(resp)


def test_font_diversity_counts_first_in_stack_only():
    """When two stacks have the same lead family, count one."""
    issues = _style_discipline_issues(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<text font-family="Pretendard, Apple SD Gothic Neo, sans-serif">a</text>'
        '<text font-family="Pretendard, Malgun Gothic">b</text>'
        '<text font-family="Arial, Times">c</text>'
        '</svg>'
    )
    diversity_issues = [i for i in issues if i[0] == "style_font_diversity_high"]
    # Two unique lead families (Pretendard, Arial); under threshold.
    assert diversity_issues == []


def test_palette_and_font_can_both_fire():
    """Both warnings independent; one slide can carry both."""
    rects = "".join(
        f'<rect fill="#{i:06x}"/>' for i in range(0x100000, 0x100010)
    )
    texts = "".join(
        f'<text font-family="Font{i}">x</text>' for i in range(6)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg">{rects}{texts}</svg>'
    )
    resp = _resp(svg)
    codes = _codes(resp)
    assert "style_palette_too_large" in codes
    assert "style_font_diversity_high" in codes


def test_warnings_dont_fail_quality_pass():
    """A deck with only style warnings should still pass quality (no
    errors). Retry must NOT trigger on stylistic drift."""
    rects = "".join(
        f'<rect fill="#{i:06x}"/>' for i in range(0x100000, 0x100010)
    )
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 1280 720" width="1280" height="720">'
        + rects
        + '</svg>'
    )
    resp = _resp(svg)
    assert resp.passed is True, [(i.severity, i.code) for i in resp.issues]
    assert any(i.severity == "warning" for i in resp.issues)
