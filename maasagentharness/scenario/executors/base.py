"""The executor contract. See docs/phase-1-5-implementation-plan.md §3.3.

Deviation from the plan's literal signature: `Executor.run()` here takes
no `duration_s` parameter — each concrete executor already carries its own
schedule (a flat duration, or a list of `Stage`s) from construction,
because a Stage-based ramp and a flat duration don't fit one float
parameter uniformly. `build_executor()` (factory.py) is what turns an
`ExecutorConfig` into a fully-configured instance.
"""
import asyncio
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel

IterationFn = Callable[[], Awaitable[None]]


class ExecutorStats(BaseModel):
    iterations_started: int = 0
    iterations_completed: int = 0
    # Open-model executors only — couldn't start an iteration on schedule
    # because `max_vus` were already in flight. THE key saturation signal:
    # a closed-model executor can never produce this number because it
    # never offers more load than it's already waiting on (coordinated
    # omission) — see redesign-spec.md §3's k6 discussion.
    iterations_dropped: int = 0
    duration_s: float = 0.0


class Executor(Protocol):
    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats: ...
