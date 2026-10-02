import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from maasagentharness.telemetry import TelemetryEvent

_shutdown_event = asyncio.Event()

app = FastAPI(title="MaaS Rate-Limit Demo Dashboard API")


@app.on_event("shutdown")
async def _on_shutdown():
    _shutdown_event.set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

WINDOW_SECONDS = 900
events_buffer: List[TelemetryEvent] = []

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


@app.post("/events")
async def receive_event(event: TelemetryEvent):
    events_buffer.append(event)
    now = time.time()
    # Prune old events outside 15 min window if buffer gets large
    if len(events_buffer) > 2000:
        cutoff = now - WINDOW_SECONDS
        # keep last 1000 events
        del events_buffer[: len(events_buffer) - 1000]
    return {"status": "ok"}


@app.get("/control/{user_id}")
async def get_control(user_id: str):
    if user_id in control_states:
        return control_states[user_id]
    return {"user": user_id, "team": "team-alpha", "state": "running", "agent": "triage", "mode": "naive", "limit_tpm": 2000}


@app.post("/control/{user_id}")
async def update_control(user_id: str, payload: Dict[str, Any]):
    if user_id not in control_states:
        control_states[user_id] = {"user": user_id, "team": "team-alpha"}
    control_states[user_id].update(payload)
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
    current_bucket_idx = int(now // 5)
    for b in range(current_bucket_idx - 11, current_bucket_idx + 1):
        bucket: Dict[str, Any] = {"time": datetime.fromtimestamp(b * 5).strftime("%H:%M:%S")}
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
            b_idx = int(ev_ts // 5)
            if b_idx in buckets:
                tokens = ev.prompt_tokens + ev.completion_tokens
                buckets[b_idx][f"{user}_tokens"] += tokens
                if ev.status == 429:
                    buckets[b_idx][f"{user}_429"] += 1

    chart_series = [buckets[k] for k in sorted(buckets.keys())]

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
FRONTEND_DIST = Path(__file__).parent.parent.parent / "dashboard" / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file_path = FRONTEND_DIST / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
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
