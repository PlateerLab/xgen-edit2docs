"""Unit tests for core.template_import.context (Strategist digest + canvas)."""

from __future__ import annotations

import pytest

from xgen_edit2docs.core.template_import.context import (
    TemplateCanvasError,
    build_template_context,
    resolve_template_canvas,
)


def _manifest(width_px: int = 1280, height_px: int = 720) -> dict:
    return {
        "source": {"name": "brand_deck.pptx"},
        "slideSize": {
            "width_px": width_px,
            "height_px": height_px,
            "width_emu": width_px * 9525,
            "height_emu": height_px * 9525,
        },
        "theme": {
            "colors": {"dk1": "#000000", "accent1": "#1B64DA"},
            "fonts": {"majorLatin": "Pretendard", "minorEastAsia": "Noto Sans KR"},
        },
        "pageTypeCandidates": {"cover": [1], "content": [2, 3]},
        "slides": [
            {"index": 1, "textSamples": ["2026 브랜드 전략", "Q3 실적 요약"]},
            {"index": 2, "textSamples": ["핵심 지표"]},
        ],
    }


class TestResolveTemplateCanvas:
    def test_standard_16_9(self):
        assert resolve_template_canvas(_manifest(1280, 720)) == ("ppt169", 1280, 720)

    def test_legacy_small_16_9(self):
        assert resolve_template_canvas(_manifest(960, 540)) == ("ppt169", 960, 540)

    def test_standard_4_3(self):
        assert resolve_template_canvas(_manifest(960, 720)) == ("ppt43", 960, 720)

    def test_unsupported_aspect_raises(self):
        with pytest.raises(TemplateCanvasError):
            resolve_template_canvas(_manifest(1080, 1920))

    def test_zero_size_raises(self):
        with pytest.raises(TemplateCanvasError):
            resolve_template_canvas({"slideSize": {}})


class TestBuildTemplateContext:
    def test_digest_contains_theme_and_samples(self):
        ctx = build_template_context(_manifest())
        assert "brand_deck.pptx" in ctx
        assert "accent1: #1B64DA" in ctx
        assert "majorLatin: Pretendard" in ctx
        assert "1280 x 720 px" in ctx
        assert "cover: slides 1" in ctx
        assert "2026 브랜드 전략" in ctx

    def test_extend_mode_warns_about_master_chrome(self):
        ctx = build_template_context(_manifest(), deck_mode="template_extend")
        assert "APPENDED" in ctx

    def test_restyle_mode_mentions_fresh_deck(self):
        ctx = build_template_context(_manifest(), deck_mode="template_restyle")
        assert "fresh deck" in ctx

    def test_empty_theme_still_renders(self):
        manifest = _manifest()
        manifest["theme"] = {"colors": {}, "fonts": {}}
        manifest["slides"] = []
        manifest["pageTypeCandidates"] = {}
        ctx = build_template_context(manifest)
        assert "brand_deck.pptx" in ctx
