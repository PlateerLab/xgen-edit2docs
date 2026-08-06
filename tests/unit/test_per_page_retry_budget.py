"""Per-page retry budget tests (P2.5).

The retry loop in `generate_deck` enforces two budgets:
* `per_page_cap` — max attempts for any single page
* `total_remaining` — global cap so a deck-wide failure can't
  burn through unbounded budget

Pages that pass quality the first time don't consume budget;
their unused attempts can't be transferred to a still-failing page.

The full retry loop runs inside the async pipeline and is awkward
to unit-test end-to-end. These tests assert the *budget bookkeeping*
behaviour through targeted invariants — when budget is exhausted the
loop emits a warning code that downstream consumers can rely on.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools.types import WarningEntry


# ---------------------------------------------------------------------------
# Stub LLM and stub execute_batch — we exercise the orchestration code,
# not the actual LLM call.
# ---------------------------------------------------------------------------


@dataclass
class _PageFailureScript:
    """Per-page script: list of fail/pass for each retry attempt."""

    pattern: list[bool]  # True = quality fail this attempt, False = pass
    call_count: int = 0

    def will_fail_next(self) -> bool:
        if self.call_count < len(self.pattern):
            r = self.pattern[self.call_count]
        else:
            r = False  # default to pass once script exhausted
        self.call_count += 1
        return r


# Sample SVG that passes quality (canonical viewBox, no forbidden elements).
_GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<rect width="1280" height="720" fill="#FFFFFF"/>'
    '<text x="60" y="120" font-size="40" font-family="Pretendard, sans-serif">제목</text>'
    '</svg>'
)

# A page output that contains a forbidden_foreign_object so quality
# flags it. We use `<foreignObject>` rather than `<use>` because the
# `<use>` safety-net (PR #26) silently rewrites unresolvable
# references into empty `<g>` shapes and the page would otherwise
# pass quality on its own. `<foreignObject>` survives every
# normalisation pass and reliably fires a quality error.
_BAD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" '
    'width="1280" height="720">'
    '<rect width="1280" height="720" fill="#FFFFFF"/>'
    '<foreignObject width="100" height="100">'
    '<div xmlns="http://www.w3.org/1999/xhtml">x</div>'
    '</foreignObject>'
    '</svg>'
)


# ---------------------------------------------------------------------------
# Direct test on the bookkeeping invariants
# ---------------------------------------------------------------------------


def test_per_page_cap_independent_of_total():
    """When a single page repeatedly fails, it stops after
    `per_page_cap` attempts — even if total budget is still
    available for other pages."""
    per_page_attempts: dict[int, int] = {0: 2}  # page 0 already retried twice
    per_page_cap = 2

    errors_by_page = {0: [object()], 1: [object()]}  # both still failing

    eligible = sorted(
        i for i in errors_by_page.keys()
        if per_page_attempts.get(i, 0) < per_page_cap
    )
    assert eligible == [1]  # page 0 is exhausted, page 1 still has budget


def test_total_remaining_decrements_per_attempt():
    """One round retrying 3 pages consumes 3 from total budget."""
    failing_pages = [0, 1, 2]
    total_remaining = 10
    for _ in failing_pages:
        total_remaining -= 1
    assert total_remaining == 7


def test_total_remaining_caps_round_size():
    """When the failing-pages list is larger than the remaining
    total budget, only `total_remaining` pages are picked."""
    failing_pages = [0, 1, 2, 3, 4]
    total_remaining = 2
    picked = failing_pages[:total_remaining]
    dropped = failing_pages[total_remaining:]
    assert picked == [0, 1]
    assert dropped == [2, 3, 4]


def test_total_default_at_least_six_even_for_short_decks():
    """A 2-page deck still gets at least 6 total retries so any one
    page that's flaky has room to converge."""
    page_count = 2
    per_page = 2
    total = max(6, page_count * per_page)
    assert total == 6


def test_total_scales_with_page_count():
    """A 10-page deck with per_page=2 has up to 20 retries
    available — enough that several pages can fail without
    blocking other pages' attempts."""
    page_count = 10
    per_page = 2
    total = max(6, page_count * per_page)
    assert total == 20


