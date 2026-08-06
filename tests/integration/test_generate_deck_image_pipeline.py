"""Integration test for the C1 image pipeline.

Drives generate_deck end-to-end with:
- Strategist stub that emits an `images:` section in spec_lock
- Image generation stubs that return deterministic PNG bytes
- Executor stub that asserts it received the right ExecutorImage list per page
- Export check: the PPTX is produced and the workspace held the image file

The point of this test is to prove the wiring: Strategist's plan ->
generate_image / search_image -> ExecutorImage(placeholder, url) -> SVG
references -> export_pptx receives image bytes -> final PPTX contains the
embedded media.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xgen_edit2docs.llm.anthropic_client import LLMResult, LLMUsage
from xgen_edit2docs.tools import (
    ConvertRequest,
    ConvertResponse,
    CostBreakdown,
    ExecuteBatchResponse,
    ExecutePageResponse,
    GenerateImageResponse,
    SearchImageResponse,
    StrategizeResponse,
    WarningEntry,
)
from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck

# 1x1 PNG (8 bytes header + IHDR + IDAT + IEND). Used as the image generator's
# deterministic return value.
TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000005000186fb56250000000049454e44ae426082"
)

KOREAN_SVG_WITH_IMAGE = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
  <rect x="0" y="0" width="1920" height="1080" fill="#ffffff"/>
  <image href="hero_cover.png" x="100" y="100" width="800" height="600"/>
  <text x="120" y="780" font-family="Pretendard, sans-serif" font-size="48" fill="#1a1a1a">2026년 3분기 매출 보고</text>
</svg>
"""

KOREAN_SVG_TEXT_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1920 1080" width="1920" height="1080">
  <rect x="0" y="0" width="1920" height="1080" fill="#ffffff"/>
  <text x="120" y="540" font-family="Pretendard, sans-serif" font-size="56" fill="#1a1a1a">텍스트만 있는 슬라이드</text>
