"""Parser for image plans emitted in spec_lock.yaml.

The Strategist may declare per-page image needs in `spec_lock.yaml`. We
accept two shapes and normalize to a single list:

Shape A — flat top-level list:
    images:
      - page_index: 0
        placeholder: hero_cover
        mode: generate
        prompt: Modern Korean office tower at sunset, photographic
        aspect_ratio: 16:9
        backend: openai

Shape B — nested under pages:
    pages:
      - id: cover
        images:
          - placeholder: hero
            mode: search
            query: Seoul skyline night
            providers: [pexels, pixabay]

Anything missing / unparseable / unrecognized: skipped silently with a
warning. The pipeline keeps running with whatever it could resolve.
"""

from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# YAML 1.1 parses unquoted `aspect_ratio: 16:9` as the sexagesimal int
# `16*60 + 9 = 969`. PyYAML bakes this into the int resolver as one
# combined regex; we cannot cleanly remove just the sexagesimal arm. So we
# recover the original ratio at the Pydantic level using this reverse map
# of the ratios decks actually use. Strategist authors should still quote
# `aspect_ratio` strings, but tolerance protects deck generation when they
# don't.
_SEXAGESIMAL_ASPECT_REVERSE = {
    1 * 60 + 1: "1:1",
    16 * 60 + 9: "16:9",
    4 * 60 + 3: "4:3",
    9 * 60 + 16: "9:16",
    3 * 60 + 4: "3:4",
    2 * 60 + 3: "2:3",
    3 * 60 + 2: "3:2",
    21 * 60 + 9: "21:9",
}

ImageMode = Literal["generate", "search"]


class ImagePlanItem(BaseModel):
    """One image the deck needs. Lives on `page_index`, identified by `placeholder`."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    page_index: int = Field(..., ge=0)
    placeholder: str = Field(..., min_length=1, max_length=64)
    mode: ImageMode

    # Generation parameters (mode=generate)
    prompt: str | None = None
    backend: str | None = None  # e.g. "openai", "gemini"; defaults at acquisition time

    # Search parameters (mode=search)
    query: str | None = None
    providers: list[str] | None = None  # e.g. ["pexels", "pixabay"]

    # Common
    aspect_ratio: str = "16:9"
    description: str | None = None

    @field_validator("aspect_ratio", mode="before")
    @classmethod
    def _coerce_aspect_ratio(cls, value):
        # Recover from YAML 1.1 sexagesimal ints (`16:9` -> 969).
        if isinstance(value, int):
            return _SEXAGESIMAL_ASPECT_REVERSE.get(value, str(value))
        return value

    @field_validator("placeholder")
    @classmethod
    def _ascii_placeholder(cls, value: str) -> str:
        """Track A — placeholders flow into SVG <image href="<placeholder>.png">,
        which we save as a filename inside the workspace. Must be ASCII."""
        try:
            value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"placeholder must be ASCII (got {value!r}); "
                "Korean filenames belong only in `original_filename` on assets."
            ) from exc
        # Also enforce filesystem-safe characters.
        for ch in value:
            if not (ch.isalnum() or ch in "_-"):
                raise ValueError(
                    f"placeholder must use [A-Za-z0-9_-] only (got {value!r})"
                )
        return value


def parse_image_plan(spec_lock_yaml: str) -> list[ImagePlanItem]:
    """Pull every ImagePlanItem out of spec_lock.yaml.

    Tolerates malformed YAML by returning [] rather than raising — the engine
    keeps running with no image plan, and image-bearing slides simply lack
    their hero asset.
    """
    try:
        parsed = yaml.safe_load(spec_lock_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(parsed, dict):
        return []

    items: list[ImagePlanItem] = []
    seen_keys: set[tuple[int, str]] = set()

    # Shape A: top-level `images:` list.
    for raw in _coerce_list(parsed.get("images")):
        item = _safe_parse(raw)
        if item is not None and (item.page_index, item.placeholder) not in seen_keys:
            items.append(item)
            seen_keys.add((item.page_index, item.placeholder))

    # Shape B: per-page `images:` under each `pages[*]`.
    pages = parsed.get("pages")
    if isinstance(pages, list):
        for page_idx, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            for raw in _coerce_list(page.get("images")):
                if isinstance(raw, dict) and "page_index" not in raw:
                    raw = {**raw, "page_index": page_idx}
                item = _safe_parse(raw)
                if item is not None and (item.page_index, item.placeholder) not in seen_keys:
                    items.append(item)
                    seen_keys.add((item.page_index, item.placeholder))

    return items


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _coerce_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _safe_parse(raw: object) -> ImagePlanItem | None:
    if not isinstance(raw, dict):
        return None
    try:
        return ImagePlanItem.model_validate(raw)
    except Exception:
        return None
