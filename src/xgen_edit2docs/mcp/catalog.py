"""Static catalogs surfaced by MCP tools.

For M4.1 we expose two read-only catalogs that don't require any database
or LLM access: the layout template list and the Edge-TTS voice list. They
make the MCP server immediately useful as a discovery surface even before
the heavier tools (upload_source, generate_deck) are wired up.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ..core.tts_backends.backend_edge import COMMON_VOICES, DEFAULT_VOICE_PER_LOCALE
from ..i18n import normalize_locale

LAYOUTS_INDEX_PATH = (
    Path(__file__).resolve().parent.parent
    / "core"
    / "templates"
    / "layouts"
    / "layouts_index.json"
)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_layouts_index() -> dict[str, dict]:
    if not LAYOUTS_INDEX_PATH.exists():
        return {}
    return json.loads(LAYOUTS_INDEX_PATH.read_text(encoding="utf-8"))


def list_templates(*, locale: str = "en-US") -> list[dict]:
    """Return the template catalog as a list of plain dicts (MCP-serializable)."""
    locale = normalize_locale(locale)
    items: list[dict] = []
    for name, meta in _load_layouts_index().items():
        items.append(
            {
                "name": name,
                "summary": meta.get("summary", ""),
                "keywords": list(meta.get("keywords", [])),
            }
        )
    items.sort(key=lambda item: item["name"])
    return items


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


def list_voices(*, lang: str | None = None) -> list[dict]:
    """Return curated Edge-TTS voices, optionally filtered by locale prefix.

    Each entry: { locale, voice_id, notes, is_default_for_locale }.
    """
    rows: list[dict] = []
    for locale, voice_id, notes in COMMON_VOICES:
        if lang and not locale.lower().startswith(lang.lower().split("-")[0]):
            continue
        rows.append(
            {
                "locale": locale,
                "voice_id": voice_id,
                "notes": notes,
                "is_default_for_locale": DEFAULT_VOICE_PER_LOCALE.get(locale) == voice_id,
            }
        )
    return rows