</svg>
"""


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _StrategizeStub:
    """Returns a StrategizeResponse whose spec_lock has an image plan."""

    spec_lock_yaml: str
    page_count: int

    async def __call__(self, req, *, client=None):
        return StrategizeResponse(
            raw_output="...",
            design_spec="\n".join(
                f"## Page {i}\n페이지 {i} 내용" for i in range(self.page_count)
            ),
            spec_lock=self.spec_lock_yaml,
            cost=CostBreakdown(input_tokens=10, output_tokens=10),
        )


@dataclass
class _ExecuteBatchStub:
    """Captures every ExecutePageRequest and returns canned SVGs."""

    received_pages: list = field(default_factory=list)

    async def __call__(self, req, *, client=None):
        self.received_pages.extend(req.pages)
        results = [
            ExecutePageResponse(
                page_index=p.page_index,
                # Reference the supplied image filename verbatim so the
                # downstream export can resolve it; otherwise emit a
                # text-only slide.
                svg=(
                    KOREAN_SVG_WITH_IMAGE.replace("hero_cover.png", p.images[0].url)
                    if p.images
                    else KOREAN_SVG_TEXT_ONLY
                ),
                speaker_notes=f"노트 {p.page_index}",
                raw_output="...",
                cost=CostBreakdown(),
                warnings=[],
            )
            for p in req.pages
        ]
        return ExecuteBatchResponse(results=results, cost=CostBreakdown(), warnings=[])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestImagePipelineEndToEnd:
    def setup_method(self):
        # Reach the generate_deck module via sys.modules (the submodule
        # attribute is shadowed by the re-exported function).
        self.gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        import xgen_edit2docs.tools.convert as convert_module
        self.convert_module = convert_module

    def _make_request(self, **overrides) -> GenerateDeckRequest:
        defaults = {
            "sources": [ConvertRequest(source_type="pdf", content=b"%PDF", original_filename="src.pdf")],
            "user_intent": "Q3 영업 결과 임원 보고",
            "target_pages": (2, 2),
            "lang": "ko-KR",
            "anthropic_api_key": "sk-ant-stub",
            "fail_on_quality_error": False,
        }
        defaults.update(overrides)
        return GenerateDeckRequest(**defaults)

    @pytest.mark.asyncio
    async def test_generated_image_flows_to_executor_and_export(self, monkeypatch):
        # Strategist plan: page 0 needs a generated hero, page 1 has no image.
        spec_lock = (
            "lang: ko-KR\n"
            "pages:\n"
            "  - 표지\n"
            "  - 결론\n"
            "images:\n"
            "  - page_index: 0\n"
            "    placeholder: hero_cover\n"
            "    mode: generate\n"
            "    prompt: Modern Korean office tower at sunset\n"
            "    aspect_ratio: 16:9\n"
            "    backend: openai\n"
        )
        strat = _StrategizeStub(spec_lock_yaml=spec_lock, page_count=2)
        execute = _ExecuteBatchStub()

        captured_image_requests: list = []

        def _stub_generate_image(req):
            captured_image_requests.append(req)
            return GenerateImageResponse(
                image=TRANSPARENT_PNG,
                mime_type="image/png",
                backend_used=req.backend,
                cost=CostBreakdown(image_count=1),
                warnings=[],
            )

        # Patch the call sites on the module that generate_deck imports from.
        monkeypatch.setattr(self.gd, "strategize", strat)
        monkeypatch.setattr(self.gd, "execute_batch", execute)
        monkeypatch.setattr(self.gd, "generate_image", _stub_generate_image)
        monkeypatch.setattr(
            self.convert_module,
            "convert_to_markdown",
            lambda r: ConvertResponse(
                markdown="# x", detected_format="pdf",
                original_filename=r.original_filename, char_count=1,
                cost=CostBreakdown(),
            ),
        )
        monkeypatch.setattr(self.gd, "convert_to_markdown",
                            lambda r: ConvertResponse(
                                markdown="# x", detected_format="pdf",
                                original_filename=r.original_filename, char_count=1,
                                cost=CostBreakdown(),
                            ))
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())

        result = await generate_deck(self._make_request(
            image_api_keys={"OPENAI_API_KEY": "sk-test"},
        ))

        # 1) Generation was invoked exactly once with the right prompt.
        assert len(captured_image_requests) == 1
        gen_req = captured_image_requests[0]
        assert gen_req.prompt.startswith("Modern Korean office tower")
        assert gen_req.backend == "openai"
        assert gen_req.api_keys == {"OPENAI_API_KEY": "sk-test"}

        # 2) Executor saw the right images per page.
        # page 0 has [hero_cover], page 1 has [].
        page_0 = next(p for p in execute.received_pages if p.page_index == 0)
        page_1 = next(p for p in execute.received_pages if p.page_index == 1)
        assert len(page_0.images) == 1
        img = page_0.images[0]
        assert img.placeholder == "hero_cover"
        assert img.url == "hero_cover.png"   # SVG-relative filename
        assert page_1.images == []

        # 3) The PPTX was produced and the embedded image bytes are inside.
        assert result.pptx[:4] == b"PK\x03\x04"
        with zipfile.ZipFile(io.BytesIO(result.pptx), "r") as zf:
            # python-pptx packs media under ppt/media/. Look for at least one
            # png in there.
            png_members = [
                n for n in zf.namelist() if n.startswith("ppt/media/") and n.endswith(".png")
            ]
            assert png_members, f"PPTX has no media/*.png entries. names={zf.namelist()[:20]}"

        # 4) Cost reflects the image acquisition.
        assert result.cost.image_count == 1

    @pytest.mark.asyncio
    async def test_skip_images_short_circuits_acquisition(self, monkeypatch):
        spec_lock = (
            "lang: ko-KR\n"
            "images:\n"
            "  - page_index: 0\n"
            "    placeholder: hero\n"
            "    mode: generate\n"
            "    prompt: ignored\n"
        )
        strat = _StrategizeStub(spec_lock_yaml=spec_lock, page_count=1)
        execute = _ExecuteBatchStub()

        called = []
        monkeypatch.setattr(self.gd, "strategize", strat)
        monkeypatch.setattr(self.gd, "execute_batch", execute)
        monkeypatch.setattr(self.gd, "generate_image",
                            lambda req: (called.append(req), None)[1])
        monkeypatch.setattr(self.gd, "convert_to_markdown",
                            lambda r: ConvertResponse(
                                markdown="# x", detected_format="pdf",
                                original_filename=None, char_count=1,
                                cost=CostBreakdown(),
                            ))
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())

        await generate_deck(self._make_request(skip_images=True))

        # generate_image was never invoked because skip_images=True.
        assert called == []
        # Executor received no images.
        assert execute.received_pages[0].images == []

    @pytest.mark.asyncio
    async def test_failed_image_acquisition_records_warning_but_continues(self, monkeypatch):
        spec_lock = (
            "lang: ko-KR\n"
            "images:\n"
            "  - page_index: 0\n"
            "    placeholder: hero\n"
            "    mode: generate\n"
            "    prompt: trigger failure\n"
        )
        strat = _StrategizeStub(spec_lock_yaml=spec_lock, page_count=1)
        execute = _ExecuteBatchStub()

        def _failing_generate(req):
            raise RuntimeError("backend blew up")

        monkeypatch.setattr(self.gd, "strategize", strat)
        monkeypatch.setattr(self.gd, "execute_batch", execute)
        monkeypatch.setattr(self.gd, "generate_image", _failing_generate)
        monkeypatch.setattr(self.gd, "convert_to_markdown",
                            lambda r: ConvertResponse(
                                markdown="# x", detected_format="pdf",
                                original_filename=None, char_count=1,
                                cost=CostBreakdown(),
                            ))
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())

        result = await generate_deck(self._make_request())

        # Pipeline did not crash. The page-level Executor got an empty image
        # list. A warning surfaces the acquisition failure.
        assert execute.received_pages[0].images == []
        codes = {w.code for w in result.warnings}
        assert "image_acquisition_failed" in codes
