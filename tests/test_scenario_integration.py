"""Phase 3 integration tests: a real AgentWorker.run_scenario() against the
real FastAPI app (httpx.ASGITransport — no sockets), including live Stop
mid-scenario, and the POST /api/runs orchestration route (real subprocess
spawn, cleaned up via a real kill — same pattern as
tests/test_control_plane.py's supervisor test).
"""
import asyncio

import httpx
import pytest

from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.dashboard import app as dashboard_app
from maasagentharness.runner import AgentWorker
from maasagentharness.scenario.schema import ExecutorConfig, ScenarioConfig


@pytest.fixture
async def store(tmp_path):
    s = WorkerStore(str(tmp_path / "scenario_e2e.db"))
    await s.connect()
    dashboard_app.app.state.worker_store = s
    dashboard_app.app.state.supervisor = Supervisor(s)
    yield s
    await s.close()


@pytest.fixture
def transport():
    return httpx.ASGITransport(app=dashboard_app.app)


async def test_run_scenario_completes_and_reports_stats(store, transport):
    worker = AgentWorker(user_id="scenario-stats", team_id="team-x", dashboard_url="http://test")
    worker.control.transport = transport

    scenario = ScenarioConfig(
        agent="triage", mode="naive",
        executor=ExecutorConfig(kind="constant_vus", vus=2, duration="0.3s"),
    )
    stats = await worker.run_scenario(scenario)

    assert stats.iterations_started > 0
    assert stats.iterations_completed == stats.iterations_started
    assert stats.duration_s < 2.0

    record = await store.get("scenario-stats")
    assert record.phase == "stopped"
    assert record.desired_state == "stopped"
    assert record.iteration == stats.iterations_started * 3  # triage = 3 telemetry events/cycle


async def test_run_scenario_registers_as_a_visible_worker_while_running(store, transport):
    """While a scenario is mid-run, it should look like any other running
    worker in the store — this is what makes it show up in the dashboard.
    """
    worker = AgentWorker(user_id="scenario-visible", team_id="team-x", dashboard_url="http://test")
    worker.control.transport = transport

    scenario = ScenarioConfig(
        agent="triage", mode="naive",
        executor=ExecutorConfig(kind="constant_vus", vus=1, duration="1.0s"),
    )
    run_task = asyncio.ensure_future(worker.run_scenario(scenario))
    try:
        seen_running = False
        for _ in range(30):
            record = await store.get("scenario-visible")
            if record is not None and record.desired_state == "running":
                seen_running = True
                break
            await asyncio.sleep(0.05)
        assert seen_running
    finally:
        await run_task


async def test_run_scenario_stops_early_on_live_stop_command(store, transport):
    """The point of Phase 0/1's stop_event machinery carrying over to
    scenarios: a Stop command from the dashboard should cut a long-running
    scenario short, not just the dashboard's own fixed-cadence run_loop.
    """
    worker = AgentWorker(user_id="scenario-stop", team_id="team-x", dashboard_url="http://test")
    worker.control.transport = transport

    scenario = ScenarioConfig(
        agent="triage", mode="naive",
        executor=ExecutorConfig(kind="constant_vus", vus=2, duration="10.0s"),  # would run 10s uninterrupted
    )
    run_task = asyncio.ensure_future(worker.run_scenario(scenario))

    # Wait for it to actually start, then issue a live Stop via a second client.
    for _ in range(30):
        record = await store.get("scenario-stop")
        if record is not None and record.desired_state == "running":
            break
        await asyncio.sleep(0.05)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
        await admin.patch("/api/workers/scenario-stop/desired", json={"state": "stopped"})

    stats = await asyncio.wait_for(run_task, timeout=5.0)
    assert stats.duration_s < 5.0  # stopped well before the configured 10s


async def test_run_scenario_dropped_iterations_under_open_model_pressure(store, transport):
    """End-to-end version of the dropped-iteration unit test — confirms
    the signal survives the full run_scenario/control-plane wiring, not
    just the bare executor.
    """
    worker = AgentWorker(user_id="scenario-drop", team_id="team-x", dashboard_url="http://test")
    worker.control.transport = transport

    # assistant mode streams, which is slower per-call than triage; pair
    # a fast offered rate with a tight max_vus to force drops regardless.
    scenario = ScenarioConfig(
        agent="triage", mode="naive",
        executor=ExecutorConfig(kind="constant_arrival_rate", rate=6000, duration="0.5s", max_vus=1),
    )
    stats = await worker.run_scenario(scenario)
    assert stats.iterations_dropped > 0


# ---------------------------------------------------------------------------
# POST /api/runs — real subprocess spawn, one per named scenario
# ---------------------------------------------------------------------------

async def test_launch_run_spawns_one_managed_worker_per_scenario(store, transport, tmp_path):
    scenario_file = tmp_path / "run.yaml"
    scenario_file.write_text(
        """
run: itest-run
scenarios:
  sce-a:
    agent: triage
    executor: {kind: constant_vus, vus: 1, duration: 60s}
  sce-b:
    agent: research
    executor: {kind: constant_vus, vus: 1, duration: 60s}
"""
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs", json={"scenario_path": str(scenario_file)})
        assert resp.status_code == 200
        body = resp.json()
        assert body["run"] == "itest-run"
        assert set(body["spawned"]) == {"itest-run-sce-a", "itest-run-sce-b"}

        for worker_id in body["spawned"]:
            record = await store.get(worker_id)
            assert record is not None
            assert record.managed is True
            assert record.pid is not None

    # Clean up the real spawned subprocesses.
    for worker_id in body["spawned"]:
        await dashboard_app.app.state.supervisor.kill(worker_id)
        record = await store.get(worker_id)
        assert record.pid is None


async def test_launch_run_404_unknown_scenario_file(store, transport):
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs", json={"scenario_path": "/nonexistent/path.yaml"})
        assert resp.status_code == 400


async def test_launch_run_400_unknown_scenario_name(store, transport, tmp_path):
    scenario_file = tmp_path / "run.yaml"
    scenario_file.write_text(
        """
run: itest-run2
scenarios:
  only-one:
    agent: triage
    executor: {kind: constant_vus, vus: 1, duration: 10s}
"""
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/runs", json={
            "scenario_path": str(scenario_file), "scenario_names": ["does-not-exist"],
        })
        assert resp.status_code == 400
