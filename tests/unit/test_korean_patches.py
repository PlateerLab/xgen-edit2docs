"""Unit tests for the G1/G2/G3 Korean patches against the core engine.

These run without external services (no LLM, no DB). They exist to lock in
the patches from ppt-master-analysis/03-korean-gaps.md against future
upstream merges or refactors.
"""

from __future__ import annotations

import pytest

from xgen_edit2docs.core.config import DEFAULT_FONT_STACKS, default_font_stack
from xgen_edit2docs.core.svg_to_pptx.drawingml_utils import (
    EA_FONTS,
    FONT_FALLBACK_WIN,
    detect_lang,
    estimate_text_width,
    is_cjk_char,
    is_hangul_char,
)


# ---------------------------------------------------------------------------
# G1: Hangul belongs to is_cjk_char (and gets full-width treatment)
# ---------------------------------------------------------------------------

class TestG1HangulInCjkRange:
    @pytest.mark.parametrize("ch", ["가", "안", "녕", "하", "세", "요", "힣"])
    def test_hangul_syllables_are_cjk(self, ch: str):
        assert is_cjk_char(ch), f"{ch!r} should be classified as CJK"
        assert is_hangul_char(ch)

    @pytest.mark.parametrize("ch", ["中", "国", "电", "建", "汽", "研"])
    def test_chinese_han_still_cjk(self, ch: str):
        assert is_cjk_char(ch)
        assert not is_hangul_char(ch)

    @pytest.mark.parametrize("ch", ["あ", "い", "ア", "イ"])
    def test_japanese_kana_now_cjk(self, ch: str):
        # The patch also brought kana into the CJK set.
        assert is_cjk_char(ch)

    @pytest.mark.parametrize("ch", ["A", "z", "1", " ", "?"])
    def test_latin_is_not_cjk(self, ch: str):
        assert not is_cjk_char(ch)

    def test_estimate_text_width_korean_now_full_width(self):
        # Before G1: Korean fell into the Latin else-branch (0.55 * font_size).
        # After G1: each Hangul syllable is counted at 1.0 * font_size.
        text = "안녕하세요"  # 5 syllables
        width = estimate_text_width(text, font_size=24, font_weight="400")
        # Tolerance ~5% for the 1.05 bold multiplier rounding etc.
        assert 5 * 24 * 0.95 <= width <= 5 * 24 * 1.05, (
            f"expected ~{5 * 24} px, got {width}"
        )


# ---------------------------------------------------------------------------
# G2: detect_lang chooses the right OOXML lang code
# ---------------------------------------------------------------------------

class TestG2DetectLang:
    def test_korean_text(self):
        assert detect_lang("안녕하세요") == "ko-KR"

    def test_korean_mixed_with_latin(self):
        assert detect_lang("안녕 Hello") == "ko-KR"

    def test_english_only(self):
        assert detect_lang("Hello world") == "en-US"

    def test_chinese_only(self):
        assert detect_lang("你好世界") == "zh-CN"

    def test_japanese_kana(self):
        assert detect_lang("こんにちは") == "ja-JP"

    def test_japanese_priority_over_han(self):
        # Kana presence wins over kanji-only fallback.
        assert detect_lang("こんにちは中国") == "ja-JP"

    def test_korean_priority_over_han(self):
        # Hangul wins over han (Korean sometimes mixes Hanja).
        assert detect_lang("안녕中") == "ko-KR"

    def test_empty_uses_default(self):
        assert detect_lang("", default="en-US") == "en-US"
        assert detect_lang("", default="ko-KR") == "ko-KR"


# ---------------------------------------------------------------------------
# G3: Korean fonts present in EA_FONTS + FONT_FALLBACK_WIN
# ---------------------------------------------------------------------------

class TestG3KoreanFontFallback:
    @pytest.mark.parametrize(
        "font",
        [
            "Malgun Gothic", "Gulim", "Dotum", "Batang",
            "Noto Sans KR", "Noto Serif KR",
            "Apple SD Gothic Neo", "Pretendard",
            "Spoqa Han Sans Neo", "Nanum Gothic",
        ],
    )
    def test_korean_font_in_ea_set(self, font: str):
        assert font in EA_FONTS, f"{font!r} must be registered as an East Asian font"

    @pytest.mark.parametrize(
        "macos_font, windows_font",
        [
            ("Apple SD Gothic Neo", "Malgun Gothic"),
            ("Pretendard", "Malgun Gothic"),
            ("Spoqa Han Sans Neo", "Malgun Gothic"),
            ("Source Han Sans KR", "Malgun Gothic"),
            ("Noto Sans CJK KR", "Malgun Gothic"),
            ("Noto Sans KR", "Malgun Gothic"),
        ],
    )
    def test_korean_windows_fallback_mapping(self, macos_font: str, windows_font: str):
        assert FONT_FALLBACK_WIN.get(macos_font) == windows_font

    def test_default_font_stack_for_korean(self):
        stack = default_font_stack("ko-KR")
        assert "Pretendard" in stack
        assert "Malgun Gothic" in stack
        assert "Noto Sans KR" in stack

    def test_default_font_stack_falls_back(self):
        # English-first: unknown locales fall back to the en-US stack;
        # Korean callers still get the full Korean stack (test above).
        stack = default_font_stack("xx-XX")
        assert stack == DEFAULT_FONT_STACKS["en-US"]

    def test_default_font_stack_prefix_match(self):
        # 2-letter prefix matches: "ko" -> "ko-KR"
        assert default_font_stack("ko") == DEFAULT_FONT_STACKS["ko-KR"]


# ---------------------------------------------------------------------------
# Cross-cutting: OOXML lang threads through the rendered XML
# ---------------------------------------------------------------------------

class TestOoxmlLangPropagation:
    def test_notes_slide_uses_detected_lang(self):
        from xgen_edit2docs.core.svg_to_pptx.pptx_notes import create_notes_slide_xml

        xml = create_notes_slide_xml(slide_num=1, notes_text="안녕하세요, 발표자 노트입니다.")
        assert 'lang="ko-KR"' in xml
        assert 'lang="zh-CN"' not in xml

    def test_notes_slide_respects_explicit_lang(self):
        from xgen_edit2docs.core.svg_to_pptx.pptx_notes import create_notes_slide_xml

        xml = create_notes_slide_xml(slide_num=1, notes_text="Hello", lang="ko-KR")
        # Explicit ko-KR even though text is English.
        assert 'lang="ko-KR"' in xml

    def test_notes_slide_english_default(self):
        from xgen_edit2docs.core.svg_to_pptx.pptx_notes import create_notes_slide_xml

        xml = create_notes_slide_xml(slide_num=1, notes_text="Plain English note.")
        assert 'lang="en-US"' in xml
