"""Phase 1 control-plane tests: WorkerStore, Supervisor, the /api/workers
routes, and an end-to-end AgentWorker run against the real FastAPI app
(no real sockets — httpx.ASGITransport in-process).
"""
import asyncio
import sys

import httpx
import pytest

from maasagentharness.control.models import Phase
from maasagentharness.control.store import LEASE_TTL, WorkerStore
from maasagentharness.control.supervisor import ManagedWorkerError, Supervisor
from maasagentharness.dashboard import app as dashboard_app
from maasagentharness.runner import AgentWorker


# ---------------------------------------------------------------------------
# WorkerStore
# ---------------------------------------------------------------------------

@pytest.fixture
async def store(tmp_path):
    s = WorkerStore(str(tmp_path / "store.db"))
    await s.connect()
    yield s
    await s.close()


async def test_register_is_idempotent_and_never_resets_desired_state(store):
    await store.register("w1", team="team-a", display_name="Worker One")
    await store.patch_desired("w1", {"state": "running", "mode": "paced"})

    # Re-registering (e.g. the worker reconnected after a network blip)
    # must NOT reset state/mode/generation back to defaults.
    record = await store.register("w1", team="team-a", display_name="Worker One (renamed)")
    assert record.desired_state == "running"
    assert record.desired_mode == "paced"
    assert record.display_name == "Worker One (renamed)"


async def test_patch_desired_bumps_generation(store):
    await store.register("w1", team="team-a")
    r0 = await store.get("w1")
    r1 = await store.patch_desired("w1", {"state": "running"})
    assert r1.generation == r0.generation + 1


async def test_patch_desired_returns_none_for_unknown_worker(store):
    result = await store.patch_desired("never-seen", {"state": "running"})
    assert result is None


async def test_heartbeat_self_heals_unknown_worker(store):
    """A heartbeat from a worker_id the store has never seen (e.g. the
    dashboard restarted and lost its db) re-creates the row instead of
    erroring — this is the "no persistence" fix actually working."""
    record = await store.heartbeat(
        "ghost-worker",
        applied_generation=0,
        phase="running",
        current_node="classify",
        iteration=3,
        last_error=None,
        team="team-a",
        display_name="Ghost",
    )
    assert record.worker_id == "ghost-worker"
    assert record.phase == Phase.RUNNING
    assert record.iteration == 3


async def test_derive_status_pending_when_applied_generation_behind(store):
    await store.register("w1", team="team-a")
    await store.patch_desired("w1", {"state": "running"})  # generation -> 1
    record = await store.get("w1")
    assert record.applied_generation == 0
    assert record.generation == 1
    assert record.status == "pending"


async def test_derive_status_lost_when_lease_expired(store):
    await store.register("w1", team="team-a")
    await store.heartbeat("w1", applied_generation=0, phase="running", current_node=None, iteration=0, last_error=None)
    record = await store.get("w1")
    assert record.status == "running"

    # Force the lease into the past directly (simulating LEASE_TTL elapsing).
    import time
    async with store._lock:
        await store._conn.execute("UPDATE workers SET lease_expires_at = ? WHERE worker_id = ?", (time.time() - 1, "w1"))
        await store._conn.commit()
    record = await store.get("w1")
    assert record.status == "lost"


async def test_request_interrupt_bumps_token(store):
    await store.register("w1", team="team-a")
    r1 = await store.request_interrupt("w1")
    r2 = await store.request_interrupt("w1")
    assert r2.interrupt_token == r1.interrupt_token + 1


async def test_delete_removes_worker(store):
    await store.register("w1", team="team-a")
    assert await store.delete("w1") is True
    assert await store.get("w1") is None


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

async def test_supervisor_spawn_and_kill_real_process(store):
    supervisor = Supervisor(store)
    await store.register("w1", team="team-a", managed=True)

    pid = await supervisor.spawn("w1", {})
    # Overwrite argv so the spawned process just sleeps instead of running
    # the real runner module (which would try to reach a dashboard).
    assert pid > 0
    assert supervisor.is_managed_by_this_process("w1")

    await supervisor.kill("w1")
    record = await store.get("w1")
    assert record.pid is None


async def test_supervisor_kill_unmanaged_raises(store):
    supervisor = Supervisor(store)
    with pytest.raises(ManagedWorkerError):
        await supervisor.kill("never-spawned-by-me")


# ---------------------------------------------------------------------------
# /api/workers routes
# ---------------------------------------------------------------------------

@pytest.fixture
async def api_client(tmp_path):
    s = WorkerStore(str(tmp_path / "api.db"))
    await s.connect()
    dashboard_app.app.state.worker_store = s
    dashboard_app.app.state.supervisor = Supervisor(s)
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await s.close()


