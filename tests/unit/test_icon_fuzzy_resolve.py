"""Fuzzy icon-name resolution for the use-data-icon expander.

The Strategist routinely invents icon names that don't quite match the
bundled library — `trending-up` instead of `arrow-trend-up`, `brain`
instead of `brain-2`, and so on. Before the fuzzy resolver, every such
name became a `forbidden_use_data_icon` quality error that the retry
loop tried to nudge the model around. With fuzzy resolution we accept
near-matches at expansion time, so the deck visually carries the
intended glyph and no retry is needed.
"""

from __future__ import annotations

from pathlib import Path

from xgen_edit2docs.core.svg_to_pptx.use_expander import _fuzzy_resolve


ICONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "xgen_edit2docs" / "core" / "templates" / "icons"
)


def test_substring_match_resolves_trending_up():
    """Real production failure: chunk-filled/trending-up doesn't exist
    but arrow-trend-up does. Fuzzy resolver must pick the substring
    match without intervention."""
    matched = _fuzzy_resolve("chunk-filled/trending-up", ICONS_DIR)
    assert matched is not None
    assert matched.stem in ("arrow-trend-up", "arrow-trend-down")


def test_exact_substring_picks_shortest_candidate():
    """If multiple files contain the name as a substring, the resolver
    prefers the shortest stem (closest length to the requested name)."""
    # `target` exists exactly in chunk-filled — that should win over
    # `target-arrow` and `location-target`.
    matched = _fuzzy_resolve("chunk-filled/target", ICONS_DIR)
    assert matched is not None
    assert matched.stem == "target"


def test_unknown_library_returns_none():
    """No library folder → no candidates → None (caller falls back to
    placeholder behaviour)."""
    matched = _fuzzy_resolve("imaginary-library/anything", ICONS_DIR)
    assert matched is None


def test_no_separator_returns_none():
    matched = _fuzzy_resolve("just-a-name", ICONS_DIR)
    assert matched is None


def test_difflib_fallback_for_typos(tmp_path):
    """When no substring match exists, fall back to difflib similarity.
    Build a tiny synthetic library to make the test hermetic."""
    lib = tmp_path / "synthetic"
    lib.mkdir()
    for stem in ("rocket", "arrow-right", "chevron-down", "bolt"):
        (lib / f"{stem}.svg").write_text("<svg/>", encoding="utf-8")

    matched = _fuzzy_resolve("synthetic/rockt", tmp_path)  # typo for `rocket`
    assert matched is not None
    assert matched.stem == "rocket"


def test_completely_unrelated_name_returns_none(tmp_path):
    lib = tmp_path / "synthetic"
    lib.mkdir()
    (lib / "rocket.svg").write_text("<svg/>", encoding="utf-8")
    # `xyzzy` has nothing in common with `rocket`; cutoff is 0.7.
    matched = _fuzzy_resolve("synthetic/xyzzy", tmp_path)
    assert matched is None