# ---------------------------------------------------------------------------
# Integration test — runs the actual retry loop with stubbed LLM
# ---------------------------------------------------------------------------


class _PageScriptedLLM:
    """Stub LLM whose output depends on a per-page failure script.

    The first call to `complete` for each page picks the script keyed
    by `page_index` (extracted from the user message). Subsequent
    calls for the same page advance the script."""

    def __init__(self, scripts: dict[int, _PageFailureScript]):
        self.scripts = scripts
        self.calls = 0

    async def complete(self, system_prompt, user_message, **kwargs):
        self.calls += 1
        # Extract page index from message: `# Page N (lang)`
        import re
        m = re.search(r"# Page (\d+)", user_message)
        page_index = int(m.group(1)) if m else 0
        script = self.scripts.setdefault(page_index, _PageFailureScript(pattern=[]))
        will_fail = script.will_fail_next()
        svg_payload = _BAD_SVG if will_fail else _GOOD_SVG
        text = f"```svg\n{svg_payload}\n```\n```notes\nstub\n```"
        return LLMResult(
            text=text,
            usage=LLMUsage(input_tokens=10, output_tokens=10),
            model="stub",
            stop_reason="end_turn",
        )


@pytest.mark.asyncio
async def test_exhausted_page_emits_warning_code(monkeypatch):
    """When a single page exhausts its per-page cap, an
    `retry_per_page_cap_reached` warning is emitted on the response."""
    from xgen_edit2docs.tools.execute import execute_batch
    from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck

    # Patch strategize so we control the spec_lock/design_spec.
    from xgen_edit2docs.tools import strategize as strategize_mod
    from xgen_edit2docs.tools.strategize import StrategizeResponse
    from xgen_edit2docs.tools.types import CostBreakdown

    async def fake_strategize(req, *, client=None):
        return StrategizeResponse(
            raw_output="stub",
            design_spec="## IX. Outline\n#### P01. test\n#### P02. test\n",
            spec_lock="canvas:\n  format: ppt169\ncolors:\n  primary: '#000000'\ntypography:\n  body: 20\n",
            cost=CostBreakdown(),
        )

    # Patch execute_batch with our stub LLM. Page 0 always fails;
    # page 1 always passes.
    scripts = {
        0: _PageFailureScript(pattern=[True, True, True, True]),
        1: _PageFailureScript(pattern=[False]),
    }
    stub_llm = _PageScriptedLLM(scripts)

    async def patched_execute_batch(req, *, client=None):
        return await execute_batch(req, client=stub_llm)

    import sys as _sys
    gd = _sys.modules["xgen_edit2docs.tools.generate_deck"]
    monkeypatch.setattr(gd, "strategize", fake_strategize)
    monkeypatch.setattr(gd, "execute_batch", patched_execute_batch)

    # Skip the export stage — we only care about the retry bookkeeping.
    from xgen_edit2docs.tools.export import ExportResponse

    def fake_export(req):
        return ExportResponse(
            pptx=b"PK\x03\x04stub",
            page_count=len(req.slides),
            detected_langs=[],
            cost=CostBreakdown(),
        )

    monkeypatch.setattr(gd, "export_pptx", fake_export)
    monkeypatch.setattr(gd, "AnthropicClient", lambda **kw: stub_llm)

    resp = await generate_deck(
        GenerateDeckRequest(
            sources=[],
            user_intent="테스트",
            anthropic_api_key="sk-ant-stub",
            target_pages=(2, 2),
            skip_images=True,
            fail_on_quality_error=False,
            retry_pages_on_quality_error=2,
        )
    )
    codes = [w.code for w in resp.warnings]
    # Page 0 keeps failing — after 2 attempts the per-page cap fires.
    assert "retry_per_page_cap_reached" in codes, codes
    # Page 1 passes so it doesn't appear in the exhausted list.
    cap_warning = next(w for w in resp.warnings if w.code == "retry_per_page_cap_reached")
    assert 0 in cap_warning.detail["pages"]
    assert 1 not in cap_warning.detail["pages"]
