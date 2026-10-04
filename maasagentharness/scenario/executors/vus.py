"""Closed-model executors: hold concurrency (VU count) fixed and let each
VU loop back-to-back. If the endpoint slows down, offered load slows down
with it — this is what redesign-spec.md's k6 research calls coordinated
omission: it can hide the very saturation you're trying to expose. Use
these for capacity planning against a fixed concurrency limit; use the
arrival-rate executors (arrival.py) to actually find a saturation point.
"""
import asyncio
import time
from typing import List

from maasagentharness.scenario.executors.base import Executor, ExecutorStats, IterationFn
from maasagentharness.scenario.schema import Stage, parse_duration


async def _vu_loop(iteration_fn: IterationFn, stop_event: asyncio.Event, deadline: float, stats: ExecutorStats) -> None:
    while time.monotonic() < deadline and not stop_event.is_set():
        stats.iterations_started += 1
        try:
            await iteration_fn()
            stats.iterations_completed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Agent-level failures are already captured in telemetry
            # (every ModelHarness call emits a TelemetryEvent regardless of
            # outcome) — the executor's job is just to keep offering load,
            # not to re-report what already got reported.
            pass


class ConstantVUs(Executor):
    """N tasks, each looping `iteration_fn` back-to-back for `duration_s`.
    This IS the pre-Phase-3 runner.py behavior (one process, one loop)
    generalized from N=1 to any N.
    """
    def __init__(self, vus: int, duration_s: float):
        self.vus = vus
        self.duration_s = duration_s

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats:
        stats = ExecutorStats()
        start = time.monotonic()
        deadline = start + self.duration_s

        tasks = [
            asyncio.create_task(_vu_loop(iteration_fn, stop_event, deadline, stats))
            for _ in range(self.vus)
        ]
        try:
            await asyncio.wait(tasks, timeout=self.duration_s + 5.0)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        stats.duration_s = time.monotonic() - start
        return stats


class RampingVUs(Executor):
    """Adjusts the number of active VUs at each stage boundary by
    spawning or cancelling tasks — k6's ramping-vus equivalent. Use for
    capacity planning against a concurrency limit (connection pools,
    worker threads) rather than an offered request rate.
    """
    def __init__(self, stages: List[Stage]):
        if not stages:
            raise ValueError("RampingVUs requires at least one stage")
        self.stages = stages

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats:
        stats = ExecutorStats()
        start = time.monotonic()
        tasks: List[asyncio.Task] = []
        # Far-future deadline — stage duration governs each VU's loop
        # lifetime in practice via cancellation at stage boundaries, not
        # this deadline; it only matters as a last-resort bound.
        far_future = start + sum(parse_duration(s.duration) for s in self.stages) + 3600.0

        for stage in self.stages:
            if stop_event.is_set():
                break
            target = max(0, stage.target)
            while len(tasks) < target:
                tasks.append(asyncio.create_task(_vu_loop(iteration_fn, stop_event, far_future, stats)))
            while len(tasks) > target:
                t = tasks.pop()
                t.cancel()

            stage_deadline = time.monotonic() + parse_duration(stage.duration)
            while time.monotonic() < stage_deadline and not stop_event.is_set():
                await asyncio.sleep(min(0.2, max(0.0, stage_deadline - time.monotonic())))

        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        stats.duration_s = time.monotonic() - start
        return stats
