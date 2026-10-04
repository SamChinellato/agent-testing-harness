"""Dashboard API tests for the Phase 5 surface: event ingest, the live stream
and its resume semantics, the snapshot's shape, scenario listing, and the
retirement of the legacy v1 routes.

httpx.ASGITransport does not run FastAPI's lifespan, so these fixtures wire
up `app.state` directly.
"""
import json
import time

import httpx
import pytest

from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.dashboard import app as dashboard_app
from maasagentharness.metrics.store import default_store


@pytest.fixture
async def client(tmp_path):
    s = WorkerStore(str(tmp_path / "dash.db"))
    await s.connect()
    dashboard_app.app.state.worker_store = s
    dashboard_app.app.state.supervisor = Supervisor(s)
    dashboard_app.events_buffer.clear()
    default_store.reset()
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await s.close()
    dashboard_app.events_buffer.clear()
    default_store.reset()


def _event(user="w1", **overrides):
    base = {
        "ts": "2020-01-01T00:00:00",
        "user": user, "team": "t", "agent": "triage", "mode": "naive",
        "run_id": "r1", "node": "classify", "status": 200,
        "prompt_tokens": 100, "completion_tokens": 20, "latency_ms": 300, "attempt": 1,
    }
    base.update(overrides)
    return base


def _parse_sse(text):
    """Parse one or more SSE frames into (event, id, data) tuples."""
    frames = []
    for block in text.strip().split("\n\n"):
        event = frame_id = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("id: "):
                frame_id = line[len("id: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        if event:
            frames.append((event, frame_id, json.loads("\n".join(data_lines))))
    return frames


# ---------------------------------------------------------------------------
# Event ingest
# ---------------------------------------------------------------------------

async def test_ingest_assigns_seq_and_server_timestamp(client):
    before = int(time.time() * 1000)
    await client.post("/events", json=_event())
    await client.post("/events", json=_event())
    a, b = dashboard_app.events_buffer[-2:]
    assert a.seq < b.seq
    assert a.ts_ingest_ms >= before
    # The agent's 2020 timestamp is kept for skew only; it does not drop the event.
    assert a in dashboard_app.events_buffer


# ---------------------------------------------------------------------------
# Live stream
# ---------------------------------------------------------------------------

async def test_stream_emits_snapshot_with_fleet_and_budgets(client):
    await client.post("/api/workers", json={"worker_id": "w1", "team": "team-a", "display_name": "Triage one"})
    resp = await client.get("/api/stream", params={"once": 1})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _parse_sse(resp.text)
    kinds = [f[0] for f in frames]
    assert "snapshot" in kinds
    snap = next(f[2] for f in frames if f[0] == "snapshot")
    assert snap["revision"] >= 1
    assert len(snap["buckets"]) == 12
    [worker] = snap["workers"]
    assert worker["worker_id"] == "w1"
    assert worker["display_name"] == "Triage one"
    assert worker["observed"]["status"] in ("stopped", "unknown", "running", "pending", "lost")
    assert len(worker["series"]) == 12
    assert snap["budgets"], "bootstrap global budget should be present"
    assert snap["totals"]["workers"] == 1


async def test_snapshot_live_rate_reflects_recent_events(client):
    await client.post("/api/workers", json={"worker_id": "w1", "team": "t"})
    now_ms = int(time.time() * 1000)
    await client.post("/events", json=_event(status=429, prompt_tokens=0, completion_tokens=0))
    await client.post("/events", json=_event(status=200, prompt_tokens=50, completion_tokens=10))
    frames = _parse_sse((await client.get("/api/stream", params={"once": 1})).text)
    snap = next(f[2] for f in frames if f[0] == "snapshot")
    live = snap["workers"][0]["live"]
    assert live["throttled_last_minute"] == 1
    assert live["tokens_per_minute"] == 60
    assert snap["totals"]["throttled_last_minute"] == 1
    assert now_ms <= snap["timestamp"] * 1000 + 1000


async def test_stream_replays_events_after_last_event_id(client):
    for i in range(3):
        await client.post("/events", json=_event(node=f"n{i}"))
    all_seqs = [e.seq for e in dashboard_app.events_buffer]

    resp = await client.get("/api/stream", params={"once": 1}, headers={"Last-Event-ID": str(all_seqs[0])})
    events_frames = [f for f in _parse_sse(resp.text) if f[0] == "events"]
    assert events_frames, "should replay the events after the given id"
    replayed = [e["seq"] for e in events_frames[0][2]]
    assert replayed == all_seqs[1:]
    assert events_frames[0][1] == str(all_seqs[-1])  # SSE id is the last seq sent


async def test_fresh_client_gets_recent_backlog(client):
    await client.post("/events", json=_event(node="only"))
    frames = _parse_sse((await client.get("/api/stream", params={"once": 1})).text)
    events_frames = [f for f in frames if f[0] == "events"]
    assert events_frames and events_frames[0][2][-1]["node"] == "only"


# ---------------------------------------------------------------------------
# Scenarios listing
# ---------------------------------------------------------------------------

async def test_scenarios_endpoint_lists_and_validates(client, tmp_path, monkeypatch):
    scen = tmp_path / "scen"
    scen.mkdir()
    (scen / "good.yaml").write_text(
        "run: good-run\nscenarios:\n  s1:\n    agent: triage\n"
        "    executor: {kind: constant_vus, vus: 1, duration: 1s}\n"
    )
    (scen / "bad.yaml").write_text("run: bad\nscenarios:\n  s1:\n    agent: nope\n")
    monkeypatch.setattr(dashboard_app, "SCENARIOS_DIR", scen)

    body = (await client.get("/api/scenarios")).json()
    by_file = {row["file"]: row for row in body}
    assert by_file["good.yaml"]["valid"] is True
    assert by_file["good.yaml"]["scenarios"][0]["name"] == "s1"
    assert by_file["bad.yaml"]["valid"] is False
    assert by_file["bad.yaml"]["error"]


async def test_scenarios_endpoint_empty_when_dir_missing(client, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_app, "SCENARIOS_DIR", tmp_path / "absent")
    assert (await client.get("/api/scenarios")).json() == []


# ---------------------------------------------------------------------------
# Retired legacy routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("POST", "/register"),
    ("GET", "/control/w1"),
    ("POST", "/control/w1"),
    ("POST", "/limits/team-a"),
    ("POST", "/act/1"),
    ("POST", "/reset"),
    ("GET", "/events/stream"),
])
async def test_legacy_routes_are_gone(client, method, path):
    resp = await client.request(method, path, json={})
    # Unknown routes hit the SPA catch-all, which only answers GET. Anything
    # other than a 200 OK from a real handler counts as gone.
    assert resp.status_code in (404, 405) or (method == "GET" and resp.headers["content-type"].startswith("text/html"))
    assert not (method == "POST" and resp.status_code == 200)


async def test_spa_route_serves_something(client):
    resp = await client.get("/")
    assert resp.status_code == 200


async def test_sparkline_last_point_matches_live_rate(client):
    """The gauge and the trace must describe the same 60 seconds. The last point of
    each worker's series is the trailing-minute total, so it equals the live rate."""
    await client.post("/api/workers", json={"worker_id": "w1", "team": "t"})
    await client.post("/events", json=_event(prompt_tokens=300, completion_tokens=40))
    await client.post("/events", json=_event(prompt_tokens=100, completion_tokens=10))
    frames = _parse_sse((await client.get("/api/stream", params={"once": 1})).text)
    snap = next(f[2] for f in frames if f[0] == "snapshot")
    worker = snap["workers"][0]
    assert worker["series"][-1] == worker["live"]["tokens_per_minute"] == 450
    assert len(worker["series"]) == len(snap["buckets"]) == 12
