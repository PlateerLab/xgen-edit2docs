"""English-first: flip the projects.lang server default from ko-KR to en-US.

Existing rows keep their recorded lang — this only changes what a missing
value means going forward. Deployments that want Korean-by-default set
EDIT2DOCS_DEFAULT_LANG=ko-KR (the request-level default), which is applied
before rows are inserted, so this server default is a last-resort backstop.

Revision ID: 0002_english_first
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_english_first"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:  # batch: SQLite-compatible
        batch.alter_column(
            "lang",
            existing_type=sa.String(16),
            server_default="en-US",
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            "lang",
            existing_type=sa.String(16),
            server_default="ko-KR",
            existing_nullable=False,
        )
