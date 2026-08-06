"""The auto-id pass runs at the Executor → pipeline boundary.

PR #28 wired auto-id into the converter, but quality runs BEFORE
the converter — so production decks still showed nineteen
`Top-level visible <g> #N has no id` warnings. The right place for
the transformation is on the way out of the Executor, so the same
normalized SVG flows through quality, retry, export, and the final
PPTX.
"""

from __future__ import annotations

from xgen_edit2docs.tools.execute import _autoid_top_level_groups


def test_anonymous_groups_get_ids():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">'
        '<g><rect width="10" height="10"/></g>'
        '<g><circle r="5"/></g>'
        '<g><path d="M 0 0"/></g>'
        '</svg>'
    )
    out = _autoid_top_level_groups(svg)
    assert 'id="auto_grp_01"' in out
    assert 'id="auto_grp_02"' in out
    assert 'id="auto_grp_03"' in out


def test_namespace_preserved_in_output():
    """The serialized SVG must keep the `xmlns="http://www.w3.org/2000/svg"`
    declaration — without it the downstream parsers can't find tags."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><g><rect/></g></svg>'
    )
    out = _autoid_top_level_groups(svg)
    assert "http://www.w3.org/2000/svg" in out


def test_existing_ids_preserved():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="hero"><rect/></g>'
        '<g><circle/></g>'
        '</svg>'
    )
    out = _autoid_top_level_groups(svg)
    assert 'id="hero"' in out
    assert 'id="auto_grp_01"' in out
    # No nuking of the original.
    assert 'id="hero"' in out


def test_already_clean_svg_passes_through_untouched():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<g id="a"><rect/></g>'
        '<g id="b"><circle/></g>'
        '</svg>'
    )
    out = _autoid_top_level_groups(svg)
    # No `auto_grp_*` injected.
    assert "auto_grp" not in out


def test_malformed_svg_returned_unchanged():
    """Garbage in → same garbage out (downstream will surface the
    real error)."""
    garbage = "<svg><unclosed>"
    assert _autoid_top_level_groups(garbage) == garbage


def test_empty_input_returned_unchanged():
    assert _autoid_top_level_groups("") == ""
    assert _autoid_top_level_groups("not an svg") == "not an svg"
