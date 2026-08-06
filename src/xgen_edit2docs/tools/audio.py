"""Audio narration tool: speaker notes -> MP3 (via edge-tts by default).

For M2 we only wire up the edge backend (free, locale-aware, supports Korean
voices added by the G10 patch in core/tts_backends/backend_edge.py). Other
backends from ppt-master (ElevenLabs, MiniMax, Qwen, CosyVoice) can be added
in M5 once the BYOK key plumbing is in place.

Each speaker note is rendered independently — the caller (worker) decides
how to parallelize.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import Field

from ..core.tts_backends.backend_edge import (
    DEFAULT_VOICE_PER_LOCALE,
    default_voice_for_locale,
    generate as edge_generate,
)
from ..core.notes_to_audio import spoken_text
from ._workspace import temp_workspace
from .types import (
    CostBreakdown,
    DEFAULT_LANG,
    LangCode,
    ToolRequest,
    ToolResponse,
    WarningEntry,
)


class NarrateSlide(ToolRequest):
    index: int = Field(..., ge=0)
    name: str
    notes_markdown: str


class NarrateRequest(ToolRequest):
    slides: list[NarrateSlide]
    lang: LangCode = DEFAULT_LANG
    voice: str | None = Field(
        default=None,
        description="Edge-TTS ShortName (e.g. ko-KR-SunHiNeural). Falls back to the lang's default.",
    )
    rate: str = Field(default="+0%", description="Edge-TTS speaking rate: -50%..+50%")


class NarrateSlideAudio(ToolResponse):
    index: int
    name: str
    mp3: bytes
    voice_used: str
    spoken_chars: int


class NarrateResponse(ToolResponse):
    audios: list[NarrateSlideAudio]
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def narrate(req: NarrateRequest) -> NarrateResponse:
    """Synthesize speaker notes into MP3 audio (one per slide).

    Synchronous wrapper around the async edge-tts backend so the tool layer
    can be invoked from anywhere. Workers should prefer the async version.
    """
    return asyncio.run(narrate_async(req))


async def narrate_async(req: NarrateRequest) -> NarrateResponse:
    if not req.slides:
        return NarrateResponse(
            audios=[],
            cost=CostBreakdown(),
        )

    voice = req.voice or default_voice_for_locale(req.lang)
    if voice is None:
        raise ValueError(
            f"No default Edge-TTS voice for lang={req.lang!r}; pass `voice` explicitly. "
            f"Known defaults: {sorted(DEFAULT_VOICE_PER_LOCALE)}"
        )

    started = time.perf_counter()
    warnings: list[WarningEntry] = []
    results: list[NarrateSlideAudio] = []

    with temp_workspace(prefix="xgen_edit2docs-narrate-") as ws:
        for slide in sorted(req.slides, key=lambda s: s.index):
            text = spoken_text(slide.notes_markdown).strip()
            if not text:
                warnings.append(
                    WarningEntry(
                        code="empty_notes",
                        message=f"Slide {slide.index} has no narratable content; skipped.",
                    )
                )
                continue
            out_path = ws / f"slide_{slide.index:03d}.mp3"
            await edge_generate(text, out_path, voice=voice, rate=req.rate)
            results.append(
                NarrateSlideAudio(
                    index=slide.index,
                    name=slide.name,
                    mp3=out_path.read_bytes(),
                    voice_used=voice,
                    spoken_chars=len(text),
                )
            )

    return NarrateResponse(
        audios=results,
        cost=CostBreakdown(
            duration_seconds=time.perf_counter() - started,
            audio_seconds=sum(a.spoken_chars for a in results) / 15.0,  # ~15 chars/sec heuristic
        ),
        warnings=warnings,
    )
