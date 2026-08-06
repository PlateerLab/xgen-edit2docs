"""Tests for P2.4 — promote unfixed layout violations into quality
errors so the retry loop can target them, with coord-aware retry
hints.

Background: PR #33 added the `layout_repair` pass that reports
violations as page-level warnings, with `fix_applied: bool` telling
whether the SVG was actually mutated. Auto-fixed violations are
informational only (the operator sees what we did); unfixed ones
need a retry — the model has to re-emit the page with proper
coordinates. This PR wires the two:

* `_promote_layout_violations` walks page warnings, picks the
  `layout_*` ones with `fix_applied=False`, and adds them to the
  `quality_resp.issues` list as severity=error so the retry loop
  fires.
* `_format_layout_violation_message` embeds the measured bbox /
  required width / overlap ratio into the message so the retry hint
  speaks the same numbers the model used.
* `_RETRY_HINTS` learns the `layout_*` codes so the hint builder
  picks the actionable directive instead of the generic footer.
"""

from __future__ import annotations

from dataclasses import dataclass

from xgen_edit2docs.tools.generate_deck import (
    _build_retry_hint,
    _format_layout_violation_message,
    _promote_layout_violations,
    _RETRY_HINTS,
)


# ---------------------------------------------------------------------------
# Hints catalog
# ---------------------------------------------------------------------------


def test_layout_hint_codes_registered():
    """Every layout_* code we surface must have an entry in
    _RETRY_HINTS or the retry-hint builder falls back to the generic
    footer and the model gets no targeted feedback."""
    for code in (
        "layout_overlap",
        "layout_text_overflow_x",
        "layout_off_canvas",
        "layout_empty_decoration",
    ):
        assert code in _RETRY_HINTS, f"missing {code}"


# ---------------------------------------------------------------------------
# Message formatting (embeds measured coords)
# ---------------------------------------------------------------------------


def test_overlap_message_quotes_actual_bboxes_and_target_y():
    detail = {
        "actual": {
            "small_bbox": (62, 461, 310, 35),
            "big_bbox": (42, 243, 270, 352),
            "overlap_ratio": 0.81,
        },
        "fix_applied": False,
    }
    msg = _format_layout_violation_message("layout_overlap", detail)
    assert "81%" in msg
    assert "x=62" in msg
    assert "x=42" in msg
    # Target y position the model should aim for (big.y + big.h + 8).
    assert str(243 + 352 + 8) in msg


def test_text_overflow_message_quotes_required_and_box_width():
    detail = {
        "actual": {
            "required_w": 167,
            "box_w": 80,
            "text": "CHAPTER 01 · THE NUMBERS",
        },
        "fix_applied": False,
    }
    msg = _format_layout_violation_message("layout_text_overflow_x", detail)
    assert "167" in msg
    assert "80" in msg
    assert "CHAPTER 01" in msg


def test_off_canvas_message_quotes_bbox_and_safe_area():
    detail = {
        "actual": {"bbox": (1240, 100, 100, 50)},
        "fix_applied": False,
    }
    msg = _format_layout_violation_message("layout_off_canvas", detail)
    assert "1240" in msg
    assert "40" in msg and ("1240" in msg or "680" in msg)


def test_unknown_code_falls_back_to_human_readable():
    """Codes we don't recognise yield a sensible default rather than
    a Python repr or crash."""
    msg = _format_layout_violation_message("layout_unknown", {})
    assert "layout" in msg.lower()


# ---------------------------------------------------------------------------
# Promotion: warnings → quality errors
# ---------------------------------------------------------------------------


@dataclass
class _StubWarning:
    code: str
    message: str = ""
    detail: dict | None = None


@dataclass
class _StubPage:
    page_index: int
    warnings: list[_StubWarning]


@dataclass
class _StubIssue:
    page_index: int | None
    severity: str
    code: str
    message: str
    location: str | None


class _StubQualityResp:
    def __init__(self):
        self.issues: list = []
        self.passed: bool = True


def test_unfixed_violation_promoted_to_quality_error():
    """An unfixed layout violation should turn into a severity=error
    QualityIssue so the retry loop picks it up."""
    page = _StubPage(
        page_index=3,
        warnings=[
            _StubWarning(
                code="layout_overlap",
                message="overlap detected",
                detail={
                    "actual": {
                        "small_bbox": (10, 10, 20, 20),
                        "big_bbox": (5, 5, 100, 100),
                        "overlap_ratio": 0.6,
                    },
                    "fix_applied": False,
                },
            )
        ],
    )
    resp = _StubQualityResp()
    _promote_layout_violations({3: page}, resp)
    assert any(i.severity == "error" and i.code == "layout_overlap" for i in resp.issues)
    assert resp.passed is False


def test_fixed_violation_not_promoted():
    """Auto-fixed violations stay as warnings (informational)."""
    page = _StubPage(
        page_index=3,
        warnings=[
            _StubWarning(
                code="layout_overlap",
                message="overlap detected",
                detail={"actual": {}, "fix_applied": True},
            )
        ],
    )
    resp = _StubQualityResp()
    _promote_layout_violations({3: page}, resp)
    assert resp.issues == []
    assert resp.passed is True


def test_non_layout_warnings_not_promoted():
    """Only `layout_*` codes promote — other warnings (palette, font
    discipline) stay as warnings and don't drive retry."""
    page = _StubPage(
        page_index=0,
        warnings=[
            _StubWarning(
                code="style_palette_too_large",
                message="palette inflation",
                detail={"colors_count": 16},
            ),
            _StubWarning(code="unfenced_svg", message="x"),
        ],
    )
    resp = _StubQualityResp()
    _promote_layout_violations({0: page}, resp)
    assert resp.issues == []


# ---------------------------------------------------------------------------
# End-to-end: retry hint quotes the measured coords
# ---------------------------------------------------------------------------


def test_retry_hint_includes_layout_directive_and_coords():
    """When a layout_overlap error fires, the retry hint must include
    BOTH the directive from _RETRY_HINTS AND the measured numbers
    that came in via the message."""
    detail = {
        "actual": {
            "small_bbox": (62, 461, 310, 35),
            "big_bbox": (42, 243, 270, 352),
            "overlap_ratio": 0.81,
        },
        "fix_applied": False,
    }
    msg = _format_layout_violation_message("layout_overlap", detail)
    issue = _StubIssue(
        page_index=3,
        severity="error",
        code="layout_overlap",
        message=msg,
        location="slide_03",
    )
    hint = _build_retry_hint([issue])
    # Directive present (from _RETRY_HINTS).
    assert "Two visible boxes overlap" in hint or "겹침" in hint
    # The hint contains the numbers (via message fallback in
    # `_build_retry_hint` → "Additional quality errors to fix").
    assert "81%" in hint
    assert "603" in hint  # 243 + 352 + 8
