"""FastAPI application root.

Wires the lifespan (auto-bootstrap on first boot), routers, and bilingual
error handlers.

Routes:
  GET  /health                       — liveness probe
  GET  /v1/locales                   — supported message catalog locales
  GET  /v1/messages/sample           — i18n smoke test
  POST /v1/assets                    — multipart upload
  POST /v1/assets/presigned          — presigned PUT URL
  GET  /v1/assets/{id}               — metadata
  GET  /v1/assets/{id}/download      — presigned GET URL (Korean filename safe)
  DELETE /v1/assets/{id}             — delete
  POST /v1/jobs/generate-deck        — enqueue + (inline mode) kick off
  GET  /v1/jobs/{id}                 — job status
  GET  /v1/jobs/{id}/events          — SSE event stream
  GET  /v1/raw/{key}                 — LocalFilesystemStorage download endpoint

Lifespan also picks the queue backend:
- `EDIT2DOCS_REDIS_URL` set → arq pool (worker process polls Redis).
- Otherwise → inline asyncio mode (`asyncio.create_task` per submitted job).

See ppt-master-analysis/04-integration-plan.md for the layered architecture
and ppt-master-analysis/05-roadmap.md for the milestone breakdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..config import get_settings
from ..i18n import default_catalog
from ..mcp.http_transport import mount_mcp
from ..services.bootstrap import bootstrap
from .dependencies import Catalog, RequestLocale
from .errors import install_error_handlers
from .routes import assets as assets_routes
from .routes import jobs as jobs_routes
from .routes import preview as preview_routes
from .routes import text_edits as text_edits_routes
from .routes import raw as raw_routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info(
        "xgen_edit2docs starting (env=%s, lang=%s, db=%s, storage=%s, queue=%s)",
        settings.environment,
        settings.default_lang,
        "postgres" if settings.uses_postgres else "sqlite",
        "s3" if settings.uses_s3_storage else "local-fs",
        "arq" if settings.uses_redis_queue else "inline",
    )

    # Auto-bootstrap: data dir, database schema, S3 bucket. Idempotent.
    await bootstrap(settings)

    catalog = default_catalog()
    logger.info("loaded i18n locales: %s", catalog.supported_locales())

    # Queue mode resolution. We materialize the arq pool when Redis is
    # configured; otherwise jobs run inline via the route handler.
    app.state.arq_pool = None
    if settings.uses_redis_queue:
        try:
            from arq import create_pool

            from ..services.jobs import arq_redis_settings

            app.state.arq_pool = await create_pool(arq_redis_settings(settings))
            logger.info("queue: arq pool ready (Redis at %s)", settings.redis_url)
        except Exception as exc:
            logger.warning(
                "queue: arq pool failed (%s) — falling back to inline mode", exc
            )
            app.state.arq_pool = None

    # JobBus selection. In inline mode there is exactly one process producing
    # and consuming events, so an in-memory bus is the right shape — no
    # Redis required. In arq mode, leave the default (RedisJobBus) alone.
    if app.state.arq_pool is None:
        from ..services.jobs import FakeJobBus, set_default_bus

        set_default_bus(FakeJobBus())

    yield

    if app.state.arq_pool is not None:
        await app.state.arq_pool.close()
    logger.info("xgen_edit2docs shutting down")


app = FastAPI(
    title="xgen_edit2docs",
    description=(
        "AI-agent-native PPT generation server. Korean-language-first, "
        "built on top of ppt-master."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

install_error_handlers(app)
app.include_router(assets_routes.router)
app.include_router(jobs_routes.router)
app.include_router(preview_routes.router)
app.include_router(text_edits_routes.router)
app.include_router(raw_routes.router)

# MCP transports — mounted at /mcp (Streamable HTTP) and /mcp-sse (SSE).
mount_mcp(app)


# ---------------------------------------------------------------------------
# Meta routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Liveness + mode probe."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": "xgen_edit2docs",
        "mode": {
            "database": "postgres" if settings.uses_postgres else "sqlite",
            "storage": "s3" if settings.uses_s3_storage else "local-fs",
            "queue": "arq" if settings.uses_redis_queue else "inline",
        },
    }


@app.get("/v1/locales", tags=["meta"])
async def list_locales(catalog: Catalog) -> dict:
    return {"locales": catalog.supported_locales()}


@app.get("/v1/messages/sample", tags=["meta"])
async def sample_message(locale: RequestLocale, catalog: Catalog) -> dict:
    return {
        "locale": locale,
        "stage_message": catalog.get("stages.executing_page", locale, page=3, total=10),
        "stage_message_en": catalog.get("stages.executing_page", "en-US", page=3, total=10),
        "error_example": catalog.get(
            "errors.invalid_source_format", locale, format="rtf", allowed="pdf, docx, pptx, xlsx"
        ),
    }
