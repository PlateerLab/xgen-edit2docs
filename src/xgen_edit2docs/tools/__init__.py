"""xgen_edit2docs Layer 2 tool functions (stateless, in-process).

See ppt-master-analysis/04-integration-plan.md §4.3 for the layer model.
Each tool takes a Pydantic request and returns a Pydantic response; no
disk paths leak across the boundary.

The async tools (strategize, execute, generate_deck, narrate_async) require
an asyncio event loop. The sync ones (convert, export, quality, narrate)
work anywhere.
"""

from .audio import (
    NarrateRequest,
    NarrateResponse,
    NarrateSlide,
    NarrateSlideAudio,
    narrate,
    narrate_async,
)
from .analyze_template import (
    AnalyzeTemplateRequest,
    AnalyzeTemplateResponse,
    analyze_template,
)
from .apply_text_edits import (
    ApplyTextEditsRequest,
    ApplyTextEditsResponse,
    TextEdit,
    apply_text_edits,
)
from .convert import ConvertRequest, ConvertResponse, convert_to_markdown
from .edit_deck import ChatTurn, EditDeckRequest, EditDeckResponse, edit_deck
from .execute import (
    ExecuteBatchRequest,
    ExecuteBatchResponse,
    ExecutePageRequest,
    ExecutePageResponse,
    ExecutorImage,
    execute_batch,
    execute_page,
)
from .export import ExportRequest, ExportResponse, SlideInput, export_pptx
from .generate_deck import (
    GenerateDeckRequest,
    GenerateDeckResponse,
    StageEvent,
    generate_deck,
)
from .images import (
    GenerateImageRequest,
    GenerateImageResponse,
    SearchImageRequest,
    SearchImageResponse,
    generate_image,
    search_image,
)
from .render_preview import (
    RenderPreviewRequest,
    RenderPreviewResponse,
    SlidePreview,
    render_preview,
)
from .quality import (
    QualityCheckRequest,
    QualityCheckResponse,
    QualitySlide,
    check_svg_quality,
)
from .strategize import StrategizeRequest, StrategizeResponse, strategize
from .types import (
    CanvasFormat,
    CostBreakdown,
    DEFAULT_CANVAS,
    DEFAULT_LANG,
    LangCode,
    QualityIssue,
    SourceFormat,
    WarningEntry,
)

__all__ = [
    # types
    "CanvasFormat",
    "CostBreakdown",
    "DEFAULT_CANVAS",
    "DEFAULT_LANG",
    "LangCode",
    "QualityIssue",
    "SourceFormat",
    "WarningEntry",
    # analyze_template
    "AnalyzeTemplateRequest",
    "AnalyzeTemplateResponse",
    "analyze_template",
    # apply_text_edits
    "ApplyTextEditsRequest",
    "ApplyTextEditsResponse",
    "TextEdit",
    "apply_text_edits",
    # edit_deck
    "ChatTurn",
    "EditDeckRequest",
    "EditDeckResponse",
    "edit_deck",
    # render_preview
    "RenderPreviewRequest",
    "RenderPreviewResponse",
    "SlidePreview",
    "render_preview",
    # convert
    "ConvertRequest",
    "ConvertResponse",
    "convert_to_markdown",
    # strategize
    "StrategizeRequest",
    "StrategizeResponse",
    "strategize",
    # execute
    "ExecutePageRequest",
    "ExecutePageResponse",
    "ExecuteBatchRequest",
    "ExecuteBatchResponse",
    "ExecutorImage",
    "execute_page",
    "execute_batch",
    # images
    "GenerateImageRequest",
    "GenerateImageResponse",
    "generate_image",
    "SearchImageRequest",
    "SearchImageResponse",
    "search_image",
    # quality
    "QualityCheckRequest",
    "QualityCheckResponse",
    "QualitySlide",
    "check_svg_quality",
    # export
    "ExportRequest",
    "ExportResponse",
    "SlideInput",
    "export_pptx",
    # audio
    "NarrateRequest",
    "NarrateResponse",
    "NarrateSlide",
    "NarrateSlideAudio",
    "narrate",
    "narrate_async",
    # orchestrator
    "GenerateDeckRequest",
    "GenerateDeckResponse",
    "StageEvent",
    "generate_deck",
]
