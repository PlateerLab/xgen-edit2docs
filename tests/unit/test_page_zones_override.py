"""Tests for P2.2 — Strategist-declared `page_zones` override the
rhythm-based defaults in the layout brief.

Three coordinated paths:
1. The Strategist prompt asks for `page_zones` (see
   `test_prompt_page_zones_instructions_present`).
2. `_parse_zones_from_spec_lock` reads the YAML.
3. `build_layout_briefs` merges declared zones over rhythm defaults
   per-role.
4. `_spec_validator._check_page_zones` flags off-canvas /
   too-narrow page-number / title-chapter y overlap.
"""

from __future__ import annotations

from xgen_edit2docs.tools._layout_brief import (
    Zone,
    _merge_zones,
    _parse_zones_from_spec_lock,
    build_layout_briefs,
)


# ---------------------------------------------------------------------------
# Prompt instruction
# ---------------------------------------------------------------------------


def test_prompt_page_zones_instructions_present():
    from xgen_edit2docs.llm import load_prompt
    text = load_prompt("strategist")
    assert "page_zones" in text
    # The format example is in the prompt so the model knows the schema.
    assert "{ x:" in text
    assert "page_number" in text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_basic_yaml_page_zones():
    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    title: { x: 60, y: 100, w: 1180, h: 120 }\n"
        "    hero: { x: 60, y: 240, w: 1180, h: 300 }\n"
    )
    out = _parse_zones_from_spec_lock(spec)
    assert 1 in out
    roles = {z.role: z for z in out[1]}
    assert roles["title"].x == 60
    assert roles["title"].y == 100
    assert roles["hero"].h == 300


def test_parse_two_digit_pid_supported():
    spec = (
        "page_zones:\n"
        "  P10:\n"
        "    hero: { x: 60, y: 100, w: 1180, h: 500 }\n"
    )
    out = _parse_zones_from_spec_lock(spec)
    assert 10 in out


def test_parse_invalid_bbox_skipped():
    """Bbox missing a required key → silently skipped, doesn't poison
    the rest of the zone list."""
    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    title: { x: 60, y: 100, w: 1180 }\n"  # missing h
        "    hero: { x: 60, y: 240, w: 1180, h: 300 }\n"
    )
    out = _parse_zones_from_spec_lock(spec)
    roles = {z.role for z in out[1]}
    assert roles == {"hero"}


def test_parse_off_canvas_clamped():
    """Strategist emitted a bbox extending past canvas — the parser
    clamps to fit so downstream stages don't get poisoned."""
    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    hero: { x: 1100, y: 100, w: 400, h: 700 }\n"
    )
    out = _parse_zones_from_spec_lock(spec)
    z = next(z for z in out[1] if z.role == "hero")
    assert z.x + z.w <= 1280
    assert z.y + z.h <= 720


def test_parse_missing_page_zones_returns_empty():
    assert _parse_zones_from_spec_lock("colors:\n  primary: '#000'") == {}


def test_parse_malformed_yaml_returns_empty():
    assert _parse_zones_from_spec_lock("page_zones: {{}}") == {}


# ---------------------------------------------------------------------------
# Merge semantics
# ---------------------------------------------------------------------------


def test_declared_role_overrides_default():
    defaults = [
        Zone(role="title", x=0, y=0, w=100, h=20),
        Zone(role="footer", x=0, y=700, w=1280, h=20),
    ]
    declared = [Zone(role="title", x=60, y=100, w=1180, h=120)]
    merged = _merge_zones(defaults, declared)
    roles = {z.role: z for z in merged}
    assert roles["title"].x == 60  # declared wins
    assert roles["title"].w == 1180
    assert roles["footer"].y == 700  # default preserved


def test_declared_only_role_appended():
    """A role the Strategist invented (not in defaults) gets
    appended."""
    defaults = [Zone(role="title", x=0, y=0, w=100, h=20)]
    declared = [Zone(role="legend", x=900, y=600, w=300, h=80)]
    merged = _merge_zones(defaults, declared)
    assert any(z.role == "legend" for z in merged)
    assert any(z.role == "title" for z in merged)


# ---------------------------------------------------------------------------
# End-to-end: build_layout_briefs prefers declared zones
# ---------------------------------------------------------------------------


def test_build_briefs_uses_declared_zones_when_present():
    spec = (
        "page_rhythm:\n"
        "  P01: anchor\n"
        "page_zones:\n"
        "  P01:\n"
        "    page_number: { x: 1080, y: 670, w: 180, h: 35 }\n"
        "    title: { x: 80, y: 120, w: 1120, h: 100 }\n"
    )
    briefs = build_layout_briefs(spec_lock=spec, page_count=1)
    roles = {z.role: z for z in briefs[0].zones}
    # Strategist-declared title wins over the anchor-default title.
    assert roles["title"].x == 80
    assert roles["title"].w == 1120
    # Page-number override — wider than the canonical 140.
    assert roles["page_number"].w == 180
    # Roles the Strategist didn't declare keep the rhythm defaults.
    assert "chapter_label" in roles


def test_build_briefs_falls_back_to_rhythm_when_no_zones():
    """No page_zones declared → rhythm-based defaults only."""
    briefs = build_layout_briefs(
        spec_lock="page_rhythm:\n  P01: dense\n",
        page_count=1,
    )
    roles = {z.role for z in briefs[0].zones}
    assert "title" in roles
    assert "body" in roles
    assert "page_number" in roles


# ---------------------------------------------------------------------------
# Spec validator
# ---------------------------------------------------------------------------


def test_validator_flags_off_canvas_zone():
    from xgen_edit2docs.tools._spec_validator import validate_spec_lock

    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    hero: { x: 1100, y: 100, w: 500, h: 600 }\n"
    )
    result = validate_spec_lock(spec)
    codes = [w.code for w in result.warnings]
    assert "spec_validator_page_zones" in codes
    detail = next(w for w in result.warnings if w.code == "spec_validator_page_zones").detail
    issues = {o["issue"] for o in detail["offenders"]}
    assert "off_canvas" in issues


def test_validator_flags_page_number_too_narrow():
    from xgen_edit2docs.tools._spec_validator import validate_spec_lock

    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    page_number: { x: 1200, y: 690, w: 60, h: 20 }\n"
    )
    result = validate_spec_lock(spec)
    detail = next(
        w for w in result.warnings if w.code == "spec_validator_page_zones"
    ).detail
    issues = {o["issue"] for o in detail["offenders"]}
    assert "page_number_too_narrow" in issues


def test_validator_flags_title_chapter_y_overlap():
    from xgen_edit2docs.tools._spec_validator import validate_spec_lock

    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    chapter_label: { x: 60, y: 90, w: 1180, h: 40 }\n"
        "    title: { x: 60, y: 100, w: 1180, h: 80 }\n"
    )
    result = validate_spec_lock(spec)
    detail = next(
        w for w in result.warnings if w.code == "spec_validator_page_zones"
    ).detail
    issues = {o["issue"] for o in detail["offenders"]}
    assert "title_and_chapter_overlap_y" in issues


def test_validator_passes_clean_zones():
    from xgen_edit2docs.tools._spec_validator import validate_spec_lock

    spec = (
        "page_zones:\n"
        "  P01:\n"
        "    chapter_label: { x: 60, y: 40, w: 1180, h: 24 }\n"
        "    title: { x: 60, y: 100, w: 1180, h: 120 }\n"
        "    page_number: { x: 1100, y: 684, w: 140, h: 20 }\n"
    )
    result = validate_spec_lock(spec)
    assert "spec_validator_page_zones" not in [w.code for w in result.warnings]
