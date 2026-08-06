"""Unit tests for the M5.3 Korean appendices on image prompts."""

from __future__ import annotations

from xgen_edit2docs.llm import load_prompt


class TestImageGeneratorKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("image-generator")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text

    def test_prompt_stays_english_rule(self):
        # The load-bearing rule: prompts to the model stay English even for
        # Korean decks.
        assert "image-generation prompts" in self.text.lower() or "prompt" in self.text.lower()
        assert "English" in self.text
        # Reasoning paragraph present.
        assert "training data" in self.text.lower()

    def test_korean_subject_table_present(self):
        # Spot-check canonical Korean subjects + their English renderings.
        for korean, english in (
            ("한복", "hanbok"),
            ("한옥", "hanok"),
            ("한식", "bibimbap"),
        ):
            assert korean in self.text, f"{korean!r} missing from subjects table"
            assert english in self.text, f"{english!r} (English phrasing) missing"

    def test_no_hangul_in_image_rule(self):
        assert "Hangul" in self.text
        # The hard rule: do not render Hangul inside the image; add text as SVG.
        assert "SVG" in self.text or "<text>" in self.text

    def test_brand_tone_translations(self):
        # The Korean brand-tone hints from strategist §K.2 map to English
        # visual-prompt vocabulary here.
        for cue in ("Toss", "Kakao", "Naver", "Samsung"):
            assert cue in self.text, f"brand-tone cue {cue!r} missing"


class TestImageSearcherKoreanAppendix:
    def setup_method(self):
        self.text = load_prompt("image-searcher")

    def test_appendix_present(self):
        assert "Appendix K. Korean" in self.text

    def test_translate_to_english_rule(self):
        # The load-bearing rule: search queries stay English even for Korean
        # decks because Pexels/Pixabay/Openverse catalog English captions.
        assert "English" in self.text
        for provider in ("Pexels", "Pixabay", "Wikimedia"):
            assert provider in self.text, f"provider {provider!r} not referenced"

    def test_korean_query_translation_table(self):
        # Spot-check the table.
        for korean, english in (
            ("한국 직장인", "Korean office workers"),
            ("서울 야경", "Seoul"),
        ):
            assert korean in self.text, f"{korean!r} missing from translation table"
            assert english in self.text, f"{english!r} translation missing"

    def test_attribution_korean_phrasing(self):
        # Korean attribution text patterns.
        assert "사진:" in self.text or "출처" in self.text
        # And the license code stays English/uppercase.
        assert "CC BY" in self.text

    def test_track_a_callout(self):
        # JSON keys stay English even with Korean attribution_text values.
        assert "image_sources.json" in self.text
        # Track A keyword appears (English-only structural keys).
        assert "Track A" in self.text or "keys" in self.text.lower()
