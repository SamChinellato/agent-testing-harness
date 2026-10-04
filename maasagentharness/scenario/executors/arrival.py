"""Open-model executors: offer load on a fixed schedule regardless of how
long previous iterations take. When the system can't keep up, you see
queueing, concurrency exhaustion, and — the signal these executors exist
to produce — dropped iterations, instead of the offered rate silently
slowing down to match whatever the system can do (coordinated omission).
See redesign-spec.md §3's k6 research for why this distinction matters;
`vus.py`'s closed-model executors cannot produce this signal by
construction.
"""
import asyncio
import random
import time
from typing import Callable, List, Optional, Set

from maasagentharness.scenario.executors.base import Executor, ExecutorStats, IterationFn
from maasagentharness.scenario.schema import Stage, parse_duration

RateFn = Callable[[float], float]  # elapsed seconds -> iterations/min


async def _run_scheduled(
    iteration_fn: IterationFn,
    stop_event: asyncio.Event,
    rate_at: RateFn,
    interval_for_rate: Callable[[float], float],
    total_duration_s: float,
    max_vus: int,
) -> ExecutorStats:
    """Shared scheduler loop for every open-model executor below. Fires a
    new task on a clock; if `max_vus` are already in flight, the new
    iteration is dropped (counted, never started) rather than queued —
    queuing it would turn this back into a closed model.
    """
    stats = ExecutorStats()
    start = time.monotonic()
    in_flight: Set[asyncio.Task] = set()

    async def tracked_iteration() -> None:
        stats.iterations_started += 1
        try:
            await iteration_fn()
            stats.iterations_completed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    next_fire = start
    while True:
        now = time.monotonic()
        elapsed = now - start
        if elapsed >= total_duration_s or stop_event.is_set():
            break

        in_flight = {t for t in in_flight if not t.done()}

        if now >= next_fire:
            rate = rate_at(elapsed)
            if len(in_flight) >= max_vus:
                stats.iterations_dropped += 1
            else:
                in_flight.add(asyncio.create_task(tracked_iteration()))
            next_fire = now + interval_for_rate(rate)

        sleep_for = min(max(next_fire - time.monotonic(), 0.0), 0.05)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
        except asyncio.TimeoutError:
            pass

    for t in in_flight:
        if not t.done():
            t.cancel()
    if in_flight:
        await asyncio.gather(*in_flight, return_exceptions=True)

    stats.duration_s = time.monotonic() - start
    return stats


def _constant_interval(rate_per_min: float) -> float:
    return 60.0 / rate_per_min if rate_per_min > 0 else 1.0


class ConstantArrivalRate(Executor):
    """Fires exactly `rate` iterations/min, on a fixed clock, for `duration_s`."""
    def __init__(self, rate: float, duration_s: float, max_vus: int):
        self.rate = rate
        self.duration_s = duration_s
        self.max_vus = max_vus

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats:
        return await _run_scheduled(
            iteration_fn, stop_event, rate_at=lambda _elapsed: self.rate,
            interval_for_rate=_constant_interval, total_duration_s=self.duration_s, max_vus=self.max_vus,
        )


class RampingArrivalRate(Executor):
    """Interpolates the offered rate piecewise-linearly across `stages`
    (same semantics as k6's ramping-arrival-rate: ramp FROM the previous
    stage's target TO this stage's target over its duration).
    """
    def __init__(self, stages: List[Stage], max_vus: int):
        if not stages:
            raise ValueError("RampingArrivalRate requires at least one stage")
        self.stages = stages
        self.max_vus = max_vus
        self._durations = [parse_duration(s.duration) for s in stages]
        self.total_duration_s = sum(self._durations)

    def _rate_at(self, elapsed: float) -> float:
        t = 0.0
        prev_target = self.stages[0].target
        for stage, dur in zip(self.stages, self._durations):
            if dur <= 0:
                t += dur
                prev_target = stage.target
                continue
            if elapsed <= t + dur:
                frac = max(0.0, min(1.0, (elapsed - t) / dur))
                return prev_target + (stage.target - prev_target) * frac
            prev_target = stage.target
            t += dur
        return float(self.stages[-1].target)

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats:
        return await _run_scheduled(
            iteration_fn, stop_event, rate_at=self._rate_at,
            interval_for_rate=_constant_interval, total_duration_s=self.total_duration_s, max_vus=self.max_vus,
        )


class PoissonArrivalRate(Executor):
    """Same schedule as ConstantArrivalRate, but inter-arrival times are
    drawn from an exponential distribution (mean = 60/rate seconds)
    instead of fixed spacing — GuideLLM-style bursty arrivals, closer to
    how real traffic actually arrives than a metronome.
    """
    def __init__(self, rate: float, duration_s: float, max_vus: int, rng: Optional[random.Random] = None):
        self.rate = rate
        self.duration_s = duration_s
        self.max_vus = max_vus
        self._rng = rng or random.Random()

    def _interval(self, rate_per_min: float) -> float:
        mean_interval = _constant_interval(rate_per_min)
        return self._rng.expovariate(1.0 / mean_interval)

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> ExecutorStats:
        return await _run_scheduled(
            iteration_fn, stop_event, rate_at=lambda _elapsed: self.rate,
            interval_for_rate=self._interval, total_duration_s=self.duration_s, max_vus=self.max_vus,
        )