async def test_api_register_and_get_desired(api_client):
    resp = await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a", "agent": "triage", "mode": "naive"})
    assert resp.status_code == 200

    resp = await api_client.get("/api/workers/w1/desired")
    body = resp.json()
    assert body["state"] == "stopped"
    assert body["unregistered"] is False
    assert body["generation"] == 0


async def test_api_get_desired_unknown_worker(api_client):
    resp = await api_client.get("/api/workers/never-seen/desired")
    assert resp.json()["unregistered"] is True
    assert resp.json()["state"] == "stopped"


async def test_api_patch_desired_404_unknown(api_client):
    resp = await api_client.patch("/api/workers/never-seen/desired", json={"state": "running"})
    assert resp.status_code == 404


async def test_api_patch_desired_rejects_extra_fields(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    resp = await api_client.patch("/api/workers/w1/desired", json={"bogus": 1})
    assert resp.status_code == 422


async def test_api_heartbeat_extends_lease(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    resp = await api_client.post("/api/workers/w1/heartbeat", json={
        "applied_generation": 0, "phase": "running", "current_node": "classify", "iteration": 1,
    })
    assert resp.status_code == 200
    assert resp.json()["lease_expires_at"] > 0


async def test_api_interrupt_unknown_404(api_client):
    resp = await api_client.post("/api/workers/never-seen/interrupt")
    assert resp.status_code == 404


async def test_api_kill_unmanaged_409(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a", "managed": False})
    resp = await api_client.post("/api/workers/w1/kill")
    assert resp.status_code == 409


async def test_api_forget_requires_stopped_or_lost(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    await api_client.patch("/api/workers/w1/desired", json={"state": "running"})
    await api_client.post("/api/workers/w1/heartbeat", json={
        "applied_generation": 1, "phase": "running", "current_node": None, "iteration": 0,
    })
    resp = await api_client.delete("/api/workers/w1")
    assert resp.status_code == 409

    await api_client.patch("/api/workers/w1/desired", json={"state": "stopped"})
    await api_client.post("/api/workers/w1/heartbeat", json={
        "applied_generation": 2, "phase": "stopped", "current_node": None, "iteration": 0,
    })
    resp = await api_client.delete("/api/workers/w1")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# End-to-end: a real AgentWorker against the real FastAPI app
# ---------------------------------------------------------------------------

async def test_agent_worker_reaches_running_phase_against_live_app(tmp_path):
    """Drives an actual AgentWorker.run_loop() against the actual FastAPI
    app over ASGITransport (no sockets), flips its desired state to
    running via a second client (simulating the UI's Start button), and
    asserts the worker's heartbeat-reported phase converges to RUNNING in
    the store — the core Phase 1 promise: Start/Stop are real, observable,
    round-tripping operations, not a dict write with no feedback loop.
    """
    store = WorkerStore(str(tmp_path / "e2e.db"))
    await store.connect()
    dashboard_app.app.state.worker_store = store
    dashboard_app.app.state.supervisor = Supervisor(store)
    transport = httpx.ASGITransport(app=dashboard_app.app)

    worker = AgentWorker(user_id="e2e-worker", team_id="team-e2e", dashboard_url="http://test")
    worker.control.transport = transport

    run_task = asyncio.create_task(worker.run_loop())
    try:
        # Give the worker a moment to register and send its first heartbeat
        # while stopped.
        for _ in range(20):
            record = await store.get("e2e-worker")
            if record is not None:
                break
            await asyncio.sleep(0.05)
        assert record is not None
        assert record.desired_state == "stopped"

        # Simulate the dashboard UI's Start button via a second client.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
            resp = await admin.patch("/api/workers/e2e-worker/desired", json={"state": "running"})
            assert resp.status_code == 200

        # Wait for the worker's own poll loop (ticks every 1s) to pick up
        # the new desired state and report it back via heartbeat.
        converged = False
        for _ in range(40):
            record = await store.get("e2e-worker")
            if record.phase == Phase.RUNNING and record.applied_generation >= 1:
                converged = True
                break
            await asyncio.sleep(0.1)
        assert converged, f"worker never converged to running: {record}"

        # Stop it again and confirm convergence back to stopped.
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
            resp = await admin.patch("/api/workers/e2e-worker/desired", json={"state": "stopped"})
            assert resp.status_code == 200

        converged = False
        for _ in range(40):
            record = await store.get("e2e-worker")
            if record.phase == Phase.STOPPED and record.applied_generation >= 2:
                converged = True
                break
            await asyncio.sleep(0.1)
        assert converged, f"worker never converged to stopped: {record}"
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await store.close()
