"""SQLAlchemy ORM models for xgen_edit2docs.

Schema from ppt-master-analysis/04-integration-plan.md §4.6. Every table is
keyed on UUID, every row carries a `tenant_id` for multi-tenant isolation,
and `assets.original_filename` is kept separate from `assets.storage_key`
so Korean filenames flow through unmodified ([06-bilingual-conventions §6.6.1]).

All identifiers (table/column names) are English snake_case (Track A).
Stored values (project name, original_filename, etc.) may contain any
Unicode (Track C).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Portable JSON: JSONB on Postgres (binary + indexed), plain JSON elsewhere
# (SQLite for tests). Same query semantics for our usage.
JsonB = JSON().with_variant(JSONB(astext_type=Text()), "postgresql")

# Portable UUID: native uuid type on Postgres, CHAR(32) on SQLite. as_uuid=True
# means values are python uuid.UUID on both sides.
UUIDType = Uuid(as_uuid=True)


class Base(DeclarativeBase):
    """Shared declarative base. All models import from here."""

    metadata_naming_convention = {
        # Alembic friendly: deterministic constraint names so autogenerate diffs cleanly.
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TenantStatus(str, enum.Enum):
    active = "active"
    suspended = "suspended"


class AssetKind(str, enum.Enum):
    source = "source"
    docx = "docx"
    xlsx = "xlsx"
    markdown = "markdown"
    spec_lock = "spec_lock"
    svg = "svg"
    image = "image"
    pptx = "pptx"
    audio = "audio"
    preview = "preview"


class JobKind(str, enum.Enum):
    generate_deck = "generate_deck"
    edit_deck = "edit_deck"
    convert = "convert"
    strategize = "strategize"
    execute = "execute"
    export = "export"
    narrate = "narrate"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


class JobEventType(str, enum.Enum):
    progress = "progress"
    stage = "stage"
    page_done = "page_done"
    log = "log"
    error = "error"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    status: Mapped[TenantStatus] = mapped_column(
        Enum(TenantStatus, name="tenant_status"), nullable=False, default=TenantStatus.active
    )
    # Encrypted BYOK key map: {"anthropic": "<ciphertext>", "openai": "...", ...}
    byok_encrypted: Mapped[dict | None] = mapped_column(JsonB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    assets: Mapped[list["Asset"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    jobs: Mapped[list["Job"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # Plain prefix for log correlation (first ~12 chars of the full key).
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    # bcrypt/argon2 hash of the full key. Never store the plaintext key.
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="api_keys")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    lang: Mapped[str] = mapped_column(String(16), nullable=False, default="en-US")
    template_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    style: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="projects")
    assets: Mapped[list["Asset"]] = relationship(back_populates="project")
    jobs: Mapped[list["Job"]] = relationship(back_populates="project")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[AssetKind] = mapped_column(Enum(AssetKind, name="asset_kind"), nullable=False)

    # Original filename as the user uploaded it. May contain Korean / any Unicode (Track C).
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Object-storage key. ASCII only (Track A), e.g. "tenants/<tid>/sources/<ulid>.pdf"
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)

    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="assets")
    project: Mapped[Project | None] = relationship(back_populates="assets")

    __table_args__ = (
        Index("ix_assets_tenant_id_kind", "tenant_id", "kind"),
        Index("ix_assets_tenant_id_project_id", "tenant_id", "project_id"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind, name="job_kind"), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.queued
    )
    # Request payload echoed back for replay / debugging. Excludes secrets (api keys
    # are passed through transient headers, never persisted on a job row).
    params: Mapped[dict] = mapped_column(JsonB, nullable=False, default=dict)
    # Stage-by-stage cost rollup (input/output tokens, image counts, audio seconds).
    cost: Mapped[dict] = mapped_column(JsonB, nullable=False, default=dict)
    # Job result asset refs (pptx_asset_id, preview_asset_ids, etc.).
    result: Mapped[dict] = mapped_column(JsonB, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="jobs")
    project: Mapped[Project | None] = relationship(back_populates="jobs")
    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobEvent.created_at"
    )

    __table_args__ = (
        Index("ix_jobs_tenant_id_status", "tenant_id", "status"),
        Index("ix_jobs_tenant_id_kind", "tenant_id", "kind"),
        Index("ix_jobs_created_at", "created_at"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"

    id: Mapped[uuid.UUID] = mapped_column(UUIDType, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[JobEventType] = mapped_column(
        Enum(JobEventType, name="job_event_type"), nullable=False
    )
    payload: Mapped[dict] = mapped_column(JsonB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="events")

    __table_args__ = (
        # Fast SSE replay: events for a job ordered by time.
        Index("ix_job_events_job_id_created_at", "job_id", "created_at"),
    )


__all__ = [
    "Base",
    "Tenant",
    "TenantStatus",
    "ApiKey",
    "Project",
    "Asset",
    "AssetKind",
    "Job",
    "JobKind",
    "JobStatus",
    "JobEvent",
    "JobEventType",
]
