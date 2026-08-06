"""English-first defaults with first-class Korean support.

The contract under test: every default is en-US, every Korean behavior is
one explicit ``lang="ko-KR"`` (or Accept-Language) away, and nothing about
the Korean path is a degraded subset.
"""

from __future__ import annotations

from xgen_edit2docs.api.errors import bilingual_detail
from xgen_edit2docs.core.config import DEFAULT_FONT_STACKS, default_font_stack
from xgen_edit2docs.documents.docx_engine import base_font_for_lang
from xgen_edit2docs.i18n import DEFAULT_LOCALE, FALLBACK_LOCALE
from xgen_edit2docs.tools._edit_events import op_summary, plan_event_vars
from xgen_edit2docs.tools._reply_texts import reply_text
from xgen_edit2docs.tools.types import DEFAULT_LANG


class TestDefaults:
    def test_engine_default_lang_is_english(self):
        assert DEFAULT_LANG == "en-US"

    def test_catalog_default_and_fallback_are_english(self):
        assert DEFAULT_LOCALE == "en-US"
        assert FALLBACK_LOCALE == "en-US"

    def test_settings_default_lang_is_english(self):
        from xgen_edit2docs.config import Settings

        assert Settings.model_fields["default_lang"].default == "en-US"

    def test_font_stack_fallback_is_english_but_korean_intact(self):
        assert default_font_stack("xx-XX") == DEFAULT_FONT_STACKS["en-US"]
        korean = default_font_stack("ko-KR")
        assert "Pretendard" in korean and "Malgun Gothic" in korean


class TestOpLabels:
    OPS = {
        "docx": {"action": "replace", "para": 2},
        "pptx": {"action": "edit", "slide": 3},
        "xlsx": {"action": "set_cell", "sheet": "Sales", "cell": "B3"},
    }

    def test_labels_default_to_english(self):
        s = op_summary("docx", self.OPS["docx"], index=0, total=1)
        assert s["label"] == "Replace paragraph 2"
        s = op_summary("pptx", self.OPS["pptx"], index=0, total=1)
        assert s["label"] == "Edit slide 3"
        s = op_summary("xlsx", self.OPS["xlsx"], index=0, total=1)
        assert s["label"] == "[Sales] Edit cell B3"

    def test_labels_localize_to_korean(self):
        s = op_summary("docx", self.OPS["docx"], index=0, total=1, lang="ko-KR")
        assert s["label"] == "2번 문단 교체"
        s = op_summary("pptx", self.OPS["pptx"], index=0, total=1, lang="ko-KR")
        assert s["label"] == "3번 슬라이드 편집"
        s = op_summary("xlsx", self.OPS["xlsx"], index=0, total=1, lang="ko-KR")
        assert s["label"] == "[Sales] B3 셀 수정"

    def test_targets_are_language_independent(self):
        en = op_summary("docx", self.OPS["docx"], index=0, total=1, lang="en-US")
        ko = op_summary("docx", self.OPS["docx"], index=0, total=1, lang="ko-KR")
        assert en["target"] == ko["target"] == {"kind": "paragraph", "para": 2}

    def test_plan_event_vars_threads_lang(self):
        plan = plan_event_vars("docx", [self.OPS["docx"]], lang="ko-KR")["plan"]
        assert plan[0]["label"] == "2번 문단 교체"


class TestReplyTexts:
    def test_english_default(self):
        assert "no changes were applied" in reply_text("plan_failed", "en-US")
        assert reply_text("request_done", "en-US") == "Done."

    def test_korean(self):
        assert "적용되지 않았습니다" in reply_text("plan_failed", "ko-KR")
        assert reply_text("request_done", "ko-KR") == "요청을 처리했습니다."

    def test_unknown_locale_falls_back_to_english(self):
        assert "no changes were applied" in reply_text("plan_failed", "ja-JP")

    def test_format_vars(self):
        text = reply_text("plan_truncated", "en-US", emitted=40, cap=30)
        assert "40" in text and "30" in text
        text_ko = reply_text("plan_truncated", "ko-KR", emitted=40, cap=30)
        assert "40" in text_ko and "30" in text_ko


class TestBilingualDetail:
    def test_english_primary_by_default(self):
        d = bilingual_detail("X", en="english", ko="한국어")
        assert d["message"] == "english"
        assert d["message_en"] == "english" and d["message_ko"] == "한국어"

    def test_korean_locale_selects_korean(self):
        d = bilingual_detail("X", en="english", ko="한국어", locale="ko-KR")
        assert d["message"] == "한국어"
        assert d["message_en"] == "english"  # always present for clients

    def test_extra_fields_pass_through(self):
        d = bilingual_detail("X", en="e", ko="k", details={"a": 1})
        assert d["details"] == {"a": 1}


class TestDocxBaseFont:
    def test_english_default_is_calibri(self):
        assert base_font_for_lang("en-US") == "Calibri"
        assert base_font_for_lang(None) == "Calibri"

    def test_cjk_locales_get_native_faces(self):
        assert base_font_for_lang("ko-KR") == "맑은 고딕"
        assert base_font_for_lang("ja-JP") == "Yu Gothic"
        assert base_font_for_lang("zh-CN") == "Microsoft YaHei"

    def test_generated_docx_carries_lang_font(self):
        import io

        from docx import Document

        from xgen_edit2docs.documents.docx_engine import docx_from_markdown

        en_doc = Document(io.BytesIO(docx_from_markdown("# Title\n\nBody")))
        assert en_doc.styles["Normal"].font.name == "Calibri"
        ko_doc = Document(io.BytesIO(docx_from_markdown("# 제목\n\n본문", lang="ko-KR")))
        assert ko_doc.styles["Normal"].font.name == "맑은 고딕"
