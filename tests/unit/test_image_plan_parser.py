"""Unit tests for the image-plan parser used by generate_deck."""

from __future__ import annotations

import pytest

from xgen_edit2docs.tools._image_plan import ImagePlanItem, parse_image_plan


class TestFlatTopLevelImages:
    def test_basic_generate_item(self):
        yaml_text = """
        lang: ko-KR
        images:
          - page_index: 0
            placeholder: hero_cover
            mode: generate
            prompt: Modern Korean office tower at sunset
            aspect_ratio: 16:9
            backend: openai
        """
        items = parse_image_plan(yaml_text)
        assert len(items) == 1
        assert items[0].page_index == 0
        assert items[0].placeholder == "hero_cover"
        assert items[0].mode == "generate"
        assert items[0].prompt == "Modern Korean office tower at sunset"
        assert items[0].aspect_ratio == "16:9"
        assert items[0].backend == "openai"

    def test_search_item(self):
        yaml_text = """
        images:
          - page_index: 2
            placeholder: chart_revenue
            mode: search
            query: Seoul stock market trading floor
            providers: [pexels, pixabay]
        """
        items = parse_image_plan(yaml_text)
        assert len(items) == 1
        assert items[0].mode == "search"
        assert items[0].query == "Seoul stock market trading floor"
        assert items[0].providers == ["pexels", "pixabay"]

    def test_multiple_items_preserved_in_order(self):
        yaml_text = """
        images:
          - page_index: 0
            placeholder: hero
            mode: generate
            prompt: cover
          - page_index: 1
            placeholder: section_a
            mode: search
            query: data viz
          - page_index: 5
            placeholder: appendix_chart
            mode: generate
            prompt: bar chart
        """
        items = parse_image_plan(yaml_text)
        assert [i.page_index for i in items] == [0, 1, 5]
        assert [i.placeholder for i in items] == ["hero", "section_a", "appendix_chart"]


class TestNestedPerPageImages:
    def test_nested_under_pages(self):
        yaml_text = """
        pages:
          - id: cover
            title: 표지
            images:
              - placeholder: hero
                mode: generate
                prompt: A
          - id: content_1
            images:
              - placeholder: scene_1
                mode: search
                query: B
        """
        items = parse_image_plan(yaml_text)
        # page_index is inferred from list position (0, 1).
        assert len(items) == 2
        assert items[0].placeholder == "hero" and items[0].page_index == 0
        assert items[1].placeholder == "scene_1" and items[1].page_index == 1

    def test_explicit_page_index_overrides_position(self):
        yaml_text = """
        pages:
          - id: cover
            images:
              - placeholder: hero
                mode: generate
                prompt: x
                page_index: 7
        """
        items = parse_image_plan(yaml_text)
        assert items[0].page_index == 7


class TestEdgeCases:
    def test_no_images_yields_empty(self):
        assert parse_image_plan("lang: ko-KR\npages:\n  - id: cover\n") == []

    def test_invalid_yaml_returns_empty(self):
        assert parse_image_plan("::: not valid yaml :::") == []

    def test_malformed_item_is_dropped(self):
        yaml_text = """
        images:
          - page_index: 0
            placeholder: ok
            mode: generate
            prompt: fine
          - page_index: -1     # invalid (must be >= 0)
            placeholder: bad
            mode: generate
            prompt: ...
        """
        items = parse_image_plan(yaml_text)
        assert len(items) == 1
        assert items[0].placeholder == "ok"

    def test_dedup_by_page_and_placeholder(self):
        yaml_text = """
        images:
          - page_index: 0
            placeholder: hero
            mode: generate
            prompt: A
          - page_index: 0
            placeholder: hero
            mode: search
            query: dup
        """
        items = parse_image_plan(yaml_text)
        # First wins; duplicate dropped.
        assert len(items) == 1
        assert items[0].mode == "generate"


class TestPlaceholderHygiene:
    def test_korean_placeholder_rejected(self):
        with pytest.raises(ValueError, match="ASCII"):
            ImagePlanItem(page_index=0, placeholder="표지", mode="generate", prompt="x")

    def test_space_in_placeholder_rejected(self):
        with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
            ImagePlanItem(page_index=0, placeholder="hero cover", mode="generate", prompt="x")

    def test_underscore_dash_allowed(self):
        item = ImagePlanItem(
            page_index=0, placeholder="hero_cover-v2", mode="generate", prompt="x"
        )
        assert item.placeholder == "hero_cover-v2"
