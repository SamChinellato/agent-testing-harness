"""sweep: synchronous latency floor -> throughput ceiling -> intermediate
rates. Replaces probe.py's "blast for 90s and see what happens" with a
principled, reusable sequence of real benchmarks, GuideLLM-style.

Not a primitive Executor (different result shape — a sequence of steps,
not one ExecutorStats) — run directly via `Sweep.run()`.
"""
import asyncio
import time
from typing import Callable, List, Optional

from maasagentharness.scenario.executors.arrival import ConstantArrivalRate
from maasagentharness.scenario.executors.base import ExecutorStats, IterationFn

BreachFn = Callable[[ExecutorStats], bool]


class SweepStep(ExecutorStats):
    rate: float


class SweepResult:
    def __init__(self, steps: List[SweepStep], ceiling_rate: Optional[float]):
        self.steps = steps
        # None means the ceiling search exhausted its range without ever
        # seeing a breach — either the endpoint is far faster than
        # max_vus/floor_rate can probe, or (more likely in testing) the
        # fake/real endpoint genuinely never saturates at these rates.
        self.ceiling_rate = ceiling_rate


class Sweep:
    def __init__(
        self,
        max_vus: int,
        floor_rate: float = 1.0,
        ceiling_search_duration_s: float = 10.0,
        step_duration_s: float = 10.0,
        num_intermediate_steps: int = 4,
        breach_fn: Optional[BreachFn] = None,
        max_ceiling_multiplier: float = 4096.0,
    ):
        self.max_vus = max_vus
        self.floor_rate = floor_rate
        self.ceiling_search_duration_s = ceiling_search_duration_s
        self.step_duration_s = step_duration_s
        self.num_intermediate_steps = num_intermediate_steps
        # Default breach signal: any dropped iteration at all. This is
        # the one saturation signal available without Phase 4's
        # percentile store — see schema.py's ThresholdSpec docstring.
        self.breach_fn: BreachFn = breach_fn or (lambda stats: stats.iterations_dropped > 0)
        self.max_ceiling_multiplier = max_ceiling_multiplier

    async def run(self, iteration_fn: IterationFn, stop_event: asyncio.Event) -> SweepResult:
        steps: List[SweepStep] = []

        async def probe(rate: float, duration_s: float) -> SweepStep:
            executor = ConstantArrivalRate(rate=rate, duration_s=duration_s, max_vus=self.max_vus)
            stats = await executor.run(iteration_fn, stop_event)
            step = SweepStep(**stats.model_dump(), rate=rate)
            steps.append(step)
            return step

        # 1. Synchronous floor — one at a time, a real latency baseline.
        floor_step = await probe(self.floor_rate, self.step_duration_s)
        if stop_event.is_set():
            return SweepResult(steps, ceiling_rate=None)

        # 2. Throughput ceiling: double the rate until a breach, bounded
        # so this can't run forever against an endpoint that never
        # saturates.
        rate = max(self.floor_rate * 2, 2.0)
        ceiling_rate: Optional[float] = None
        while rate < self.floor_rate * self.max_ceiling_multiplier:
            if stop_event.is_set():
                break
            step = await probe(rate, self.ceiling_search_duration_s)
            if self.breach_fn(step):
                ceiling_rate = rate
                break
            rate *= 2

        if ceiling_rate is None or stop_event.is_set():
            return SweepResult(steps, ceiling_rate=ceiling_rate)

        # 3. Intermediate rates between floor and ceiling, for a usable curve.
        if self.num_intermediate_steps > 0:
            span = ceiling_rate - self.floor_rate
            for i in range(1, self.num_intermediate_steps + 1):
                if stop_event.is_set():
                    break
                r = self.floor_rate + span * i / (self.num_intermediate_steps + 1)
                await probe(r, self.step_duration_s)

        return SweepResult(steps, ceiling_rate=ceiling_rate)
