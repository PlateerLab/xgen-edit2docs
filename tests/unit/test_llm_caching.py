"""Prompt-cache assembly + role model tiering — the token-cost levers.

These verify the request SHAPE (breakpoint placement, block structure)
without an API call, plus the honest usage accounting and env-driven
model resolution. They are the guardrails for the token-optimization work.
"""

from __future__ import annotations

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMUsage, build_create_kwargs


def _system_blocks(kw):
    return kw["system"]


def _user_content(kw):
    return kw["messages"][0]["content"]


class TestSystemCaching:
    def test_single_system_block_caches_the_block(self):
        kw = build_create_kwargs(
            system_prompt="SYS", user_message="U", model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="",
        )
        blocks = _system_blocks(kw)
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_system_suffix_is_a_second_cached_block(self):
        """spec_lock/brief dedup: shared suffix cached, read across pages."""
        kw = build_create_kwargs(
            system_prompt="EXEC-SYS", user_message="page 3", model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix="SPEC_LOCK", user_suffix="",
        )
        blocks = _system_blocks(kw)
        assert [b["text"] for b in blocks] == ["EXEC-SYS", "SPEC_LOCK"]
        # Breakpoint on the LAST block caches the whole prefix (sys+suffix).
        assert "cache_control" not in blocks[0]
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}
        # And the page-specific content is a plain (uncached) user string.
        assert _user_content(kw) == "page 3"

    def test_cache_system_false_places_no_breakpoints(self):
        kw = build_create_kwargs(
            system_prompt="S", user_message="U", model="m",
            max_output_tokens=1000, temperature=None, cache_system=False,
            system_suffix="X", user_suffix="",
        )
        assert all("cache_control" not in b for b in _system_blocks(kw))


class TestUserCaching:
    def test_no_suffix_sends_plain_user_string(self):
        kw = build_create_kwargs(
            system_prompt="S", user_message="OUTLINE", model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="",
        )
        assert _user_content(kw) == "OUTLINE"

    def test_retry_prefix_is_cached_reminder_is_not(self):
        """The edit-retry win: identical prefix reads cache, only the
        reminder is fresh."""
        prefix = "big outline + sources + history"
        kw = build_create_kwargs(
            system_prompt="S", user_message=prefix, model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="\n\n# REMINDER: emit the plan block",
        )
        content = _user_content(kw)
        assert isinstance(content, list) and len(content) == 2
        assert content[0]["text"] == prefix
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert "REMINDER" in content[1]["text"]
        assert "cache_control" not in content[1]

    def test_first_call_and_retry_share_the_cache_prefix(self):
        """First call (no suffix) and retry (with suffix) must present the
        SAME cacheable prefix bytes so the retry reads what the first wrote.

        The first call carries no breakpoint on the plain string, but the
        cached prefix content is byte-identical, so the retry's breakpoint
        finds the write from a warm-up or the first turn's system cache."""
        prefix = "X" * 5000
        first = build_create_kwargs(
            system_prompt="S", user_message=prefix, model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="",
        )
        retry = build_create_kwargs(
            system_prompt="S", user_message=prefix, model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="\n\nREMINDER",
        )
        # Retry's first user block equals the first call's whole user content.
        assert _user_content(first) == _user_content(retry)[0]["text"]


class TestBreakpointBudget:
    def test_at_most_three_breakpoints(self):
        """API allows 4; we never place more than 3 (sys, suffix, user)."""
        kw = build_create_kwargs(
            system_prompt="S", user_message="U", model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix="SUF", user_suffix="TAIL",
        )
        n = sum("cache_control" in b for b in _system_blocks(kw))
        content = _user_content(kw)
        if isinstance(content, list):
            n += sum("cache_control" in b for b in content)
        assert n <= 4 and n == 2  # suffix block + user prefix


class TestTemperatureOmission:
    def test_omitted_when_none(self):
        kw = build_create_kwargs(
            system_prompt="S", user_message="U", model="m",
            max_output_tokens=1000, temperature=None, cache_system=True,
            system_suffix=None, user_suffix="",
        )
        assert "temperature" not in kw

    def test_included_when_set(self):
        kw = build_create_kwargs(
            system_prompt="S", user_message="U", model="m",
            max_output_tokens=1000, temperature=0.4, cache_system=True,
            system_suffix=None, user_suffix="",
        )
        assert kw["temperature"] == 0.4


class TestUsageAccounting:
    def test_total_prompt_tokens_includes_cache(self):
        u = LLMUsage(input_tokens=100, output_tokens=50,
                     cache_read_tokens=9000, cache_write_tokens=1000)
        assert u.total_prompt_tokens == 100 + 1000 + 9000

    def test_cost_equiv_discounts_cache_reads(self):
        u = LLMUsage(input_tokens=0, cache_read_tokens=10000)
        # 10k cache reads bill at ~0.1x → ~1000 equiv, not 10000.
        assert u.cost_input_equiv_tokens == pytest.approx(1000.0)

    def test_add_merges_all_fields(self):
        a = LLMUsage(1, 2, 3, 4)
        b = LLMUsage(10, 20, 30, 40)
        s = a + b
        assert (s.input_tokens, s.output_tokens, s.cache_read_tokens,
                s.cache_write_tokens) == (11, 22, 33, 44)


class TestModelTiering:
    def test_resolve_model_defaults_to_requested(self, monkeypatch):
        from xgen_edit2docs.config import reset_settings_cache, resolve_model

        for k in ("EDIT2DOCS_MODEL_PLANNER", "EDIT2DOCS_MODEL_EXECUTOR"):
            monkeypatch.delenv(k, raising=False)
        reset_settings_cache()
        assert resolve_model("planner", "claude-opus-4-7") == "claude-opus-4-7"

    def test_env_override_applies_per_role(self, monkeypatch):
        from xgen_edit2docs.config import reset_settings_cache, resolve_model

        monkeypatch.setenv("EDIT2DOCS_MODEL_PLANNER", "claude-sonnet-5")
        reset_settings_cache()
        assert resolve_model("planner", "claude-opus-4-7") == "claude-sonnet-5"
        # A role without an override still uses the requested model.
        assert resolve_model("executor", "claude-opus-4-7") == "claude-opus-4-7"
        monkeypatch.delenv("EDIT2DOCS_MODEL_PLANNER")
        reset_settings_cache()
