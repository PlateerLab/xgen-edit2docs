"""Unit tests for tools.edit_deck's deterministic helpers."""

from __future__ import annotations

from xgen_edit2docs.tools.edit_deck import (
    _extract_slide_text,
    _extract_svg_block,
    _parse_plan,
    _restore_images,
    _stub_images,
    _validate_operations,
)


class TestImageStubbing:
    def test_round_trip(self):
        svg = (
            '<svg><image href="data:image/png;base64,AAAA"/>'
            '<image href="data:image/jpeg;base64,BBBB"/></svg>'
        )
        stubbed, mapping = _stub_images(svg)
        assert "data:" not in stubbed
        assert 'href="asset:IMG_1"' in stubbed
        assert 'href="asset:IMG_2"' in stubbed
        assert _restore_images(stubbed, mapping) == svg

    def test_no_images_pass_through(self):
        svg = "<svg><rect/></svg>"
        stubbed, mapping = _stub_images(svg)
        assert stubbed == svg and mapping == {}


class TestPlanParsing:
    def test_reply_and_operations(self):
        text = (
            "```reply\n3번 슬라이드 제목을 바꿉니다.\n```\n"
            "```edit_plan\noperations:\n  - action: edit\n    slide: 3\n"
            '    brief: "change title"\n```'
        )
        warnings: list = []
        reply, ops, missing = _parse_plan(text, warnings)
        assert "3번" in reply
        assert ops == [{"action": "edit", "slide": 3, "brief": "change title"}]
        assert missing is False
        assert warnings == []

    def test_missing_plan_block_flags_missing(self):
        warnings: list = []
        reply, ops, missing = _parse_plan("```reply\n답변만 합니다.\n```", warnings)
        assert ops == []
        assert missing is True
        assert warnings[0].code == "edit_plan_block_missing"

    def test_empty_operations_is_valid_not_missing(self):
        warnings: list = []
        _, ops, missing = _parse_plan(
            "```reply\n질문에 답변합니다.\n```\n```edit_plan\noperations: []\n```",
            warnings,
        )
        assert ops == []
        assert missing is False

    def test_invalid_yaml_flags_missing(self):
        warnings: list = []
        _, ops, missing = _parse_plan(
            "```reply\nok\n```\n```edit_plan\n: not yaml [\n```", warnings
        )
        assert ops == []
        assert missing is True

    def test_unclosed_fence_is_recovered(self):
        # Output-token limit can cut the plan mid-entry: no closing fence
        # and a dangling last op. The parseable prefix must survive.
        text = (
            "```reply\n제목을 전부 바꿉니다.\n```\n"
            "```edit_plan\n"
            "operations:\n"
            '  - action: edit\n    slide: 1\n    brief: "title 1"\n'
            '  - action: edit\n    slide: 2\n    brief: "title 2"\n'
            '  - action: edit\n    slide: 3\n    brief: "tit'  # truncated!
        )
        warnings: list = []
        _, ops, missing = _parse_plan(text, warnings)
        assert missing is False
        assert len(ops) >= 2
        assert ops[0] == {"action": "edit", "slide": 1, "brief": "title 1"}


class TestOperationValidation:
    def test_out_of_range_and_duplicates_skipped(self):
        warnings: list = []
        ops = _validate_operations(
            [
                {"action": "edit", "slide": 99, "brief": "x"},
                {"action": "edit", "slide": 2, "brief": "x"},
                {"action": "delete", "slide": 2},
                {"action": "wat", "slide": 1},
            ],
            page_count=3,
            cap=8,
            warnings=warnings,
        )
        assert ops == [{"action": "edit", "slide": 2, "brief": "x"}]
        codes = [w.code for w in warnings]
        assert "edit_op_slide_out_of_range" in codes
        assert "edit_op_duplicate_slide" in codes
        assert "edit_op_unknown_action" in codes

    def test_add_clamps_position_and_requires_brief(self):
        warnings: list = []
        ops = _validate_operations(
            [
                {"action": "add", "after": 99, "brief": "new"},
                {"action": "add", "after": 1},
            ],
            page_count=3,
            cap=8,
            warnings=warnings,
        )
        assert ops == [{"action": "add", "after": 3, "brief": "new"}]
        assert [w.code for w in warnings] == ["edit_op_brief_missing"]

    def test_cap_truncates(self):
        warnings: list = []
        raw = [{"action": "delete", "slide": i} for i in range(1, 6)]
        ops = _validate_operations(raw, page_count=10, cap=2, warnings=warnings)
        assert len(ops) == 2
        assert warnings[-1].code == "edit_plan_truncated"


class TestSvgExtraction:
    def test_fenced_block(self):
        assert _extract_svg_block("```svg\n<svg>x</svg>\n```") == "<svg>x</svg>"

    def test_bare_document(self):
        assert _extract_svg_block("noise <svg a='1'>y</svg> tail") == "<svg a='1'>y</svg>"

    def test_missing(self):
        assert _extract_svg_block("no svg here") is None


class TestSlideTextExtraction:
    def test_collects_text_elements(self):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg">'
            "<text>제목</text><g><text>본문 <tspan>이어서</tspan></text></g></svg>"
        )
        out = _extract_slide_text(svg)
        assert "제목" in out and "본문" in out

    def test_garbage_returns_empty(self):
        assert _extract_slide_text("not xml <<<") == ""
