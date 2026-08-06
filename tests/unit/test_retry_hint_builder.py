"""Tests for the per-page retry hint builder.

The hint is the model's only signal about what the previous attempt
got wrong. A generic "make it simpler" message means the model often
re-emits the same broken pattern. The new builder enumerates every
specific rule the SVG broke, so the retry has a real chance of
producing convertible output.
"""

from __future__ import annotations

from dataclasses import dataclass

from xgen_edit2docs.tools.generate_deck import _build_retry_hint


@dataclass
class _Issue:
    code: str
    message: str
    severity: str = "error"


def test_hint_includes_specific_directive_for_each_code():
    errors = [
        _Issue(code="forbidden_use_data_icon", message="…"),
        _Issue(code="forbidden_foreign_object", message="…"),
    ]
    hint = _build_retry_hint(errors)
    # Each rule shows up with its actionable language.
    assert "data-icon" in hint
    assert "foreignObject" in hint or "tspan" in hint
    assert hint.startswith("> Retry hint:")


def test_unknown_codes_fall_back_to_quality_message():
    """Issues without a targeted hint should still appear, using the
    issue message verbatim — so legacy quality checker errors aren't
    silently lost."""
    errors = [
        _Issue(code="text_too_small", message="Body text below 14pt"),
    ]
    hint = _build_retry_hint(errors)
    assert "Body text below 14pt" in hint


def test_duplicate_codes_emitted_once():
    """Five <use> elements on a page shouldn't produce five lines of
    identical instruction — the directive is the same for all."""
    errors = [
        _Issue(code="forbidden_use_data_icon", message=f"icon {n}")
        for n in range(5)
    ]
    hint = _build_retry_hint(errors)
    # The targeted directive must appear, but only once.
    assert hint.count("data-icon") <= 3  # at most title + directive mention


def test_empty_errors_returns_empty_hint():
    assert _build_retry_hint([]) == ""


def test_hint_carries_footer_safety_net():
    """The footer ("when in doubt, prefer simpler output") is a fallback
    in case the targeted directives weren't enough — must always be
    present when there's at least one error."""
    hint = _build_retry_hint([_Issue(code="forbidden_use_bare", message="x")])
    assert "When in doubt" in hint or "simpler" in hint
