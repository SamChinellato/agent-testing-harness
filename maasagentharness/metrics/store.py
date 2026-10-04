"""Dashboard-side live metrics: one Aggregate per worker, plus dropped-iteration
totals and clock-skew tracking. Process-wide and in-memory, like the event
buffer it sits beside — persisted history lives in the run artifacts
(artifact.py), not here.

Server-side ingest timestamps (redesign-spec.md finding #15): every event is
stamped with `ts_ingest_ms` on arrival. Buckets and rates are computed from
that, never from the agent's own clock. The agent's `ts` is kept only to
measure skew between the two.
"""
import time
from typing import Dict, Optional

from maasagentharness.metrics.aggregate import Aggregate


class MetricsStore:
    def __init__(self) -> None:
        self.by_worker: Dict[str, Aggregate] = {}
        self.dropped_by_worker: Dict[str, int] = {}
        self.clock_skew_ms: Dict[str, int] = {}

    def reset(self) -> None:
        self.by_worker.clear()
        self.dropped_by_worker.clear()
        self.clock_skew_ms.clear()

    def ingest(self, event, agent_ts_ms: Optional[int] = None) -> None:
        worker = event.user
        self.by_worker.setdefault(worker, Aggregate()).add(event)
        if agent_ts_ms is not None:
            skew = int(event.ts_ingest_ms - agent_ts_ms)
            # Keep the largest absolute skew seen — that's the figure that
            # matters when deciding whether bucketing can be trusted.
            prev = self.clock_skew_ms.get(worker, 0)
            if abs(skew) > abs(prev):
                self.clock_skew_ms[worker] = skew

    def set_dropped(self, worker: str, total: int) -> None:
        """Cumulative dropped-iteration count, reported by the worker via
        heartbeat (the executor owns the count; the dashboard just mirrors
        the latest cumulative value).
        """
        self.dropped_by_worker[worker] = int(total)


def now_ms() -> int:
    return int(time.time() * 1000)


default_store = MetricsStore()
