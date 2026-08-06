"""Shared types for xgen_edit2docs tool functions.

These models define the input/output contracts for the Layer 2 tool functions.
They are stateless: every value the tool needs comes in via the request model
(bytes, strings, ids); every artifact the tool produces goes out via the
response model (bytes, strings, ids).

See ppt-master-analysis/04-integration-plan.md §4.3 (Tool layer).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# BCP-47 locales the engine knows how to render. New ones are added as we
# expand prompt coverage (see core/prompts/*.{en,ko,zh,ja}.md).
# English-first: en-US is the engine default; Korean stays a first-class
# citizen (full ko message catalog, Hangul-aware layout, ko fonts/voices).
# Pass lang="ko-KR" or set EDIT2DOCS_DEFAULT_LANG=ko-KR to flip a deployment.
LangCode = Literal["ko-KR", "en-US", "zh-CN", "zh-TW", "ja-JP"]
DEFAULT_LANG: LangCode = "en-US"

# Source document formats the converters understand.
SourceFormat = Literal["pdf", "docx", "doc", "pptx", "xlsx", "xlsm", "html", "epub", "ipynb", "url"]

# Slide canvas formats — keys match core.project_utils.CANVAS_FORMATS.
CanvasFormat = Literal[
    "ppt169", "ppt43",
    "xhs", "xhs34",  # Xiaohongshu
    "story",          # 9:16 vertical
    "wechat",
]
DEFAULT_CANVAS: CanvasFormat = "ppt169"


class ToolRequest(BaseModel):
    """Base for all tool inputs. Forbids extra fields to catch typos."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolResponse(BaseModel):
    """Base for all tool outputs."""

    model_config = ConfigDict(extra="forbid")


class CostBreakdown(ToolResponse):
    """Per-tool cost ledger entry, accumulated by the orchestrator."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    image_count: int = 0
    audio_seconds: float = 0.0
    duration_seconds: float = 0.0


class WarningEntry(ToolResponse):
    """Non-fatal issue surfaced from a tool."""

    code: str  # English, stable, e.g. "missing_optional_dependency"
    message: str  # English; UI layers translate via i18n catalog
    detail: dict | None = None


class QualityIssue(ToolResponse):
    """One finding from the SVG quality check."""

    page_index: int | None = None  # None = whole-deck issue
    severity: Literal["error", "warning", "info"]
    code: str  # e.g. "viewbox_mismatch"
    message: str
    location: str | None = None


class FontStack(ToolResponse):
    """Resolved per-language font stack used when no spec_lock override exists."""

    lang: LangCode
    stack: str = Field(..., description="CSS-style font-family string")
