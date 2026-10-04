"""Dashboard API route tests.

Phase 0 fix #0.14: converted from an `async def` script invoked only via
`__main__` (never run under pytest, nothing asserted in CI) to real
pytest-asyncio tests. Also covers the Phase 0 regressions directly:
unknown-worker-defaults-to-stopped (#0.1) and typed/validated control
patches (#0.2).
"""
import httpx
import pytest

from maasagentharness.dashboard import app as dashboard_app


@pytest.fixture
def _reset_state():
    """Each test gets a clean slate — module-level dicts persist across tests otherwise."""
    dashboard_app.control_states.clear()
    dashboard_app.agent_registry.clear()
    dashboard_app.team_limits.clear()
    dashboard_app.events_buffer.clear()
    yield
    dashboard_app.control_states.clear()
    dashboard_app.agent_registry.clear()
    dashboard_app.team_limits.clear()
    dashboard_app.events_buffer.clear()


@pytest.fixture
async def client(_reset_state):
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_spa_route_serves_something(client):
    resp = await client.get("/")
    assert resp.status_code == 200


async def test_unknown_worker_control_defaults_to_stopped(client):
    """Phase 0 fix #0.1 — this used to return state='running'."""
    resp = await client.get("/control/never-registered-id")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "stopped"
    assert body["unregistered"] is True


async def test_registered_worker_control_defaults_to_stopped_and_known(client):
    await client.post("/register", json={"user": "user-a", "team": "team-alpha"})
    resp = await client.get("/control/user-a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "stopped"
    assert body["unregistered"] is False


async def test_control_patch_rejects_unknown_user(client):
    """Phase 0 fix #0.2 — POSTing control for an unregistered user must 404,
    not silently fabricate a partial record."""
    resp = await client.post("/control/never-registered-id", json={"state": "running"})
    assert resp.status_code == 404


async def test_control_patch_rejects_unknown_field(client):
    """Phase 0 fix #0.2 — extra fields must be rejected (422), not silently accepted."""
    await client.post("/register", json={"user": "user-a", "team": "team-alpha"})
    resp = await client.post("/control/user-a", json={"bogus_field": 1})
    assert resp.status_code == 422


async def test_control_patch_does_not_reset_other_fields(client):
    """Phase 0 fix #0.2 — a state-only patch must not reset agent/mode."""
    await client.post("/register", json={"user": "user-a", "team": "team-alpha", "agent": "research", "mode": "paced"})
    resp = await client.post("/control/user-a", json={"state": "running"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent"] == "research"
    assert body["mode"] == "paced"
    assert body["state"] == "running"


async def test_act_1_and_act_2(client):
    await client.post("/register", json={"user": "user-a", "team": "team-alpha"})
    await client.post("/register", json={"user": "user-b", "team": "team-alpha"})

    resp = await client.post("/act/1")
    assert resp.status_code == 200

    resp = await client.post("/act/2")
    assert resp.status_code == 200
    assert resp.json()["control"]["user-a"]["state"] == "running"


async def test_limits_update(client):
    await client.post("/register", json={"user": "user-a", "team": "team-alpha"})
    resp = await client.post("/limits/team-alpha", json={"limit_tpm": 4000})
    assert resp.status_code == 200
    assert resp.json()["limit_tpm"] == 4000


async def test_event_ingest_assigns_increasing_seq(client):
    """Phase 0 fix #0.9 — events get a monotonically increasing seq on ingest."""
    event_payload = {
        "user": "user-a",
        "team": "team-alpha",
        "agent": "triage",
        "mode": "runaway",
        "run_id": "test-123",
        "node": "classify",
        "status": 429,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 150,
        "ratelimit_headers": {"retry-after": "5"},
        "attempt": 1,
    }
    resp1 = await client.post("/events", json=event_payload)
    resp2 = await client.post("/events", json=event_payload)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert dashboard_app.events_buffer[-2].seq < dashboard_app.events_buffer[-1].seq
