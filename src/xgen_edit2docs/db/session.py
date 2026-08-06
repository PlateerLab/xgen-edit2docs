"""Async SQLAlchemy engine + session factory for xgen_edit2docs.

The engine is created lazily on first use so that test code can configure a
different DATABASE_URL (typically pointing at a temporary SQLite or Postgres)
before the engine is materialized.
"""

from __future__ import annotations

from functools import lru_cache
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.debug,
        pool_pre_ping=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields an AsyncSession, closes it after the request."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session


def reset_engine_cache() -> None:
    """Drop the cached engine + sessionmaker. Used by tests that swap DSNs."""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
