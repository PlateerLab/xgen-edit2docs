"""Tests for the converter-parity layer of `check_svg_quality`.

This is the upstream defense against the production failure class
where the Executor emitted an SVG with an unresolvable `<use>` that
slipped past quality, slipped past the expanders, and crashed the
DrawingML converter — taking the whole deck down. With these checks
the quality stage flags the exact same elements the converter would
reject, with a machine-readable code, so the per-page retry loop in
`generate_deck` can hand the model targeted correction feedback
*before* it ever reaches export.
"""

from __future__ import annotations

from xgen_edit2docs.tools.quality import check_svg_quality, QualityCheckRequest, QualitySlide


def _resp(svg: str):
    return check_svg_quality(
        QualityCheckRequest(
            slides=[QualitySlide(index=0, name="slide_00", svg=svg)],
            canvas_format="ppt169",
        )
    )


def _codes(resp) -> set[str]:
    return {i.code for i in resp.issues if i.severity == "error"}


def test_valid_primitive_svg_passes():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" '
        'viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#fff"/>'
        '<text x="100" y="100" font-size="40" fill="#000" '
        'font-family="Pretendard, &quot;Malgun Gothic&quot;, sans-serif">정상</text>'
        '</svg>'
    )
    resp = _resp(svg)
    assert resp.passed, [i.message for i in resp.issues if i.severity == "error"]


def test_unresolvable_data_icon_flagged_specifically():
    """The Executor emitted <use data-icon="library/x"/> but library/x
    doesn't exist on disk. Must flag with the specific icon name."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<use data-icon="nonexistent-lib/never-existed" x="10" y="10" width="48" height="48"/>'
        '</svg>'
    )
    resp = _resp(svg)
    assert not resp.passed
    assert "forbidden_use_data_icon" in _codes(resp)
    # The icon name leaks into the message so the retry hint is actionable.
    msgs = [i.message for i in resp.issues if i.code == "forbidden_use_data_icon"]
    assert any("nonexistent-lib/never-existed" in m for m in msgs)


def test_unresolvable_href_use_flagged():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<use href="#missing-id"/>'
        '</svg>'
    )
    resp = _resp(svg)
    assert not resp.passed
    assert "forbidden_use_href" in _codes(resp)
    msgs = [i.message for i in resp.issues if i.code == "forbidden_use_href"]
    assert any("missing-id" in m for m in msgs)


def test_resolvable_href_use_passes():
    """When the <use href> points to an id that exists in the same SVG,
    the expander inlines it and the page passes."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<defs><circle id="dot" cx="0" cy="0" r="5" fill="red"/></defs>'
        '<use href="#dot" x="10" y="10"/>'
        '</svg>'
    )
    resp = _resp(svg)
    error_codes = _codes(resp)
    # forbidden_use_href must NOT be present — the reference resolved.
    assert "forbidden_use_href" not in error_codes


def test_foreign_object_flagged():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<foreignObject width="100" height="100">'
        '<div xmlns="http://www.w3.org/1999/xhtml">x</div>'
        '</foreignObject>'
        '</svg>'
    )
    resp = _resp(svg)
    assert "forbidden_foreign_object" in _codes(resp)


def test_script_flagged():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">'
        '<script>alert(1)</script>'
        '</svg>'
    )
    resp = _resp(svg)
    assert "forbidden_script" in _codes(resp)


def test_malformed_xml_caught():
    """Garbage in → quality error out, not a crash."""
    svg = '<svg><unclosed>'
    resp = _resp(svg)
    assert "malformed_xml" in _codes(resp)
