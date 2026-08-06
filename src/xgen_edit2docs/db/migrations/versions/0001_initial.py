"""Initial schema: tenants, api_keys, projects, assets, jobs, job_events.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-13

Implements the data model from
ppt-master-analysis/04-integration-plan.md §4.6. All Postgres-specific types
(JSONB, UUID, ENUM) are used directly; SQLite tests use the SQLAlchemy
metadata directly without going through Alembic.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", name="tenant_status"),
            nullable=False,
            server_default="active",
        ),
        sa.Column("byok_encrypted", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("email", name="uq_tenants_email"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_prefix", sa.String(32), nullable=False),
        sa.Column("key_hash", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
    )

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("lang", sa.String(16), nullable=False, server_default="ko-KR"),
        sa.Column("template_name", sa.String(255), nullable=True),
        sa.Column("style", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "source", "markdown", "spec_lock", "svg", "image", "pptx", "audio", "preview",
                name="asset_kind",
            ),
            nullable=False,
        ),
        sa.Column("original_filename", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.String(1024), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("storage_key", name="uq_assets_storage_key"),
    )
    op.create_index("ix_assets_tenant_id_kind", "assets", ["tenant_id", "kind"])
    op.create_index("ix_assets_tenant_id_project_id", "assets", ["tenant_id", "project_id"])

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "generate_deck", "convert", "strategize", "execute", "export", "narrate",
                name="job_kind",
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "done", "failed", "cancelled", name="job_status"),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "params", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "cost", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "result", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_tenant_id_status", "jobs", ["tenant_id", "status"])
    op.create_index("ix_jobs_tenant_id_kind", "jobs", ["tenant_id", "kind"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "job_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum("progress", "stage", "page_done", "log", "error", name="job_event_type"),
            nullable=False,
        ),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_job_events_job_id_created_at", "job_events", ["job_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_job_events_job_id_created_at", table_name="job_events")
    op.drop_table("job_events")

    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id_kind", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id_status", table_name="jobs")
    op.drop_table("jobs")

    op.drop_index("ix_assets_tenant_id_project_id", table_name="assets")
    op.drop_index("ix_assets_tenant_id_kind", table_name="assets")
    op.drop_table("assets")

    op.drop_table("projects")
    op.drop_table("api_keys")
    op.drop_table("tenants")

    # Drop the enum types Alembic doesn't auto-drop on Postgres.
    op.execute("DROP TYPE IF EXISTS job_event_type")
    op.execute("DROP TYPE IF EXISTS job_status")
    op.execute("DROP TYPE IF EXISTS job_kind")
    op.execute("DROP TYPE IF EXISTS asset_kind")
    op.execute("DROP TYPE IF EXISTS tenant_status")
