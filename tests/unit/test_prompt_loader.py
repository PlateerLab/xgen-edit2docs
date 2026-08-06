"""Unit tests for the M5 prompt loader + output-language directive."""

from __future__ import annotations

import pytest

from xgen_edit2docs.llm import (
    KNOWN_ROLES,
    build_output_lang_directive,
    list_available_prompts,
    load_prompt,
)
from xgen_edit2docs.llm.prompt_loader import PROMPTS_DIR


# ---------------------------------------------------------------------------
# load_prompt: single English source per role
# ---------------------------------------------------------------------------


class TestLoadPrompt:
    def test_strategist_loads(self):
        text = load_prompt("strategist")
        assert "Strategist" in text
        assert text.startswith("# Role: Strategist")

    def test_executor_base_loads(self):
        assert "executor" in load_prompt("executor-base").lower()

    def test_unknown_role_raises(self):
        with pytest.raises(FileNotFoundError):
            load_prompt("nonexistent-role")

    def test_every_known_role_resolves(self):
        for role in KNOWN_ROLES:
            # Should not raise; every role has its .en.md.
            assert load_prompt(role)

    def test_list_available_prompts(self):
        prompts = list_available_prompts()
        # Snapshot guard: every KNOWN_ROLES entry is on disk.
        assert KNOWN_ROLES <= set(prompts), (
            f"missing prompts: {KNOWN_ROLES - set(prompts)}"
        )

    def test_no_ko_md_files_exist(self):
        """Architectural assertion: there is no `<role>.ko.md` parallel set."""
        ko_md_files = list(PROMPTS_DIR.glob("*.ko.md"))
        assert ko_md_files == [], (
            f"Found {ko_md_files} — M5 collapsed prompts to a single English "
            "source. Add Korean guidance inside *.en.md instead."
        )


# ---------------------------------------------------------------------------
# build_output_lang_directive
# ---------------------------------------------------------------------------


class TestOutputLangDirective:
    def test_korean_directive_mentions_korean(self):
        directive = build_output_lang_directive("ko-KR")
        assert "Korean" in directive
        assert "한국어" in directive
        assert "ko-KR" in directive

    def test_english_directive_mentions_english(self):
        directive = build_output_lang_directive("en-US")
        assert "English" in directive
        assert "en-US" in directive

    def test_unknown_locale_echoes(self):
        # We don't have French label data, but the directive should still
        # render with the raw code so the LLM gets some signal.
        directive = build_output_lang_directive("fr-FR")
        assert "fr-FR" in directive

    def test_directive_calls_out_english_for_structural_keys(self):
        """Every directive must reinforce Track A (YAML keys stay English)."""
        directive = build_output_lang_directive("ko-KR")
        assert "YAML" in directive or "keys" in directive
        assert "English" in directive

    def test_directive_lists_user_facing_targets(self):
        directive = build_output_lang_directive("ko-KR")
        # Calls out slide titles, body copy, speaker notes etc.
        assert "title" in directive.lower()
        assert "speaker note" in directive.lower()


# ---------------------------------------------------------------------------
# Strategist prompt has the K.* appendix sections
# ---------------------------------------------------------------------------


class TestKoreanStrategistAppendix:
    def test_pretendard_recommended(self):
        text = load_prompt("strategist")
        assert "Pretendard" in text
        assert "Apple SD Gothic Neo" in text
        assert "Malgun Gothic" in text

    def test_no_italic_for_hangul_rule_present(self):
        text = load_prompt("strategist")
        # Korean copy never italicizes — this is one of the load-bearing rules.
        assert "italic" in text.lower()
        assert "Hangul" in text or "Korean" in text

    def test_korean_industry_palettes_present(self):
        text = load_prompt("strategist")
        # Pick a few must-have references from §K.2.
        for brand in ("Samsung", "Hyundai", "Naver", "Kakao", "Toss"):
            assert brand in text, f"Korean tone reference for {brand!r} missing"

    def test_korean_typography_hard_rules_present(self):
        text = load_prompt("strategist")
        assert "letter-spacing" in text or "Letter-spacing" in text
        assert "line-height" in text or "Line-height" in text

    def test_track_a_callout_present(self):
        """The appendix must restate the Track A boundary so the LLM doesn't
        accidentally translate YAML keys into Hangul."""
        text = load_prompt("strategist")
        assert "Track A" in text or "filesystem" in text.lower()
