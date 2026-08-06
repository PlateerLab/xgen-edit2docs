"""Image acquisition tool: generate via AI backend or search the web.

Wraps the engine's image_backends/ and image_sources/ packages. Each backend
reads its API key from process env vars (per ppt-master's design); we honor
BYOK by setting the env vars from the request just-in-time and restoring them
after the call.

For M2 we expose the two most-used generation backends (gemini, openai) and
the two most-used search providers (pexels, pixabay). Other backends are easy
to add — they all conform to the same `.generate()` / `.search()` interfaces.
"""

from __future__ import annotations

import importlib
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import Field

from ._workspace import temp_workspace
from .types import CostBreakdown, ToolRequest, ToolResponse, WarningEntry


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------


class GenerateImageRequest(ToolRequest):
    prompt: str
    backend: str = Field(default="openai", description="Backend module name (gemini, openai, ...).")
    aspect_ratio: str = "16:9"
    image_size: str = Field(default="1K", description="Backend-specific size (1K / 2K / etc.).")
    model: str | None = None
    api_keys: dict[str, str] = Field(
        default_factory=dict,
        description="BYOK env var map, e.g. {'OPENAI_API_KEY': 'sk-...'}.",
    )


class GenerateImageResponse(ToolResponse):
    image: bytes
    mime_type: str
    backend_used: str
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def generate_image(req: GenerateImageRequest) -> GenerateImageResponse:
    """Synchronously generate one image. Raises on backend errors."""
    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    module_name = f"xgen_edit2docs.core.image_backends.backend_{req.backend}"
    try:
        backend = importlib.import_module(module_name)
    except ImportError as exc:
        raise ValueError(
            f"Unknown image backend {req.backend!r}. Available: "
            f"{_list_available('image_backends', prefix='backend_')}"
        ) from exc

    if not hasattr(backend, "generate"):
        raise RuntimeError(f"Backend {req.backend!r} does not expose generate()")

    with temp_workspace(prefix=f"xgen_edit2docs-img-gen-{req.backend}-") as ws, _env_overlay(req.api_keys):
        path_str = backend.generate(
            prompt=req.prompt,
            aspect_ratio=req.aspect_ratio,
            image_size=req.image_size,
            output_dir=str(ws),
            filename="image",
            model=req.model,
        )
        if not path_str:
            raise RuntimeError(f"Backend {req.backend!r} returned no path")
        path = Path(path_str)
        if not path.is_absolute():
            path = ws / path
        if not path.exists():
            # Backends sometimes ignore `filename` and pick their own; find the artifact.
            produced = list(ws.iterdir())
            if not produced:
                raise RuntimeError(f"Backend {req.backend!r} produced no output file")
            path = produced[0]
            warnings.append(
                WarningEntry(
                    code="backend_renamed_output",
                    message=f"Backend {req.backend!r} wrote {path.name} instead of the requested name.",
                )
            )

        return GenerateImageResponse(
            image=path.read_bytes(),
            mime_type=_guess_mime(path),
            backend_used=req.backend,
            cost=CostBreakdown(
                image_count=1,
                duration_seconds=time.perf_counter() - started,
            ),
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchImageRequest(ToolRequest):
    query: str
    providers: list[str] = Field(default_factory=lambda: ["pexels", "pixabay"])
    aspect_ratio: str = "16:9"
    api_keys: dict[str, str] = Field(default_factory=dict)
    strict_no_attribution: bool = False


class SearchImageResponse(ToolResponse):
    image: bytes
    mime_type: str
    source_url: str | None
    license: str | None
    attribution: str | None
    provider_used: str
    cost: CostBreakdown
    warnings: list[WarningEntry] = Field(default_factory=list)


def search_image(req: SearchImageRequest) -> SearchImageResponse:
    """Find and download a web image. Returns the first acceptable candidate."""
    from ..core.image_search import ImageSearchRequest, search_and_download

    started = time.perf_counter()
    warnings: list[WarningEntry] = []

    with temp_workspace(prefix="xgen_edit2docs-img-search-") as ws, _env_overlay(req.api_keys):
        output_path = ws / "image"
        candidate, provider, _stage = search_and_download(
            providers=req.providers,
            request=ImageSearchRequest(query=req.query, aspect=req.aspect_ratio),
            output_path=output_path,
            strict_no_attribution=req.strict_no_attribution,
            save_candidates=False,
        )
        if candidate is None or provider is None:
            raise RuntimeError(
                f"No image found for query={req.query!r} across providers={req.providers}"
            )

        # search_and_download writes the file to a path derived from output_path's stem.
        # Find the actual file it wrote.
        produced = [p for p in ws.iterdir() if p.is_file()]
        if not produced:
            raise RuntimeError("image_search reported success but produced no file")
        # Prefer the largest file (the downloaded image, not metadata json).
        produced.sort(key=lambda p: p.stat().st_size, reverse=True)
        image_path = produced[0]

        return SearchImageResponse(
            image=image_path.read_bytes(),
            mime_type=_guess_mime(image_path),
            source_url=getattr(candidate, "source_url", None),
            license=getattr(candidate, "license_label", None),
            attribution=getattr(candidate, "attribution", None),
            provider_used=provider,
            cost=CostBreakdown(
                image_count=1,
                duration_seconds=time.perf_counter() - started,
            ),
            warnings=warnings,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _env_overlay(overrides: dict[str, str]) -> Iterator[None]:
    """Temporarily set env vars; restore on exit (BYOK key isolation)."""
    saved: dict[str, str | None] = {key: os.environ.get(key) for key in overrides}
    for key, value in overrides.items():
        if value:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, original in saved.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def _guess_mime(path: Path) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")


def _list_available(subpkg: str, prefix: str) -> list[str]:
    pkg_path = Path(__file__).resolve().parent.parent / "core" / subpkg
    if not pkg_path.exists():
        return []
    return sorted(
        p.stem.removeprefix(prefix) for p in pkg_path.glob(f"{prefix}*.py") if p.stem != f"{prefix}common"
    )
