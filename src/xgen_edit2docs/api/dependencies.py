"""Shared FastAPI dependencies (auth, DB session, storage, locale, catalog).

Lifted out of api/main.py so we can reuse them across route modules without
introducing circular imports.
"""

from __future__ import annotations

import uuid
from typing import Annotated, AsyncIterator

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..db.models import Tenant, TenantStatus
from ..db.session import get_sessionmaker
from ..i18n import MessageCatalog, default_catalog, normalize_locale
from ..storage import InMemoryStorage, ObjectStorage

# ---------------------------------------------------------------------------
# DB session — request-scoped
# ---------------------------------------------------------------------------


async def get_db_session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ---------------------------------------------------------------------------
# Storage — process-wide
# ---------------------------------------------------------------------------

_test_storage: ObjectStorage | None = None


def set_test_storage(storage: ObjectStorage | None) -> None:
    """Test hook: forces both the request-scoped dependency AND the
    worker-side get_default_storage() to return *storage*.

    Workers reach storage via `from ..storage import get_default_storage`
    rather than the FastAPI dependency, so the override has to live at
    both layers.
    """
    global _test_storage
    _test_storage = storage
    from ..storage import set_default_storage

    set_default_storage(storage)


def get_object_storage() -> ObjectStorage:
    if _test_storage is not None:
        return _test_storage
    from ..storage import get_default_storage

    return get_default_storage()


# ---------------------------------------------------------------------------
# i18n
# ---------------------------------------------------------------------------


def get_request_locale(
    accept_language: Annotated[str | None, Header(alias="Accept-Language")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = ...,
) -> str:
    if accept_language:
        primary = accept_language.split(",")[0].split(";")[0].strip()
        return normalize_locale(primary, default=settings.default_lang)
    return normalize_locale(settings.default_lang, default=settings.default_lang)


def get_catalog() -> MessageCatalog:
    return default_catalog()


# ---------------------------------------------------------------------------
# Auth — M0 stub: bootstrap dev tenant if dev key configured.
# ---------------------------------------------------------------------------


async def _bootstrap_dev_tenant(session: AsyncSession) -> Tenant:
    """Return (creating if needed) the singleton "dev" tenant.

    Until M6 introduces proper tenant signup, the server runs against one
    well-known tenant so the rest of the API can be exercised end-to-end.
    """
    stmt = select(Tenant).where(Tenant.email == "dev@xgen_edit2docs.local")
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    tenant = Tenant(
        name="dev",
        email="dev@xgen_edit2docs.local",
        status=TenantStatus.active,
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def require_api_key_and_tenant(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    settings: Annotated[Settings, Depends(get_settings)] = ...,
    session: Annotated[AsyncSession, Depends(get_db_session)] = ...,
    catalog: Annotated[MessageCatalog, Depends(get_catalog)] = ...,
    locale: Annotated[str, Depends(get_request_locale)] = ...,
) -> Tenant:
    """Validate the bearer token; return the associated Tenant row.

    M0 stub: a single dev API key (settings.auth_dev_api_key) maps to a
    bootstrapped "dev" tenant. Real per-tenant API keys arrive in M6.
    """
    if not settings.auth_dev_api_key:
        # Auth disabled in pure-dev mode (no key configured) — pretend the
        # caller is the dev tenant so /v1/* endpoints stay reachable.
        return await _bootstrap_dev_tenant(session)

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": catalog.get("errors.unauthorized", locale),
                "message_en": catalog.get("errors.unauthorized", "en-US"),
            },
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token.strip() != settings.auth_dev_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "UNAUTHORIZED",
                "message": catalog.get("errors.unauthorized", locale),
                "message_en": catalog.get("errors.unauthorized", "en-US"),
            },
        )
    return await _bootstrap_dev_tenant(session)


# Type aliases that route signatures use.
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
RequestLocale = Annotated[str, Depends(get_request_locale)]
Catalog = Annotated[MessageCatalog, Depends(get_catalog)]
Storage = Annotated[ObjectStorage, Depends(get_object_storage)]
CurrentTenant = Annotated[Tenant, Depends(require_api_key_and_tenant)]
