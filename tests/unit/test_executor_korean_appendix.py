"""Unit tests for the Korean appendices on every executor prompt.

These act as architectural-regression guards: when someone refactors the
executor prompts they should not silently drop the load-bearing Korean
typography / layout rules that downstream output quality depends on.
"""

from __future__ import annotations

import pytest

from xgen_edit2docs.llm import load_prompt


class TestExecutorBaseKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("executor-base")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text

    def test_hangul_density_table(self):
        # Density caps (page rhythm) are the load-bearing layout signal.
        assert "Hangul / line" in self.text or "Hangul / page" in self.text
        # Cover-title cap.
        assert "64–80" in self.text or "64-80" in self.text

    def test_no_hyphenation_rule(self):
        assert "Linebreak hygiene" in self.text or "hyphenation" in self.text.lower()
        # Korean particles list — Hangul-friendly break hints.
        assert "은/는" in self.text or "을/를" in self.text

    def test_no_italic_for_hangul(self):
        assert "italic" in self.text.lower()
        assert "normal" in self.text  # font-style="normal" rule

    def test_anchor_dense_breathing_for_korean(self):
        # The base Page Rhythm shrinks under K.3 budgets.
        for label in ("Anchor page", "Dense page", "Breathing page"):
            assert label in self.text, f"K.3 missing {label!r} section"

    def test_track_a_callout(self):
        assert "stay English" in self.text or "Track A" in self.text


class TestConsultantKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("executor-consultant")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text or "Korean (ko-KR) Consulting" in self.text

    def test_scqa_translated(self):
        # SCQA structure stays — Korean labels follow.
        for label in ("상황", "복잡성", "질문", "답변"):
            assert label in self.text, f"SCQA Korean label {label!r} missing"

    def test_korean_consulting_tones(self):
        # At least the three named tones from the strategist match here.
        assert "베인" in self.text or "Bain" in self.text
        assert "KPMG" in self.text or "삼정" in self.text

    def test_number_unit_convention(self):
        # Korean financial-unit suffixes are load-bearing for consulting decks.
        assert "억" in self.text and "조" in self.text


class TestConsultantTopKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("executor-consultant-top")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text

    def test_pyramid_structure(self):
        for label in ("답변", "논거", "근거", "피라미드"):
            assert label in self.text, f"pyramid Korean label {label!r} missing"

    def test_mece_korean_groupings(self):
        # Spot-check a couple of the canonical MECE labels.
        assert "단기" in self.text
        assert "장기" in self.text
        # MECE label literal too.
        assert "MECE" in self.text

    def test_executive_deck_structure(self):
        assert "요약" in self.text
        assert "목차" in self.text or "TOC" in self.text


class TestGeneralKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("executor-general")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text

    def test_register_table(self):
        for register in ("격식체", "해요체"):
            assert register in self.text, f"Korean register {register!r} missing"

    def test_kstartup_tone_mentioned(self):
        # The general style reflects modern K-startup conventions.
        assert "Toss" in self.text or "토스" in self.text or "당근" in self.text
