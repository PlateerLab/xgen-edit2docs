"""Placeholder executor for the `convert` kind.

Used by tests to exercise the queue -> worker -> bus -> SSE round-trip
without booting any real conversion / LLM / image work. Records three
stage events and flips the job to `done`.

Real per-kind executors (generate_deck, etc.) replace this in M3.5.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ...db.models import JobEventType, JobKind, JobStatus
from ...services.jobs import record_event
from .registry import ExecutionContext, register


@register(JobKind.convert)
async def convert_noop(ctx: ExecutionContext) -> None:
    job = ctx.job
    job.status = JobStatus.running
    job.started_at = datetime.now(timezone.utc)
    await ctx.session.flush()

    await record_event(
        session=ctx.session, bus=ctx.bus, job_id=job.id,
        type=JobEventType.stage,
        payload={"stage": "converting", "message_key": "stages.converting"},
    )
    await record_event(
        session=ctx.session, bus=ctx.bus, job_id=job.id,
        type=JobEventType.progress,
        payload={"progress": 0.5},
    )
    await record_event(
        session=ctx.session, bus=ctx.bus, job_id=job.id,
        type=JobEventType.stage,
        payload={"stage": "done", "message_key": "stages.done"},
    )

    job.status = JobStatus.done
    job.finished_at = datetime.now(timezone.utc)
    job.result = {"noop": True}
    await ctx.session.commit()
