"""Integration test for the C2 audio narration pipeline.

Drives generate_deck with `narrate=True` and asserts:
- narrate_async is invoked with the Korean voice default + speaker notes
- The MP3 bytes returned flow into export_pptx
- The final PPTX contains audio media (ppt/media/*.mp3 entries)

LLM + TTS are both stubbed; this verifies the wiring, not the audio
quality.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from xgen_edit2docs.tools import (
    ConvertRequest,
    ConvertResponse,
    CostBreakdown,
    ExecuteBatchResponse,
    ExecutePageResponse,
    NarrateRequest,
    NarrateResponse,
    NarrateSlideAudio,
    StrategizeResponse,
)
from xgen_edit2docs.tools.generate_deck import GenerateDeckRequest, generate_deck

KOREAN_SVG = (Path(__file__).resolve().parents[1] / "fixtures" / "korean_slide.svg").read_text(
    encoding="utf-8"
)

# Minimal valid MP3 frame so file-format parsers see a real audio file.
# (ID3 v2.4 empty tag + a tiny silent MP3 frame.) Synthesized by hand.
MINIMAL_MP3 = (
    b"ID3\x04\x00\x00\x00\x00\x00\x00"
    + b"\xff\xfb\x90\xc4\x00" * 64  # MP3 frames (silent garbage but parseable)
)


@dataclass
class _StrategizeStub:
    page_count: int

    async def __call__(self, req, *, client=None):
        return StrategizeResponse(
            raw_output="...",
            design_spec="\n".join(
                f"## Page {i}\n페이지 {i} 내용" for i in range(self.page_count)
            ),
            spec_lock="lang: ko-KR\npages:\n  - 표지\n  - 결론\n",
            cost=CostBreakdown(input_tokens=10, output_tokens=10),
        )


@dataclass
class _ExecuteBatchStub:
    async def __call__(self, req, *, client=None):
        results = [
            ExecutePageResponse(
                page_index=p.page_index,
                svg=KOREAN_SVG,
                speaker_notes=f"# 페이지 {p.page_index}\n\n이 슬라이드의 발표자 노트입니다.",
                raw_output="...",
                cost=CostBreakdown(),
                warnings=[],
            )
            for p in req.pages
        ]
        return ExecuteBatchResponse(results=results, cost=CostBreakdown(), warnings=[])


@dataclass
class _NarrateStub:
    received: list[NarrateRequest] = field(default_factory=list)

    async def __call__(self, req: NarrateRequest) -> NarrateResponse:
        self.received.append(req)
        return NarrateResponse(
            audios=[
                NarrateSlideAudio(
                    index=s.index,
                    name=s.name,
                    mp3=MINIMAL_MP3,
                    voice_used=req.voice or "ko-KR-SunHiNeural",
                    spoken_chars=len(s.notes_markdown),
                )
                for s in req.slides
            ],
            cost=CostBreakdown(audio_seconds=4.2),
            warnings=[],
        )


class TestNarrationPipeline:
    def setup_method(self):
        self.gd = sys.modules["xgen_edit2docs.tools.generate_deck"]
        import xgen_edit2docs.tools.convert as convert_module
        self.convert_module = convert_module

    def _wire_pipeline(self, monkeypatch, narrate_stub: _NarrateStub):
        strat = _StrategizeStub(page_count=2)
        execute = _ExecuteBatchStub()
        monkeypatch.setattr(self.gd, "strategize", strat)
        monkeypatch.setattr(self.gd, "execute_batch", execute)
        monkeypatch.setattr(self.gd, "narrate_async", narrate_stub)
        monkeypatch.setattr(self.gd, "convert_to_markdown",
                            lambda r: ConvertResponse(
                                markdown="# x", detected_format="pdf",
                                original_filename=None, char_count=1,
                                cost=CostBreakdown(),
                            ))
        monkeypatch.setattr(self.gd, "AnthropicClient", lambda **kwargs: object())

    def _make_request(self, **overrides) -> GenerateDeckRequest:
        defaults = {
            "sources": [ConvertRequest(source_type="pdf", content=b"%PDF",
                                       original_filename="src.pdf")],
            "user_intent": "Q3 영업 결과 임원 보고",
            "target_pages": (2, 2),
            "lang": "ko-KR",
            "anthropic_api_key": "sk-ant-stub",
            "fail_on_quality_error": False,
            "skip_images": True,
        }
        defaults.update(overrides)
        return GenerateDeckRequest(**defaults)

    @pytest.mark.asyncio
    async def test_narrate_true_synthesizes_and_embeds_audio(self, monkeypatch):
        narrate_stub = _NarrateStub()
        self._wire_pipeline(monkeypatch, narrate_stub)

        result = await generate_deck(self._make_request(narrate=True))

        # 1) TTS was invoked with Korean defaults.
        assert len(narrate_stub.received) == 1
        narr_req = narrate_stub.received[0]
        assert narr_req.lang == "ko-KR"
        assert len(narr_req.slides) == 2
        # Speaker notes (Korean) reached the TTS stub verbatim.
        for slide in narr_req.slides:
            assert "발표자 노트" in slide.notes_markdown

        # 2) Final PPTX is real and contains audio media.
        assert result.pptx[:4] == b"PK\x03\x04"
        with zipfile.ZipFile(io.BytesIO(result.pptx), "r") as zf:
            audio_members = [
                n for n in zf.namelist()
                if n.startswith("ppt/media/") and n.endswith(".mp3")
            ]
            assert audio_members, (
                f"PPTX has no audio media. names={zf.namelist()[:20]}"
            )

        # 3) cost.audio_seconds reflects the narration synth.
        assert result.cost.audio_seconds > 0

    @pytest.mark.asyncio
    async def test_narrate_false_skips_synthesis(self, monkeypatch):
        narrate_stub = _NarrateStub()
        self._wire_pipeline(monkeypatch, narrate_stub)

        result = await generate_deck(self._make_request(narrate=False))

        # narrate_async never called.
        assert narrate_stub.received == []
        # PPTX produced (text-only) with NO audio media.
        with zipfile.ZipFile(io.BytesIO(result.pptx), "r") as zf:
            audio_members = [
                n for n in zf.namelist() if n.endswith(".mp3")
            ]
            assert audio_members == []

    @pytest.mark.asyncio
    async def test_narration_failure_warns_but_deck_still_exports(self, monkeypatch):
        async def _fail(req):
            raise RuntimeError("edge-tts unavailable")

        self._wire_pipeline(monkeypatch, _fail)

        result = await generate_deck(self._make_request(narrate=True))

        # Warning surfaces the failure but the PPTX still produced.
        codes = {w.code for w in result.warnings}
        assert "narration_failed" in codes
        assert result.pptx[:4] == b"PK\x03\x04"

    @pytest.mark.asyncio
    async def test_custom_voice_threads_through(self, monkeypatch):
        narrate_stub = _NarrateStub()
        self._wire_pipeline(monkeypatch, narrate_stub)

        await generate_deck(self._make_request(
            narrate=True,
            narration_voice="ko-KR-InJoonNeural",
            narration_rate="-10%",
        ))

        assert narrate_stub.received
        assert narrate_stub.received[0].voice == "ko-KR-InJoonNeural"
        assert narrate_stub.received[0].rate == "-10%"
