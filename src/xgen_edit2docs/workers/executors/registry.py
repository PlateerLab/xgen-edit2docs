"""Job kind -> executor registry.

Each executor takes an ExecutionContext (Job row + session + bus) and runs
the work. Status transitions (queued -> running -> done/failed) are the
executor's responsibility; recording stage events via the bus is also done
inside the executor.

M3.5 registers `generate_deck` here. For M3.4 we ship a tiny `noop` executor
just to exercise the worker round-trip in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from ...db.models import Job, JobKind
from ...services.jobs import JobBus


@dataclass
class ExecutionContext:
    session: AsyncSession
    bus: JobBus
    job: Job


Executor = Callable[[ExecutionContext], Awaitable[None]]


# Late-bound registry; executors register on import via register().
EXECUTORS: dict[JobKind, Executor] = {}


def register(kind: JobKind):
    """Decorator: bind an executor coroutine to a job kind."""

    def _wrap(fn: Executor) -> Executor:
        EXECUTORS[kind] = fn
        return fn

    return _wrap


# Import every executor module so its @register call populates EXECUTORS at
# the time the worker starts up.
from . import edit_deck, generate_deck, noop  # noqa: F401, E402  (re-export for side effect)
