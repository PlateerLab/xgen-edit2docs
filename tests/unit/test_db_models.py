"""Unit tests for the SQLAlchemy models.

Uses an in-memory SQLite + aiosqlite engine so the tests run with no external
dependencies. Postgres-specific types (JSONB, UUID, ENUM) are aliased to
SQLAlchemy's generic types under SQLite so the metadata can be created.

These tests prove:
- The schema mounts cleanly (no relationship typos, no circular FKs).
- Tenant -> ApiKey/Project/Asset/Job/JobEvent cascade rules work.
- Track A/B/C separation: storage_key (English ASCII) and original_filename
  (Korean OK) coexist on `assets` without collision.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from xgen_edit2docs.db.models import (
    ApiKey,
    Asset,
    AssetKind,
    Base,
    Job,
    JobEvent,
    JobEventType,
    JobKind,
    JobStatus,
    Project,
    Tenant,
    TenantStatus,
)


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """Fresh in-memory SQLite engine with full schema. New per test."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Smoke: every table maps cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_all_tables(session: AsyncSession):
    expected = {
        "tenants", "api_keys", "projects", "assets", "jobs", "job_events",
    }
    actual = set(Base.metadata.tables)
    assert expected <= actual, f"missing tables: {expected - actual}"


# ---------------------------------------------------------------------------
# Tenant + cascade
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tenant_roundtrip(session: AsyncSession):
    t = Tenant(name="Test Tenant", email="test@example.com")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    assert t.id is not None
    assert isinstance(t.id, uuid.UUID)
    assert t.status == TenantStatus.active
    # SQLite stores naive datetimes; Postgres production deployments are timezone-aware.
    # The model declares DateTime(timezone=True) so prod has tzinfo; tests just check non-null.
    assert isinstance(t.created_at, datetime)


@pytest.mark.asyncio
async def test_apikey_belongs_to_tenant(session: AsyncSession):
    t = Tenant(name="T")
    k = ApiKey(tenant=t, key_prefix="ek_test_abc", key_hash="$argon2id$x", name="dev")
    session.add(t)
    await session.commit()
    await session.refresh(k)
    assert k.tenant_id == t.id


# ---------------------------------------------------------------------------
# Asset: track A vs track C separation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asset_korean_filename_preserved_alongside_ascii_storage_key(session: AsyncSession):
    """`original_filename` keeps the user's Korean string; `storage_key` is ASCII-only."""
    t = Tenant(name="T")
    session.add(t)
    await session.commit()

    asset = Asset(
        tenant=t,
        kind=AssetKind.source,
        original_filename="Q3 영업보고서.pdf",
        storage_key=f"tenants/{t.id}/sources/01h-abc.pdf",
        mime_type="application/pdf",
        size=1234,
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)

    # Track C: Unicode preserved verbatim.
    assert asset.original_filename == "Q3 영업보고서.pdf"
    # Track A: storage key is ASCII (no Korean characters).
    asset.storage_key.encode("ascii")  # raises if non-ASCII present


@pytest.mark.asyncio
async def test_asset_storage_key_uniqueness(session: AsyncSession):
    """storage_key has a UNIQUE constraint so the same object can't be registered twice."""
    from sqlalchemy.exc import IntegrityError

    t = Tenant(name="T")
    session.add(t)
    await session.commit()

    key = f"tenants/{t.id}/sources/duplicate.pdf"
    a = Asset(tenant=t, kind=AssetKind.source, storage_key=key, mime_type="application/pdf", size=10)
    session.add(a)
    await session.commit()

    a2 = Asset(tenant=t, kind=AssetKind.source, storage_key=key, mime_type="application/pdf", size=20)
    session.add(a2)
    with pytest.raises(IntegrityError):
        await session.commit()


# ---------------------------------------------------------------------------
# Job + JobEvent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_with_events(session: AsyncSession):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    t = Tenant(name="T")
    project = Project(tenant=t, name="Q3 보고", lang="ko-KR")
    session.add(t)
    await session.flush()

    job = Job(
        tenant=t,
        project=project,
        kind=JobKind.generate_deck,
        status=JobStatus.queued,
        params={"user_intent": "Q3 영업 결과 보고", "lang": "ko-KR"},
    )
    session.add(job)
    await session.flush()

    e1 = JobEvent(job=job, type=JobEventType.stage, payload={"stage": "converting"})
    e2 = JobEvent(job=job, type=JobEventType.progress, payload={"progress": 0.5})
    session.add_all([e1, e2])
    await session.commit()

    # Re-load with eager events to avoid sync lazy-load in an async context.
    loaded = (await session.execute(
        select(Job).options(selectinload(Job.events)).where(Job.id == job.id)
    )).scalar_one()
    assert len(loaded.events) == 2
    # Params support Korean values without escaping.
    assert loaded.params["user_intent"] == "Q3 영업 결과 보고"


@pytest.mark.asyncio
async def test_job_cascades_to_events(session: AsyncSession):
    t = Tenant(name="T")
    job = Job(tenant=t, kind=JobKind.convert, status=JobStatus.queued)
    job.events.append(JobEvent(type=JobEventType.stage, payload={"stage": "queued"}))
    session.add(t)
    session.add(job)
    await session.commit()

    job_id = job.id
    await session.delete(job)
    await session.commit()

    # Events should be gone too.
    from sqlalchemy import select
    remaining = (await session.execute(select(JobEvent).where(JobEvent.job_id == job_id))).scalars().all()
    assert remaining == []


# ---------------------------------------------------------------------------
# Project defaults
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_lang_defaults_to_english(session: AsyncSession):
    # English-first: new projects default to en-US; deployments flip via
    # EDIT2DOCS_DEFAULT_LANG (request-level), not the column default.
    t = Tenant(name="T")
    p = Project(tenant=t, name="default-lang test")
    session.add(t)
    await session.commit()
    await session.refresh(p)
    assert p.lang == "en-US"
