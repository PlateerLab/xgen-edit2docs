"""Process-scoped context the MCP tools share.

The MCP server is a long-running process; each tool call needs its own DB
session and reaches out to the same object storage. We hide that plumbing
behind `MCPContext.scope()` which yields a fresh AsyncSession + the
resolved tenant + the configured storage adapter.

Tests construct an MCPContext explicitly (with a SQLite engine + InMemoryStorage)
and pass it into build_mcp_server(ctx=...).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..db.models import Tenant
from ..db.session import get_sessionmaker
from ..storage import ObjectStorage, get_default_storage


@dataclass
class MCPContextScope:
    """One unit of work for a single MCP tool call."""

    session: AsyncSession
    tenant: Tenant
    storage: ObjectStorage


@dataclass
class MCPContext:
    """Process-scoped config + dependencies for MCP tools.

    `tenant_resolver` returns the Tenant a tool call should run under. The
    default resolver bootstraps a singleton "dev" tenant matching the REST
    API stub (api/dependencies.py); M6 will introduce real per-key resolution.
    """

    sessionmaker: async_sessionmaker[AsyncSession] | None = None
    storage: ObjectStorage | None = None
    _resolved_tenant: Tenant | None = field(default=None, repr=False)

    def _maker(self) -> async_sessionmaker[AsyncSession]:
        return self.sessionmaker or get_sessionmaker()

    def _storage(self) -> ObjectStorage:
        return self.storage or get_default_storage()

    @asynccontextmanager
    async def scope(self) -> AsyncIterator[MCPContextScope]:
        """Yield a session + tenant + storage, committing on clean exit."""
        async with self._maker()() as session:
            tenant = await self._ensure_dev_tenant(session)
            try:
                yield MCPContextScope(session=session, tenant=tenant, storage=self._storage())
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _ensure_dev_tenant(self, session: AsyncSession) -> Tenant:
        if self._resolved_tenant is not None:
            return self._resolved_tenant
        from sqlalchemy import select

        stmt = select(Tenant).where(Tenant.email == "dev@xgen_edit2docs.local")
        existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            existing = Tenant(name="dev", email="dev@xgen_edit2docs.local")
            session.add(existing)
            await session.flush()
        self._resolved_tenant = existing
        return existing


_default_context: MCPContext | None = None


def set_default_context(ctx: MCPContext | None) -> None:
    """Tests / dev entry points: install a process-wide MCPContext."""
    global _default_context
    _default_context = ctx


def get_default_context() -> MCPContext:
    if _default_context is None:
        # Production default: derive sessionmaker + storage from settings.
        return MCPContext()
    return _default_context
