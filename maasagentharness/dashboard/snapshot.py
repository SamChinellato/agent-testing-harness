"""The single read model the UI renders from.

Every tick the dashboard builds one complete snapshot: the fleet, the budgets,
each worker's live rate over the last minute, and a fixed-schema 60-second
series per worker. The client replaces its state atomically with each
snapshot, so it never holds a half-updated view.

Shape decisions (redesign-spec.md §2.3):
- The fleet is small, so it is sent whole each tick instead of as JSON Patch
  deltas. Only append-only data (events) is streamed incrementally.
- Series are a fixed schema: one array per worker aligned to `buckets`. A
  worker registering or leaving never changes the shape of other rows.
- Time is bucketed by server ingest time, never by an agent's clock.
"""
import time
from typing import Any, Dict, List, Optional

from maasagentharness.control.budgets import BudgetView, resolve_budget
from maasagentharness.control.store import WorkerStore
from maasagentharness.metrics.store import MetricsStore
from maasagentharness.telemetry import TelemetryEvent

BUCKET_SECONDS = 5
WINDOW_BUCKETS = 12  # 12 x 5s = the last minute
LIVE_WINDOW_SECONDS = 60.0


def event_time(ev: TelemetryEvent, fallback: float) -> float:
    """Server ingest time when present (finding #15); the agent's own
    timestamp only for events that somehow lack one."""
    if ev.ts_ingest_ms:
        return ev.ts_ingest_ms / 1000.0
    return fallback


class SnapshotBuilder:
    def __init__(self) -> None:
        self.revision = 0  # bumped on every build; the client discards anything older

    async def build(
        self,
        store: WorkerStore,
        events: List[TelemetryEvent],
        metrics: MetricsStore,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.revision += 1
        now = time.time() if now is None else now
        workers = await store.list()
        budgets = await store.list_budgets()

        current = int(now // BUCKET_SECONDS)
        bucket_ids = list(range(current - WINDOW_BUCKETS + 1, current + 1))
        # Trailing sums need 11 buckets of history before the first displayed point.
        history_ids = list(range(current - WINDOW_BUCKETS - (WINDOW_BUCKETS - 1), current + 1))
        bucket_pos = {b: i for i, b in enumerate(history_ids)}
        history_len = len(history_ids)

        live: Dict[str, Dict[str, Any]] = {
            w.worker_id: {
                "tokens_last_minute": 0,
                "calls_last_minute": 0,
                "throttled_last_minute": 0,
                "series_tokens": [0] * history_len,
                "series_throttled": [0] * WINDOW_BUCKETS,
                "latest_node": None,
                "latest_status": None,
                "latency_ms": None,
                "latest_ts": -1.0,
            }
            for w in workers
        }

        for ev in events:
            state = live.get(ev.user)
            if state is None:
                continue
            t = event_time(ev, now)
            tokens = ev.prompt_tokens + ev.completion_tokens
            if t >= state["latest_ts"]:
                state["latest_ts"] = t
                state["latest_node"] = ev.node
                state["latest_status"] = ev.status
                state["latency_ms"] = ev.latency_ms
            if t >= now - LIVE_WINDOW_SECONDS:
                state["tokens_last_minute"] += tokens
                state["calls_last_minute"] += 1
                if ev.status == 429:
                    state["throttled_last_minute"] += 1
            pos = bucket_pos.get(int(t // BUCKET_SECONDS))
            if pos is not None:
                state["series_tokens"][pos] += tokens
                shown = pos - (history_len - WINDOW_BUCKETS)
                if ev.status == 429 and shown >= 0:
                    state["series_throttled"][shown] += 1

        window_buckets = WINDOW_BUCKETS

        def trailing_minute(values: List[int]) -> List[float]:
            """Tokens over the 60 seconds ending at each displayed bucket."""
            out = []
            for i in range(window_buckets):
                end = i + (history_len - window_buckets)
                out.append(round(sum(values[end - window_buckets + 1:end + 1]), 1))
            return out

        resolved_by_worker = {w.worker_id: resolve_budget(w, budgets) for w in workers}
        shared_tpm: Dict[str, float] = {}
        shared_count: Dict[str, int] = {}
        for w in workers:
            bid = resolved_by_worker[w.worker_id].budget.budget_id
            shared_tpm[bid] = shared_tpm.get(bid, 0) + live[w.worker_id]["tokens_last_minute"]
            shared_count[bid] = shared_count.get(bid, 0) + 1

        rows: List[Dict[str, Any]] = []
        for w in workers:
            resolved = resolved_by_worker[w.worker_id]
            b = resolved.budget
            s = live[w.worker_id]
            rows.append({
                "worker_id": w.worker_id,
                "team": w.team,
                "display_name": w.display_name,
                "task_description": w.task_description,
                "managed": w.managed,
                "pid": w.pid,
                "desired": {
                    "state": w.desired_state,
                    "agent": w.desired_agent,
                    "mode": w.desired_mode,
                    "budget_id": w.desired_budget_id,
                    "generation": w.generation,
                },
                "observed": {
                    "status": w.status,
                    "phase": str(w.phase),
                    "applied_generation": w.applied_generation,
                    "current_node": w.current_node,
                    "iteration": w.iteration,
                    "last_error": w.last_error,
                    "heartbeat_at": w.heartbeat_at,
                    "lease_expires_at": w.lease_expires_at,
                },
                "budget": {
                    "budget_id": b.budget_id,
                    "scope": b.scope,
                    "resolved_via": resolved.resolved_via,
                    "tokens_per_minute": b.tokens_per_minute,
                    "effective_tokens_per_minute": b.effective_tokens_per_minute,
                    "enforcement": b.enforcement,
                    # A team or global budget is one limit shared by every worker that
                    # resolves to it. The gauge needs the shared total, not one worker's share.
                    "shared_by": shared_count[b.budget_id],
                    "shared_tokens_per_minute": shared_tpm[b.budget_id],
                },
                "live": {
                    "tokens_per_minute": s["tokens_last_minute"],
                    "calls_per_minute": s["calls_last_minute"],
                    "throttled_last_minute": s["throttled_last_minute"],
                    "latest_node": s["latest_node"],
                    "latest_status": s["latest_status"],
                    "latency_ms": s["latency_ms"],
                },
                "dropped_total": metrics.dropped_by_worker.get(w.worker_id, 0),
                "series": trailing_minute(s["series_tokens"]),
                "series_throttled": list(s["series_throttled"]),
            })

        running = sum(1 for r in rows if r["desired"]["state"] == "running")
        return {
            "revision": self.revision,
            "timestamp": now,
            "buckets": [b * BUCKET_SECONDS for b in bucket_ids],
            "workers": rows,
            "budgets": [BudgetView.from_budget(b).model_dump() for b in budgets],
            "teams": sorted({w.team for w in workers}),
            "totals": {
                "workers": len(rows),
                "running": running,
                "tokens_per_minute": sum(r["live"]["tokens_per_minute"] for r in rows),
                "throttled_last_minute": sum(r["live"]["throttled_last_minute"] for r in rows),
                "dropped_total": sum(r["dropped_total"] for r in rows),
            },
        }
