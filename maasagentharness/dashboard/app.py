import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from maasagentharness.control.api import budgets_router, router as workers_router
from maasagentharness.control.budgets import Budget, resolve_budget
from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.telemetry import TelemetryEvent

_shutdown_event = asyncio.Event()

HARNESS_DB_PATH = os.getenv("HARNESS_DB_PATH", "harness_state.db")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Phase 1: the worker fleet now lives in a real SQLite store instead of
    # a module-level dict — see redesign-spec.md finding #6. One store per
    # dashboard process; `app.state` (not a module global) is the handle, so
    # tests can spin up isolated instances against a temp db path.
    store = WorkerStore(HARNESS_DB_PATH)
    await store.connect()
    app.state.worker_store = store
    app.state.supervisor = Supervisor(store)
    reaper_task = asyncio.create_task(app.state.supervisor.start_reaper())

    yield

    reaper_task.cancel()
    await store.close()
    # Phase 0 fix #0.13: @app.on_event("shutdown") is deprecated in current
    # FastAPI; a lifespan context manager is the supported replacement and
    # does the same job — flip the event so /events/stream's wait races exit
    # promptly instead of hanging until the client disconnects.
    _shutdown_event.set()


app = FastAPI(title="MaaS Rate-Limit Demo Dashboard API", lifespan=_lifespan)
app.include_router(workers_router)
app.include_router(budgets_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Real-time retention window for the live chart/log; see _prune_events below.
# Phase 0 fix #0.12: this constant existed before but was never actually
# applied — the only pruning that happened was a hard count-based cap.
WINDOW_SECONDS = 900
BUCKET_SECONDS = 5

events_buffer: List[TelemetryEvent] = []
_next_seq = 0  # Phase 0 fix #0.9 — monotonically increasing event id.

# --- Legacy v1 control API -------------------------------------------------
# Everything below this line that touches `registered_mode_defaults`, a
# worker's `limit_tpm`, or a team's limit is the v1 compatibility layer:
# /register, /control/{id}, /act/{N}, /reset, /limits/{team}. These exist
# for two reasons: (1) any agent still running the pre-Phase-1 runner.py
# talks to them, and (2) the *shipped* React dashboard (dashboard/frontend)
# still calls them and isn't rebuilt until Phase 5. All of them now delegate
# to the same `WorkerStore` (and, as of Phase 2, the same `Budget` records)
# the new `/api/workers` and `/api/budgets` routes use — there is exactly
# one source of truth for both fleet state and rate limits, persisted,
# surviving a dashboard restart.
#
# Phase 2 specifically: `limit_tpm` as a flat dict (Phase 1's now-deleted
# control/limits.py bridge) is gone. Reading it now means
# `resolve_budget(worker, all_budgets)`
# — worker-assignment > team > global — exactly like `/api/workers/{id}/budget`
# does. Writing it now means creating/updating a real `Budget` row:
#   - POST /control/{id} {"limit_tpm": N} -> upserts a worker-scoped budget
#     and explicitly assigns it to that one worker (an override).
#   - POST /limits/{team} {"limit_tpm": N} -> upserts a team-scoped budget.
#     Unlike the Phase 1 bridge, this does NOT touch individual workers'
#     own overrides — previously it silently overwrote every worker in the
#     team, including ones that had been edited individually, which was
#     really "bulk-edit everyone" wearing a "team limit" label. Real scoped
#     resolution means a worker-level override now actually stays put
#     against a team-level change, which is the behavior "team limit" always
#     implied but never delivered.
registered_mode_defaults: Dict[str, str] = {}


def _worker_budget_id(worker_id: str) -> str:
    return f"worker:{worker_id}"


def _team_budget_id(team: str) -> str:
    return f"team:{team}"


class AgentRegistration(BaseModel):
    user: str
    team: str
    display_name: str = ""
    task_description: str = ""
    agent: str = "triage"
    mode: str = "naive"
    state: str = "stopped"
    limit_tpm: int = 2000


class ControlPatch(BaseModel):
    """A partial update to a worker's control state.

    Phase 0 fix #0.2: the previous `POST /control/{user_id}` took a bare
    `Dict[str, Any]`, accepted arbitrary extra keys, and — for an unknown
    user — fabricated a bare `{"user":…, "team": "team-alpha"}` record that
    the next `AgentControlState(**data)` on the agent side would silently
    fill out with defaults (`agent=triage, mode=naive, state=running`),
    stomping whatever that agent actually was. `extra="forbid"` plus 404 on
    an unknown user (see `update_control`) close both holes.
    """
    model_config = ConfigDict(extra="forbid")

    state: Optional[Literal["running", "stopped"]] = None
    agent: Optional[str] = None
    mode: Optional[Literal["naive", "paced", "runaway"]] = None
    limit_tpm: Optional[int] = None


def _legacy_control_shape(w, budgets: List[Budget]) -> Dict[str, Any]:
    """WorkerRecord -> the pre-Phase-1 /control/{id} response shape, plus a
    few additive fields (`status`, `phase`, `generation`, `resolved_via`)
    that the current frontend ignores but a future one (or a curious
    `curl`) can already use. `limit_tpm` is the NOMINAL (pre-headroom)
    resolved budget value — what you'd see if you typed it back unchanged;
    the pacer applies headroom itself, same as before.
    """
    resolved = resolve_budget(w, budgets)
    limit_tpm = resolved.budget.tokens_per_minute
    if limit_tpm is None:
        # enforcement="off" / truly no budget resolved — the legacy UI has
        # no way to display "unenforced", so show a harmless placeholder
        # rather than `null`. The /api/workers/{id}/budget route (and
        # Phase 5's UI) show the real, honest resolved state.
        limit_tpm = 2000
    return {
        "user": w.worker_id,
        "team": w.team,
        "state": w.desired_state,
        "agent": w.desired_agent,
        "mode": w.desired_mode,
        "limit_tpm": limit_tpm,
        "unregistered": False,
        "status": w.status,
        "phase": str(w.phase),
        "generation": w.generation,
        "applied_generation": w.applied_generation,
        "resolved_via": resolved.resolved_via,
    }


def _legacy_registry_shape(w) -> Dict[str, Any]:
    return {
        "user": w.worker_id,
        "team": w.team,
        "display_name": w.display_name,
        "task_description": w.task_description,
        "agent": w.desired_agent,
        "mode": w.desired_mode,
    }


@app.post("/register")
async def register_agent(reg: AgentRegistration, request: Request):
    """Called by agent on startup to register itself with the dashboard.

    Legacy v1 route — delegates to the same WorkerStore `POST /api/workers`
    uses. Kept for agents still running the pre-Phase-1 runner.py.
    """
    store: WorkerStore = request.app.state.worker_store
    await store.register(
        worker_id=reg.user,
        team=reg.team,
        display_name=reg.display_name or reg.user,
        task_description=reg.task_description,
        managed=False,
        agent=reg.agent,
        mode=reg.mode,
    )
    # Phase 2: registration no longer seeds a per-worker limit override —
    # that was the old design's actual bug (every worker got an
    # unconditional individual entry that nothing else could ever fall
    # back past). A freshly registered worker now resolves against
    # team/global budgets like any other, until something explicitly
    # overrides it (see `update_control`/`update_team_limit` below).
    # `reg.limit_tpm` (from each agent's own LIMIT_TPM env var) is
    # intentionally unused here — the dashboard's Budget store is now
    # authoritative, not each agent's own startup config.
    registered_mode_defaults.setdefault(reg.user, reg.mode)
    return {"status": "registered", "user": reg.user}


def _prune_events() -> None:
    """Enforce the documented 15-minute retention window, plus a hard count
    cap as a memory backstop. Phase 0 fix #0.12 — previously `WINDOW_SECONDS`
    was computed and never used; the only real limit was "buffer > 2000 ->
    keep last 1000", which does not match docs/dashboard.md's claim of a
    15-minute window. This is O(n) per call, which is fine at the event
    volumes Phase 0 targets; Phase 4 replaces this buffer with a real
    metrics store.
    """
    global events_buffer
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    kept = []
    for ev in events_buffer:
        try:
            ev_ts = datetime.fromisoformat(ev.ts).timestamp()
        except Exception:
            ev_ts = now
        if ev_ts >= cutoff:
            kept.append(ev)
    events_buffer = kept
    if len(events_buffer) > 2000:
        del events_buffer[: len(events_buffer) - 1000]


@app.post("/events")
async def receive_event(event: TelemetryEvent):
    global _next_seq
    _next_seq += 1
    event.seq = _next_seq
    events_buffer.append(event)
    _prune_events()
    return {"status": "ok"}


@app.get("/control/{user_id}")
async def get_control(user_id: str, request: Request):
    store: WorkerStore = request.app.state.worker_store
    record = await store.get(user_id)
    if record is not None:
        budgets = await store.list_budgets()
        return _legacy_control_shape(record, budgets)
    # Phase 0 fix #0.1 (carried into v2's store): unknown worker means
    # stopped, with an explicit `unregistered` flag — never "running".
    return {
        "user": user_id,
        "team": "unknown",
        "state": "stopped",
        "agent": "triage",
        "mode": "naive",
        "limit_tpm": 2000,
        "unregistered": True,
    }


@app.post("/control/{user_id}")
async def update_control(user_id: str, patch: ControlPatch, request: Request):
    store: WorkerStore = request.app.state.worker_store
    record = await store.get(user_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown worker '{user_id}' — it must call POST /register before its control state can be changed.",
        )

    state_patch = {
        k: v for k, v in patch.model_dump(exclude_none=True).items()
        if k in ("state", "agent", "mode")
    }
    if state_patch:
        record = await store.patch_desired(user_id, state_patch)
    if patch.limit_tpm is not None:
        # Editing one worker's TPM creates/updates a worker-scoped budget
        # and explicitly assigns it — an override that beats team/global
        # resolution for this one worker, same as Phase 1's bridge
        # behaved, now backed by a real Budget row instead of a dict entry.
        budget_id = _worker_budget_id(user_id)
        await store.put_budget(
            budget_id, scope="worker", scope_ref=user_id,
            tokens_per_minute=patch.limit_tpm, requests_per_minute=None,
            max_concurrency=None, headroom=0.90, enforcement="client_bucket",
        )
        record = await store.patch_desired(user_id, {"budget_id": budget_id})

    budgets = await store.list_budgets()
    return _legacy_control_shape(record, budgets)


@app.post("/limits/{team_id}")
async def update_team_limit(team_id: str, payload: Dict[str, Any], request: Request):
    """Upserts a team-scoped Budget. Unlike the pre-Phase-2 version, this
    does NOT touch any worker's individual override — see this module's
    "Legacy v1 control API" docstring for why that's a fix, not a
    regression.
    """
    store: WorkerStore = request.app.state.worker_store
    new_limit = int(payload.get("limit_tpm", 2000))
    budget_id = _team_budget_id(team_id)
    await store.put_budget(
        budget_id, scope="team", scope_ref=team_id,
        tokens_per_minute=new_limit, requests_per_minute=None,
        max_concurrency=None, headroom=0.90, enforcement="client_bucket",
    )
    return {"team": team_id, "limit_tpm": new_limit}


async def _display_team_limits(store: WorkerStore, workers: List) -> Dict[str, int]:
    """Informational only — the shipped frontend's `DashboardState.limits`
    type exists but nothing in App.tsx actually renders it. Built from
    real team-scoped (falling back to global) Budget resolution so it's
    at least honest if anything ever does read it.
    """
    budgets = await store.list_budgets()
    teams = {w.team for w in workers}
    out: Dict[str, int] = {}
    for team in teams:
        for b in budgets:
            if b.scope == "team" and b.scope_ref == team and b.tokens_per_minute is not None:
                out[team] = b.tokens_per_minute
                break
        else:
            for b in budgets:
                if b.scope == "global" and b.tokens_per_minute is not None:
                    out[team] = b.tokens_per_minute
                    break
    return out


@app.post("/act/{act_num}")
async def set_act(act_num: int, request: Request):
    store: WorkerStore = request.app.state.worker_store
    workers = await store.list()
    if act_num == 1:
        # Normal Operations: stop first registered agent, start all others paced
        for i, w in enumerate(workers):
            if i == 0:
                await store.patch_desired(w.worker_id, {"state": "stopped"})
            else:
                await store.patch_desired(w.worker_id, {"state": "running", "mode": "paced"})
    elif act_num == 2:
        # Runaway: start all agents, first one goes runaway
        for i, w in enumerate(workers):
            if i == 0:
                await store.patch_desired(w.worker_id, {"state": "running", "mode": "runaway"})
            else:
                await store.patch_desired(w.worker_id, {"state": "running", "mode": "paced"})
    workers = await store.list()
    budgets = await store.list_budgets()
    return {
        "act": act_num,
        "control": {w.worker_id: _legacy_control_shape(w, budgets) for w in workers},
        "limits": await _display_team_limits(store, workers),
    }


@app.post("/reset")
async def reset(request: Request):
    global events_buffer
    store: WorkerStore = request.app.state.worker_store
    events_buffer = []
    for w in await store.list():
        default_mode = registered_mode_defaults.get(w.worker_id, w.desired_mode)
        await store.patch_desired(w.worker_id, {"state": "stopped", "mode": default_mode})
    workers = await store.list()
    budgets = await store.list_budgets()
    return {
        "status": "reset",
        "control": {w.worker_id: _legacy_control_shape(w, budgets) for w in workers},
        "limits": await _display_team_limits(store, workers),
    }


async def calculate_metrics(registered_users: List[str]):
    """Calculate rolling 1-minute tokens per minute per registered agent."""
    now = time.time()
    one_min_ago = now - 60.0

    user_metrics = {
        u: {"tpm": 0, "status_429_count": 0, "total_calls": 0, "latest_status": 200, "latest_node": "idle", "latency_ms": 0}
        for u in registered_users
    }

    # Time series: per-user token buckets (5s intervals, last 60s)
    buckets: Dict[int, Dict[str, Any]] = {}
    current_bucket_idx = int(now // BUCKET_SECONDS)
    for b in range(current_bucket_idx - 11, current_bucket_idx + 1):
        bucket: Dict[str, Any] = {"time": datetime.fromtimestamp(b * BUCKET_SECONDS).strftime("%H:%M:%S")}
        for u in registered_users:
            bucket[f"{u}_tokens"] = 0
            bucket[f"{u}_429"] = 0
        buckets[b] = bucket

    for ev in events_buffer:
        try:
            ev_ts = datetime.fromisoformat(ev.ts).timestamp()
        except Exception:
            ev_ts = now

        user = ev.user
        if user in user_metrics:
            user_metrics[user]["latest_status"] = ev.status
            user_metrics[user]["latest_node"] = ev.node
            user_metrics[user]["latency_ms"] = ev.latency_ms
            if ev_ts >= one_min_ago:
                user_metrics[user]["total_calls"] += 1
                tokens = ev.prompt_tokens + ev.completion_tokens
                user_metrics[user]["tpm"] += tokens
                if ev.status == 429:
                    user_metrics[user]["status_429_count"] += 1
            b_idx = int(ev_ts // BUCKET_SECONDS)
            if b_idx in buckets:
                tokens = ev.prompt_tokens + ev.completion_tokens
                buckets[b_idx][f"{user}_tokens"] += tokens
                if ev.status == 429:
                    buckets[b_idx][f"{user}_429"] += 1

    # Phase 0 fix #0.10: buckets accumulate tokens over a BUCKET_SECONDS
    # window, but `limit_tpm` (and the chart's reference line) are tokens
    # PER MINUTE. Without this scaling the plotted line sits at ~1/12th of
    # where it should, so it can never visibly cross the limit line — the
    # entire point of the chart. Scale each bucket's token count up to its
    # per-minute-equivalent rate so both series share units. `_429` counts
    # are left as raw per-bucket counts — those are already dimensionless.
    scale = 60.0 / BUCKET_SECONDS
    chart_series = []
    for k in sorted(buckets.keys()):
        bucket = buckets[k]
        scaled = dict(bucket)
        for key in bucket:
            if key.endswith("_tokens"):
                scaled[key] = bucket[key] * scale
        chart_series.append(scaled)

    return {
        "user_metrics": user_metrics,
        "chart_series": chart_series,
        "recent_events": [e.model_dump() for e in events_buffer[-30:]],
    }


@app.get("/events/stream")
async def events_stream(request: Request):
    """SSE endpoint streaming live telemetry updates once per second."""
    store: WorkerStore = request.app.state.worker_store

    async def event_generator():
        try:
            while not _shutdown_event.is_set():
                if await request.is_disconnected():
                    break
                workers = await store.list()
                budgets = await store.list_budgets()
                control = {w.worker_id: _legacy_control_shape(w, budgets) for w in workers}
                agent_registry = {w.worker_id: _legacy_registry_shape(w) for w in workers}
                metrics_data = await calculate_metrics(list(control.keys()))
                payload = {
                    "timestamp": time.time(),
                    "control": control,
                    "limits": await _display_team_limits(store, workers),
                    "agent_registry": agent_registry,
                    **metrics_data,
                }
                yield f"data: {json.dumps(payload)}\n\n"
                # Race the 1s sleep against the shutdown event
                done, _ = await asyncio.wait(
                    [
                        asyncio.ensure_future(asyncio.sleep(1.0)),
                        asyncio.ensure_future(_shutdown_event.wait()),
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except asyncio.CancelledError:
            pass  # graceful shutdown — suppress traceback

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Serve static built frontend files if build exists
FRONTEND_DIST = (Path(__file__).parent.parent.parent / "dashboard" / "frontend" / "dist").resolve()

_NOT_BUILT_HTML = """<!doctype html>
<html><head><title>Dashboard not built</title></head>
<body style="font-family: system-ui; max-width: 640px; margin: 80px auto; line-height: 1.5;">
<h1>Frontend not built</h1>
<p>The dashboard API is running, but <code>dashboard/frontend/dist/</code> does not exist yet.</p>
<p>Build it with:</p>
<pre style="background:#f4f4f4; padding: 12px; border-radius: 4px;">cd dashboard/frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Then reload this page.</p>
</body></html>"""

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# Phase 0 fix #0.11 / #0.19: this route is now ALWAYS registered (previously
# it was only added `if FRONTEND_DIST.exists()`, so a missing build meant a
# bare, unexplained 404 on every route). It also contains any requested path
# to FRONTEND_DIST — the previous version did `FRONTEND_DIST / full_path`
# with no check that the resolved path was still inside FRONTEND_DIST, which
# is a path-traversal hole.
#
# This still shadows nothing under /api/workers/* because FastAPI matches
# routes in registration order and `workers_router` was included above,
# before this catch-all. The full /api prefix migration for the *legacy*
# routes above is deferred to Phase 5 alongside the frontend rewrite (see
# redesign-spec.md finding #19's route-prefix half).
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    if not FRONTEND_DIST.exists():
        return HTMLResponse(_NOT_BUILT_HTML, status_code=200)

    requested = (FRONTEND_DIST / full_path).resolve()
    if requested.is_relative_to(FRONTEND_DIST) and requested.is_file():
        return FileResponse(requested)
    return FileResponse(FRONTEND_DIST / "index.html")


def main():
    import uvicorn
    uvicorn.run(
        "maasagentharness.dashboard.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        timeout_graceful_shutdown=1,
    )


if __name__ == "__main__":
    main()
