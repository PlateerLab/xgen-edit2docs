"""spec_lock validation tests.

The validator runs at the boundary where the Strategist's free-form
output hands off to the rest of the pipeline. It only rewrites things
that can be deterministically fixed — color hex shorthand, rgb()
syntax, icon name typos. Everything else surfaces as a
`ValidationWarning` for the operator.
"""

from __future__ import annotations

from pathlib import Path

from xgen_edit2docs.tools._spec_validator import (
    SpecLockValidation,
    ValidationWarning,
    validate_spec_lock,
)


REAL_ICONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "xgen_edit2docs" / "core" / "templates" / "icons"
)


def _codes(result: SpecLockValidation) -> set[str]:
    return {w.code for w in result.warnings}


# ---------------------------------------------------------------------------
# Hex normalisation
# ---------------------------------------------------------------------------


def test_short_hex_expanded_to_six_digits():
    spec = "primary: #abc\naccent: #DEF\n"
    out = validate_spec_lock(spec)
    assert "#AABBCC" in out.spec_lock
    assert "#DDEEFF" in out.spec_lock
    assert "spec_validator_hex_normalised" in _codes(out)


def test_six_digit_hex_uppercased():
    spec = "primary: #aabbcc\n"
    out = validate_spec_lock(spec)
    assert "#AABBCC" in out.spec_lock


def test_rgb_converted_to_hex():
    spec = "accent: rgb(10, 20, 30)\n"
    out = validate_spec_lock(spec)
    assert "#0A141E" in out.spec_lock
    assert "rgb(" not in out.spec_lock


def test_rgba_converted_to_hex_alpha_dropped():
    """rgba() drops the alpha — the converter can't represent it via
    a single hex value anyway. fill-opacity / alpha should be set on
    the element."""
    spec = "accent: rgba(255, 0, 0, 0.5)\n"
    out = validate_spec_lock(spec)
    assert "#FF0000" in out.spec_lock
    assert "rgba(" not in out.spec_lock


def test_no_change_for_clean_spec():
    spec = "primary: #0A1628\nbody_size: 20\n"
    out = validate_spec_lock(spec)
    assert out.spec_lock.count("#0A1628") == 1
    assert "spec_validator_hex_normalised" not in _codes(out)


# ---------------------------------------------------------------------------
# Icon resolution (uses the real bundled library)
# ---------------------------------------------------------------------------


def test_exact_icon_name_left_untouched():
    spec = "icons:\n  inventory: [chunk-filled/rocket, chunk-filled/bolt]\n"
    out = validate_spec_lock(spec, icons_dir=REAL_ICONS_DIR)
    assert "chunk-filled/rocket" in out.spec_lock
    assert "spec_validator_icon_substituted" not in _codes(out)


def test_invented_icon_name_substituted_with_closest():
    """`trending-up` doesn't exist in chunk-filled (it has
    `arrow-trend-up`). The validator should substitute."""
    spec = "icons:\n  inventory: [chunk-filled/trending-up]\n"
    out = validate_spec_lock(spec, icons_dir=REAL_ICONS_DIR)
    assert "chunk-filled/trending-up" not in out.spec_lock
    assert "chunk-filled/arrow-trend-up" in out.spec_lock or "trend" in out.spec_lock
    assert "spec_validator_icon_substituted" in _codes(out)


def test_no_icons_dir_passes_through():
    spec = "icons:\n  inventory: [chunk-filled/nonexistent]\n"
    out = validate_spec_lock(spec, icons_dir=None)
    # No substitution attempted, no warning.
    assert "chunk-filled/nonexistent" in out.spec_lock


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


def test_missing_canvas_section_warned():
    spec = "colors:\n  primary: #000000\ntypography:\n  body: 20\n"
    out = validate_spec_lock(spec)
    assert "spec_validator_missing_section" in _codes(out)


def test_all_sections_present_no_warning():
    spec = "canvas:\n  format: ppt169\ncolors:\n  primary: #000000\ntypography:\n  body: 20\n"
    out = validate_spec_lock(spec)
    assert "spec_validator_missing_section" not in _codes(out)


# ---------------------------------------------------------------------------
# Windows-safe font tails
# ---------------------------------------------------------------------------


def test_windows_safe_tail_passes():
    spec = (
        'typography:\n'
        '  font_stack: "Pretendard, Apple SD Gothic Neo, Malgun Gothic, sans-serif"\n'
    )
    out = validate_spec_lock(spec)
    assert "spec_validator_font_tail_unsafe" not in _codes(out)


def test_unsafe_tail_warned():
    spec = (
        'typography:\n'
        '  font_stack: "Pretendard, Inter, sans-serif"\n'
    )
    out = validate_spec_lock(spec)
    assert "spec_validator_font_tail_unsafe" in _codes(out)


def test_generic_only_stack_is_skipped():
    """A stack that's purely generic (`sans-serif`) has no concrete tail
    to validate — skip silently."""
    spec = "typography:\n  font_stack: sans-serif\n"
    out = validate_spec_lock(spec)
    assert "spec_validator_font_tail_unsafe" not in _codes(out)


# ---------------------------------------------------------------------------
# Pass-through behaviour
# ---------------------------------------------------------------------------


def test_empty_spec_returns_empty():
    out = validate_spec_lock("")
    assert out.spec_lock == ""
    assert out.warnings == []


def test_validation_warning_dataclass_shape():
    spec = "primary: #abc\n"
    out = validate_spec_lock(spec)
    assert all(isinstance(w, ValidationWarning) for w in out.warnings)
    for w in out.warnings:
        assert w.code and w.message and isinstance(w.detail, dict)
