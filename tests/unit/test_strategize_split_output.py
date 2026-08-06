"""Tests for the Strategist response splitter.

The splitter pulls ```design_spec ... ``` and ```spec_lock ... ```
fenced blocks out of the model's raw text. The hard case (and the one
that broke production) is when the design_spec body itself contains
nested triple-backtick blocks — SVG samples, YAML examples, palette
swatches — because a naive "first ``` after the opener" closer
truncates the block partway through and silently drops every
downstream section, including §IX Content Outline.
"""

from __future__ import annotations

from xgen_edit2docs.tools.strategize import _split_output


def test_basic_two_block_extraction():
    text = (
        "Some preamble.\n"
        "```design_spec\n"
        "# Title\nContent.\n"
        "```\n"
        "```spec_lock\n"
        "lang: ko-KR\npages:\n  - cover\n"
        "```\n"
    )
    warnings: list = []
    design, spec = _split_output(text, warnings)
    assert "# Title" in design
    assert "lang: ko-KR" in spec
    assert warnings == []


def test_nested_code_block_in_design_spec_not_truncated():
    """Reproduces the production failure: the design_spec contains an
    SVG sample fenced with ```svg ... ```, and §IX comes AFTER it. The
    paired extractor must pair design_spec with the LAST fence before
    spec_lock, not the first."""
    text = (
        "```design_spec\n"
        "## I. Project Info\nstuff\n"
        "## V. Layout\nExample:\n```svg\n<svg></svg>\n```\n"
        "## IX. Content Outline\n"
        "#### Slide 01 - Cover\n- Title: 표지\n"
        "#### Slide 02 - Body\n- Title: 본문\n"
        "```\n"
        "```spec_lock\n"
        "lang: ko-KR\n"
        "```\n"
    )
    warnings: list = []
    design, spec = _split_output(text, warnings)
    # The critical assertion: §IX must survive the extraction.
    assert "Slide 01 - Cover" in design
    assert "Slide 02 - Body" in design
    # And the nested SVG fence is preserved verbatim inside design_spec.
    assert "<svg></svg>" in design
    assert spec.startswith("lang: ko-KR")
    assert warnings == []


def test_spec_lock_without_closing_fence_runs_to_eof():
    """Models sometimes forget the trailing fence. Recover by reading to EOF."""
    text = (
        "```design_spec\n"
        "## Slide 1\nfoo\n"
        "```\n"
        "```spec_lock\n"
        "lang: ko-KR\npages:\n  - cover\n"
        # no trailing ```
    )
    warnings: list = []
    design, spec = _split_output(text, warnings)
    assert "Slide 1" in design
    assert "lang: ko-KR" in spec


def test_missing_spec_lock_warning_emitted():
    """When only design_spec exists, warn and let downstream handle the gap."""
    text = "```design_spec\n## Slide 1\nfoo\n```\n"
    warnings: list = []
    design, spec = _split_output(text, warnings)
    assert "Slide 1" in design
    assert spec == ""
    assert any(w.code == "missing_spec_lock_block" for w in warnings)


def test_completely_unfenced_input_falls_back_to_full_text():
    """No fences at all → design_spec gets the raw text, spec_lock empty."""
    text = "## Slide 1\nfoo\n## Slide 2\nbar\n"
    warnings: list = []
    design, spec = _split_output(text, warnings)
    assert "Slide 1" in design
    assert spec == ""
    codes = {w.code for w in warnings}
    assert "missing_design_spec_block" in codes
    assert "missing_spec_lock_block" in codes
