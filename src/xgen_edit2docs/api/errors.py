"""Centralized exception handlers + the bilingual error envelope.

Every domain-level error inherits from `BusinessError` (defined in services/)
or a route-local helper, carries an English `code` + an i18n `message_key`,
and renders to:

    {
      "error": {
        "code": "ASSET_NOT_FOUND",
        "message": "Asset ... was not found or has expired.",   # request locale
        "message_en": "Asset ... was not found or has expired.",
        "details": {...}
      }
    }
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..i18n import default_catalog, normalize_locale
from ..services.assets import AssetError

logger = logging.getLogger(__name__)


def _resolve_locale(request: Request) -> str:
    accept = request.headers.get("Accept-Language", "")
    primary = accept.split(",")[0].split(";")[0].strip() if accept else ""
    return normalize_locale(primary)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AssetError)
    async def _asset_error(request: Request, exc: AssetError) -> JSONResponse:
        catalog = default_catalog()
        locale = _resolve_locale(request)
        message: str
        message_en: str
        if exc.message_key:
            message = catalog.get(exc.message_key, locale, **exc.vars)
            message_en = catalog.get(exc.message_key, "en-US", **exc.vars)
        else:
            message = str(exc)
            message_en = str(exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": message,
                    "message_en": message_en,
                    "details": exc.vars,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        # When handlers raise HTTPException(detail={...}), preserve that shape.
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            body: dict[str, Any] = {"error": detail}
        else:
            body = {
                "error": {
                    "code": _starlette_code(exc.status_code),
                    "message": str(detail) if detail else "HTTP error",
                    "message_en": str(detail) if detail else "HTTP error",
                }
            }
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": bilingual_detail(
                    "VALIDATION_ERROR",
                    en="Request validation failed.",
                    ko="요청을 검증할 수 없습니다.",
                    locale=_resolve_locale(request),
                    details={"errors": exc.errors()},
                )
            },
        )


def _starlette_code(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        413: "PAYLOAD_TOO_LARGE",
        429: "RATE_LIMITED",
        500: "INTERNAL_ERROR",
        502: "BAD_GATEWAY",
        503: "SERVICE_UNAVAILABLE",
    }.get(status_code, "HTTP_ERROR")


def bilingual_detail(
    code: str, *, en: str, ko: str, locale: str = "en-US", **extra: Any
) -> dict[str, Any]:
    """Inline-route error detail with locale-resolved primary message.

    English-first: ``message`` is English unless the request's
    Accept-Language resolved to Korean. ``message_en`` (and the additive
    ``message_ko``) always carry both fixed forms so clients can localize
    themselves regardless of what ``message`` picked.
    """
    message = ko if (locale or "").lower().startswith("ko") else en
    return {"code": code, "message": message, "message_en": en, "message_ko": ko, **extra}
