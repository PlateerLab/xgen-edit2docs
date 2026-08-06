"""Startup bootstrap — make the engine "just work" without manual setup.

Called from the FastAPI lifespan hook (api/main.py). Idempotent: safe to
run on every container start.

Tasks performed (each guarded so a missing dep doesn't fail the others):

1. Create `settings.data_dir` and `data_dir/storage/` if absent.
2. Ensure the database exists (Postgres: CREATE DATABASE IF NOT EXISTS via
   the admin `postgres` database; SQLite: parent dir).
3. Create the schema with `Base.metadata.create_all` if any table is
   missing. This avoids needing Alembic for the common path; users who
   want migrations can still run `alembic upgrade head` themselves.
4. If S3 is configured (`settings.uses_s3_storage`), HEAD the bucket and
   CreateBucket on miss.

Each step logs success/failure but only step 3 is fatal — a missing
schema means the engine can't talk to the DB at all.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse, urlunparse

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from ..config import Settings, get_settings
from ..db.models import Base

logger = logging.getLogger(__name__)


async def bootstrap(settings: Settings | None = None) -> None:
    """Run every bootstrap step in order. Logs progress; raises on schema fail."""
    settings = settings or get_settings()

    _ensure_data_dirs(settings)
    if settings.uses_postgres:
        await _ensure_postgres_database(settings)
    await _ensure_schema(settings)
    if settings.uses_s3_storage:
        await _ensure_s3_bucket(settings)


# ---------------------------------------------------------------------------

def _ensure_data_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "storage").mkdir(parents=True, exist_ok=True)
    logger.info("bootstrap: data_dir ready at %s", settings.data_dir)


# ---------------------------------------------------------------------------

async def _ensure_postgres_database(settings: Settings) -> None:
    """Connect to the admin `postgres` database and CREATE DATABASE if missing.

    Uses the same credentials as the application URL — operator either has
    CREATEDB privilege on that role or has already created the database
    by hand (in which case this is a no-op).
    """
    parsed = urlparse(settings.database_url.replace("+asyncpg", ""))
    target_db = (parsed.path or "/").lstrip("/") or "xgen_edit2docs"
    admin_path = "/postgres"
    admin_url = urlunparse(
        ("postgresql+asyncpg", parsed.netloc, admin_path, "", "", "")
    )

    try:
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    except Exception as exc:
        logger.warning(
            "bootstrap: cannot prepare admin connection — %s; "
            "assuming database %r already exists.",
            exc, target_db,
        )
        return

    try:
        from sqlalchemy import text

        async with admin_engine.connect() as conn:
            result = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target_db},
            )
            if result.scalar() == 1:
                logger.info("bootstrap: database %r already exists", target_db)
                return
            # CREATE DATABASE doesn't accept bound parameters.
            await conn.execute(text(f'CREATE DATABASE "{target_db}"'))
            logger.info("bootstrap: created Postgres database %r", target_db)
    except Exception as exc:
        # Permission denied is the most common case — log and let the
        # subsequent schema step fail loudly with a clearer error.
        logger.warning(
            "bootstrap: could not auto-create database %r (%s); "
            "assuming it exists or will be created out-of-band.",
            target_db, exc,
        )
    finally:
        await admin_engine.dispose()


# ---------------------------------------------------------------------------

async def _ensure_schema(settings: Settings) -> None:
    """Create every missing table via SQLAlchemy metadata.

    Idempotent — `create_all` skips tables that already exist. Users who
    want versioned migrations can still run `alembic upgrade head`; for
    the common "first boot, no schema yet" case create_all is the
    simplest path.
    """
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            # Existing-table inspection is sync — wrap with run_sync.
            existing = await conn.run_sync(
                lambda sync_conn: set(inspect(sync_conn).get_table_names())
            )
            expected = set(Base.metadata.tables.keys())
            missing = expected - existing
            if missing:
                logger.info(
                    "bootstrap: creating %d table(s): %s",
                    len(missing),
                    sorted(missing),
                )
                await conn.run_sync(Base.metadata.create_all)
            else:
                logger.info("bootstrap: schema already up-to-date (%d tables)", len(expected))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------

async def _ensure_s3_bucket(settings: Settings) -> None:
    try:
        import aioboto3  # type: ignore[import-untyped]
    except ImportError:
        logger.warning(
            "bootstrap: aioboto3 not installed — cannot auto-create S3 bucket."
        )
        return

    session = aioboto3.Session(
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        region_name=settings.s3_region,
    )
    client_kwargs: dict[str, object] = {"service_name": "s3"}
    if settings.s3_endpoint_url:
        client_kwargs["endpoint_url"] = settings.s3_endpoint_url

    try:
        async with session.client(**client_kwargs) as client:  # type: ignore[arg-type]
            try:
                await client.head_bucket(Bucket=settings.s3_bucket)
                logger.info("bootstrap: S3 bucket %r already exists", settings.s3_bucket)
                return
            except Exception:
                pass
            await client.create_bucket(Bucket=settings.s3_bucket)
            logger.info("bootstrap: created S3 bucket %r", settings.s3_bucket)
    except Exception as exc:
        logger.warning(
            "bootstrap: could not auto-create S3 bucket %r (%s); "
            "first upload will surface the real error.",
            settings.s3_bucket, exc,
        )
