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

from maasagentharness.telemetry import TelemetryEvent

_shutdown_event = asyncio.Event()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    # Phase 0 fix #0.13: @app.on_event("shutdown") is deprecated in current
    # FastAPI; a lifespan context manager is the supported replacement and
    # does the same job — flip the event so /events/stream's wait races exit
    # promptly instead of hanging until the client disconnects.
    _shutdown_event.set()


app = FastAPI(title="MaaS Rate-Limit Demo Dashboard API", lifespan=_lifespan)

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

# Dynamically populated as agents register
control_states: Dict[str, Any] = {}

# Agent display metadata — populated on registration
agent_registry: Dict[str, Any] = {}

team_limits: Dict[str, int] = {}


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


@app.post("/register")
async def register_agent(reg: AgentRegistration):
    """Called by agent on startup to register itself with the dashboard."""
    if reg.user not in control_states:
        control_states[reg.user] = {
            "user": reg.user,
            "team": reg.team,
            "state": "stopped",
            "agent": reg.agent,
            "mode": reg.mode,
            "limit_tpm": reg.limit_tpm,
            "unregistered": False,
        }
    agent_registry[reg.user] = {
        "user": reg.user,
        "team": reg.team,
        "display_name": reg.display_name or reg.user,
        "task_description": reg.task_description,
        "agent": reg.agent,
        "mode": reg.mode,
    }
    if reg.team not in team_limits:
        team_limits[reg.team] = reg.limit_tpm
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
async def get_control(user_id: str):
    if user_id in control_states:
        return control_states[user_id]
    # Phase 0 fix #0.1: this used to return state="running" for ANY unknown
    # user_id — so restarting the dashboard, or starting an agent before the
    # dashboard is up, meant that agent silently kept calling the model with
    # no card in the UI to stop it. Unknown now means stopped, with an
    # explicit `unregistered` flag so the agent (and later the UI) can tell
    # "never registered" apart from "registered and stopped".
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
async def update_control(user_id: str, patch: ControlPatch):
    if user_id not in control_states:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown worker '{user_id}' — it must call POST /register before its control state can be changed.",
        )
    control_states[user_id].update(patch.model_dump(exclude_none=True))
    return control_states[user_id]


@app.post("/limits/{team_id}")
async def update_team_limit(team_id: str, payload: Dict[str, Any]):
    new_limit = payload.get("limit_tpm", 2000)
    team_limits[team_id] = int(new_limit)
    for u, state in control_states.items():
        if state.get("team") == team_id:
            state["limit_tpm"] = int(new_limit)
    return {"team": team_id, "limit_tpm": team_limits[team_id]}


@app.post("/act/{act_num}")
async def set_act(act_num: int):
    if act_num == 1:
        # Normal Operations: stop runaway agents, start all paced except first registered
        users = list(control_states.keys())
        for i, u in enumerate(users):
            if i == 0:
                control_states[u].update({"state": "stopped"})
            else:
                control_states[u].update({"state": "running", "mode": "paced"})
    elif act_num == 2:
        # Runaway: start all agents, first one goes runaway
        users = list(control_states.keys())
        for i, u in enumerate(users):
            if i == 0:
                control_states[u].update({"state": "running", "mode": "runaway"})
            else:
                control_states[u].update({"state": "running", "mode": "paced"})
    return {"act": act_num, "control": control_states, "limits": team_limits}


@app.post("/reset")
async def reset():
    global events_buffer
    events_buffer = []
    for u in control_states:
        control_states[u]["state"] = "stopped"
        # Restore mode from registry
        if u in agent_registry:
            control_states[u]["mode"] = agent_registry[u]["mode"]
    return {"status": "reset", "control": control_states, "limits": team_limits}


def calculate_metrics():
    """Calculate rolling 1-minute tokens per minute per registered agent."""
    now = time.time()
    one_min_ago = now - 60.0
    registered_users = list(control_states.keys())

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
    async def event_generator():
        try:
            while not _shutdown_event.is_set():
                if await request.is_disconnected():
                    break
                metrics_data = calculate_metrics()
                payload = {
                    "timestamp": time.time(),
                    "control": control_states,
                    "limits": team_limits,
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
