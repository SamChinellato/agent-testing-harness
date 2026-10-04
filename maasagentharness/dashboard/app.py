import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from maasagentharness.control.api import budgets_router, runs_router, router as workers_router
from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.dashboard.snapshot import SnapshotBuilder
from maasagentharness.metrics import default_store, render_prometheus
from maasagentharness.scenario.loader import load_run_config
from maasagentharness.scenario.schema import ScenarioConfigError
from maasagentharness.telemetry import TelemetryEvent

_shutdown_event = asyncio.Event()

HARNESS_DB_PATH = os.getenv("HARNESS_DB_PATH", "harness_state.db")
SCENARIOS_DIR = Path(os.getenv("SCENARIOS_DIR", "scenarios"))
STREAM_BACKLOG = 50  # events replayed to a client that connects fresh


@asynccontextmanager
async def _lifespan(app: FastAPI):
    store = WorkerStore(HARNESS_DB_PATH)
    await store.connect()
    app.state.worker_store = store
    app.state.supervisor = Supervisor(store)
    reaper_task = asyncio.create_task(app.state.supervisor.start_reaper())

    yield

    reaper_task.cancel()
    await store.close()
    _shutdown_event.set()


app = FastAPI(title="Agent Testing Harness — Dashboard API", lifespan=_lifespan)
app.include_router(workers_router)
app.include_router(budgets_router)
app.include_router(runs_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The event log and the telemetry buffer keep a 15-minute window. Rates and
# percentiles come from the metrics store, which is cumulative.
WINDOW_SECONDS = 900

events_buffer: List[TelemetryEvent] = []
_next_seq = 0
snapshot_builder = SnapshotBuilder()


def _prune_events() -> None:
    """Keep the documented 15-minute window, with a count cap as a memory backstop.
    Time is the server ingest stamp (finding #15)."""
    global events_buffer
    cutoff = time.time() - WINDOW_SECONDS
    kept = [ev for ev in events_buffer if (ev.ts_ingest_ms / 1000.0) >= cutoff]
    if len(kept) > 2000:
        kept = kept[-1000:]
    events_buffer = kept


@app.post("/events")
async def receive_event(event: TelemetryEvent):
    """Telemetry ingest. Agents post every model call here."""
    global _next_seq
    _next_seq += 1
    event.seq = _next_seq
    # The server stamps arrival time. The agent's own timestamp is used only
    # to measure clock skew.
    ingest_ms = int(time.time() * 1000)
    event.ts_ingest_ms = ingest_ms
    agent_ts_ms = None
    try:
        agent_ts_ms = int(datetime.fromisoformat(event.ts).timestamp() * 1000)
    except Exception:
        pass
    events_buffer.append(event)
    _prune_events()
    default_store.ingest(event, agent_ts_ms=agent_ts_ms)
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    """Prometheus exposition format. Cumulative since the process started, so
    nothing in the UI clears it."""
    return render_prometheus(default_store)


def _sse(event: str, data: Any, event_id: Optional[str] = None) -> str:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


@app.get("/api/stream")
async def live_stream(request: Request, once: bool = Query(False)):
    """One server-sent stream for the whole UI.

    Every second it sends a `snapshot` event with the complete read model.
    New events go out as `events` batches, and each batch's SSE `id` is the
    last seq it contains. EventSource sends that back as `Last-Event-ID` on
    reconnect, so the client resumes without gaps or duplicates.

    `once=1` returns a single tick and closes. Tests use it; the UI never does.
    """
    store: WorkerStore = request.app.state.worker_store
    header = request.headers.get("last-event-id")
    try:
        last_seq: Optional[int] = int(header) if header else None
    except ValueError:
        last_seq = None

    async def frames():
        nonlocal last_seq
        if last_seq is None:
            backlog = events_buffer[-STREAM_BACKLOG:]
        else:
            backlog = [e for e in events_buffer if e.seq > last_seq]
        if backlog:
            last_seq = backlog[-1].seq
            yield _sse("events", [e.model_dump() for e in backlog], event_id=str(last_seq))

        while not _shutdown_event.is_set():
            if await request.is_disconnected():
                break
            snapshot = await snapshot_builder.build(store, events_buffer, default_store)
            yield _sse("snapshot", snapshot)
            fresh = [e for e in events_buffer if e.seq > (last_seq or 0)]
            if fresh:
                last_seq = fresh[-1].seq
                yield _sse("events", [e.model_dump() for e in fresh], event_id=str(last_seq))
            if once:
                break
            await asyncio.sleep(1.0)

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/scenarios")
async def list_scenarios():
    """Scenario files under SCENARIOS_DIR, each validated, so the launcher can
    show a file's problems before anyone tries to run it."""
    if not SCENARIOS_DIR.exists():
        return []
    out = []
    for path in sorted([*SCENARIOS_DIR.glob("*.yaml"), *SCENARIOS_DIR.glob("*.yml")]):
        try:
            run = load_run_config(path)
            out.append({
                "path": str(path),
                "file": path.name,
                "run": run.run,
                "valid": True,
                "error": None,
                "scenarios": [
                    {"name": name, "agent": sc.agent, "mode": sc.mode, "executor": sc.executor.kind,
                     "budget": sc.budget}
                    for name, sc in run.scenarios.items()
                ],
            })
        except ScenarioConfigError as e:
            out.append({"path": str(path), "file": path.name, "run": None,
                        "valid": False, "error": str(e), "scenarios": []})
    return out


# Built frontend. Mounted when present; the catch-all below handles the rest.
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

if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")


# Registered last so that /api/*, /events and /metrics always match first.
# Paths are contained to FRONTEND_DIST.
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
