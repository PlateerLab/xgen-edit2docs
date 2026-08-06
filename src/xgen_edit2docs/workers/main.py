"""arq worker entry point.

Run with:
    .venv/bin/arq xgen_edit2docs.workers.main.WorkerSettings

Or with reload during dev:
    .venv/bin/arq xgen_edit2docs.workers.main.WorkerSettings --watch src/

The actual job logic for each kind lives in workers.executors.*. This module
is intentionally tiny — it owns the lifecycle (startup, shutdown, retries)
and dispatches each queued job to the right executor based on its `kind`.
"""

from __future__ import annotations

import logging
import uuid

from arq.connections import RedisSettings

from ..config import get_settings
from ..db.session import get_sessionmaker
from ..services.jobs import arq_redis_settings, get_default_bus
from .executors.registry import EXECUTORS, ExecutionContext

logger = logging.getLogger(__name__)


async def startup(ctx: dict) -> None:
    """Called once per worker process."""
    settings = get_settings()
    logger.info("xgen_edit2docs worker starting", extra={"env": settings.environment})
    ctx["sessionmaker"] = get_sessionmaker()
    ctx["bus"] = get_default_bus()


async def shutdown(ctx: dict) -> None:
    logger.info("xgen_edit2docs worker shutting down")


async def run_job(ctx: dict, job_id_str: str) -> dict:
    """Dispatch a queued job to its executor.

    arq passes the function name and arguments. We load the Job row,
    look up the right executor by `kind`, and let it do its thing while
    recording events as it goes.
    """
    job_id = uuid.UUID(job_id_str)
    sessionmaker = ctx["sessionmaker"]
    bus = ctx["bus"]

    async with sessionmaker() as session:
        from ..db.models import Job, JobStatus

        # Load + flip to running.
        from sqlalchemy import select

        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if job is None:
            logger.warning("worker received unknown job_id=%s", job_id)
            return {"ok": False, "reason": "job_not_found"}

        executor = EXECUTORS.get(job.kind)
        if executor is None:
            logger.error("no executor registered for kind=%s", job.kind)
            job.status = JobStatus.failed
            job.error_message = f"no executor for kind={job.kind.value}"
            await session.commit()
            return {"ok": False, "reason": "no_executor"}

        context = ExecutionContext(session=session, bus=bus, job=job)
        try:
            await executor(context)
            return {"ok": True}
        except Exception as exc:
            logger.exception("worker job %s failed", job_id)
            job.status = JobStatus.failed
            job.error_message = str(exc)
            await session.commit()
            return {"ok": False, "reason": "exception", "error": str(exc)}


class WorkerSettings:
    """arq.Worker bindings."""

    functions = [run_job]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 4
    job_timeout = 60 * 60  # one hour per deck — Strategist + N executors can take a while

    @staticmethod
    def redis_settings() -> RedisSettings:
        return arq_redis_settings()
