"""Phase 2 tests: Budget resolution, the pacer's shadow mode, the
/api/budgets routes, and the legacy /control + /limits integration that
replaced control/limits.py's dict bridge.
"""
import asyncio

import httpx
import pytest

from maasagentharness.control.budgets import Budget, resolve_budget
from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.dashboard import app as dashboard_app
from maasagentharness.harness import ModelHarness
from maasagentharness.pacer import TokenBucket


# ---------------------------------------------------------------------------
# resolve_budget — pure function, no store needed
# ---------------------------------------------------------------------------

class _FakeWorker:
    def __init__(self, team, desired_budget_id=None):
        self.team = team
        self.desired_budget_id = desired_budget_id


def test_resolve_budget_worker_assignment_wins():
    w = _FakeWorker(team="team-a", desired_budget_id="worker-budget")
    budgets = [
        Budget(budget_id="worker-budget", scope="worker", scope_ref="w1", tokens_per_minute=500),
        Budget(budget_id="team-budget", scope="team", scope_ref="team-a", tokens_per_minute=1000),
        Budget(budget_id="global-budget", scope="global", tokens_per_minute=2000),
    ]
    resolved = resolve_budget(w, budgets)
    assert resolved.resolved_via == "worker_assignment"
    assert resolved.budget.tokens_per_minute == 500


def test_resolve_budget_falls_back_to_team():
    w = _FakeWorker(team="team-a")
    budgets = [
        Budget(budget_id="team-budget", scope="team", scope_ref="team-a", tokens_per_minute=1000),
        Budget(budget_id="global-budget", scope="global", tokens_per_minute=2000),
    ]
    resolved = resolve_budget(w, budgets)
    assert resolved.resolved_via == "team"
    assert resolved.budget.tokens_per_minute == 1000


def test_resolve_budget_falls_back_to_global():
    w = _FakeWorker(team="team-a")
    budgets = [Budget(budget_id="global-budget", scope="global", tokens_per_minute=2000)]
    resolved = resolve_budget(w, budgets)
    assert resolved.resolved_via == "global"


def test_resolve_budget_default_when_nothing_matches():
    w = _FakeWorker(team="team-a")
    resolved = resolve_budget(w, budgets=[])
    assert resolved.resolved_via == "default"
    assert resolved.budget.enforcement == "off"
    assert resolved.budget.tokens_per_minute is None


def test_effective_rate_applies_headroom_exactly():
    b = Budget(budget_id="b1", scope="global", tokens_per_minute=1000, headroom=0.8)
    assert b.effective_tokens_per_minute == 800.0


# ---------------------------------------------------------------------------
# TokenBucket shadow mode
# ---------------------------------------------------------------------------

async def test_shadow_acquire_never_blocks():
    bucket = TokenBucket(target_tpm=60)  # 1 token/sec
    start = asyncio.get_event_loop().time()
    wait_s = await bucket.acquire(600, shadow=True)  # would need 10s for real
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.5  # never actually waited
    assert wait_s > 9.0  # but reports accurately how long it WOULD have waited


async def test_shadow_acquire_allows_negative_balance():
    bucket = TokenBucket(target_tpm=60)
    await bucket.acquire(600, shadow=True)
    assert bucket.tokens < 0  # the "how far over budget" signal


async def test_enforcing_acquire_still_blocks_for_real():
    bucket = TokenBucket(target_tpm=6000)  # 100 tokens/sec — fast enough for a short test
    await bucket.acquire(50)  # within capacity, should return immediately
    assert bucket.tokens == pytest.approx(5950, rel=0.01)


# ---------------------------------------------------------------------------
# ModelHarness pacing-action decision
# ---------------------------------------------------------------------------

def test_harness_client_bucket_only_blocks_in_paced_mode():
    budget = Budget(budget_id="b1", scope="global", tokens_per_minute=1000, enforcement="client_bucket")
    naive = ModelHarness(agent_name="t", mode="naive", budget=budget)
    paced = ModelHarness(agent_name="t", mode="paced", budget=budget)
    runaway = ModelHarness(agent_name="t", mode="runaway", budget=budget)
    assert naive._pacing_action() == "none"
    assert paced._pacing_action() == "block"
    assert runaway._pacing_action() == "none"


def test_harness_shadow_enforcement_runs_regardless_of_mode():
    budget = Budget(budget_id="b1", scope="global", tokens_per_minute=1000, enforcement="shadow")
    for mode in ("naive", "paced", "runaway"):
        h = ModelHarness(agent_name="t", mode=mode, budget=budget)
        assert h._pacing_action() == "shadow"


def test_harness_off_enforcement_never_paces():
    budget = Budget(budget_id="b1", scope="global", tokens_per_minute=1000, enforcement="off")
    h = ModelHarness(agent_name="t", mode="paced", budget=budget)
    assert h._pacing_action() == "none"


def test_harness_without_budget_falls_back_to_pre_phase2_behavior():
    """No budget wired in (e.g. probe.py, or a test) -> old behavior:
    pacer only active in "paced" mode, sized from settings."""
    h_paced = ModelHarness(agent_name="t", mode="paced")
    h_naive = ModelHarness(agent_name="t", mode="naive")
    assert h_paced.pacer is not None
    assert h_naive.pacer is None


# ---------------------------------------------------------------------------
# /api/budgets routes
# ---------------------------------------------------------------------------

@pytest.fixture
async def api_client(tmp_path):
    store = WorkerStore(str(tmp_path / "budgets.db"))
    await store.connect()
    dashboard_app.app.state.worker_store = store
    dashboard_app.app.state.supervisor = Supervisor(store)
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await store.close()


async def test_fresh_store_bootstraps_global_default_budget(api_client):
    resp = await api_client.get("/api/budgets")
    assert resp.status_code == 200
    budgets = resp.json()
    assert len(budgets) == 1
    assert budgets[0]["scope"] == "global"
    assert budgets[0]["tokens_per_minute"] == 2000
    assert budgets[0]["effective_tokens_per_minute"] == pytest.approx(1800.0)


async def test_put_budget_upserts_and_shows_effective_rate(api_client):
    resp = await api_client.put("/api/budgets/team:acme", json={
        "scope": "team", "scope_ref": "acme", "tokens_per_minute": 3000, "headroom": 0.9,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["tokens_per_minute"] == 3000
    assert body["effective_tokens_per_minute"] == pytest.approx(2700.0)


async def test_put_budget_rejects_extra_fields(api_client):
    resp = await api_client.put("/api/budgets/b1", json={"scope": "global", "bogus": 1})
    assert resp.status_code == 422


async def test_delete_budget_409_when_assigned(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    await api_client.put("/api/budgets/worker:w1", json={"scope": "worker", "scope_ref": "w1", "tokens_per_minute": 500})
    await api_client.patch("/api/workers/w1/desired", json={"budget_id": "worker:w1"})

    resp = await api_client.delete("/api/budgets/worker:w1")
    assert resp.status_code == 409

    # Reassign away to the bootstrapped global default, freeing up the
    # worker-scoped budget to be deleted.
    await api_client.patch("/api/workers/w1/desired", json={"budget_id": "global-default"})
    resp = await api_client.delete("/api/budgets/worker:w1")
    assert resp.status_code == 204


async def test_delete_unknown_budget_404(api_client):
    resp = await api_client.delete("/api/budgets/never-existed")
    assert resp.status_code == 404


async def test_get_worker_budget_resolves_and_reports_scope(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    resp = await api_client.get("/api/workers/w1/budget")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_via"] == "global"
    assert body["tokens_per_minute"] == 2000

    await api_client.put("/api/budgets/team:team-a", json={"scope": "team", "scope_ref": "team-a", "tokens_per_minute": 500})
    resp = await api_client.get("/api/workers/w1/budget")
    assert resp.json()["resolved_via"] == "team"
    assert resp.json()["tokens_per_minute"] == 500


async def test_desired_endpoint_carries_resolved_limit_tpm(api_client):
    await api_client.post("/api/workers", json={"worker_id": "w1", "team": "team-a"})
    resp = await api_client.get("/api/workers/w1/desired")
    assert resp.json()["limit_tpm"] == 2000  # the bootstrapped global default


# ---------------------------------------------------------------------------
# Legacy /control + /limits integration (replaces control/limits.py)
# ---------------------------------------------------------------------------

@pytest.fixture
async def legacy_client(tmp_path):
    store = WorkerStore(str(tmp_path / "legacy.db"))
    await store.connect()
    dashboard_app.app.state.worker_store = store
    dashboard_app.app.state.supervisor = Supervisor(store)
    dashboard_app.registered_mode_defaults.clear()
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await store.close()


async def test_worker_level_override_beats_team_update(legacy_client):
    """The bug this fixes: the old /limits/{team} bulk-wrote every worker's
    entry, silently clobbering individual overrides. A real scoped Budget
    means a worker-level edit now survives a later team-level edit.
    """
    await legacy_client.post("/register", json={"user": "w1", "team": "team-a"})
    await legacy_client.post("/register", json={"user": "w2", "team": "team-a"})

    await legacy_client.post("/control/w1", json={"limit_tpm": 999})  # w1-specific override
    await legacy_client.post("/limits/team-a", json={"limit_tpm": 4000})  # team-wide change

    w1 = (await legacy_client.get("/control/w1")).json()
    w2 = (await legacy_client.get("/control/w2")).json()
    assert w1["limit_tpm"] == 999  # survived the team update
    assert w2["limit_tpm"] == 4000  # picked up the team update (no override of its own)


async def test_control_patch_limit_tpm_sets_resolved_via_worker_assignment(legacy_client):
    await legacy_client.post("/register", json={"user": "w1", "team": "team-a"})
    resp = await legacy_client.post("/control/w1", json={"limit_tpm": 777})
    body = resp.json()
    assert body["limit_tpm"] == 777
    assert body["resolved_via"] == "worker_assignment"


# ---------------------------------------------------------------------------
# End-to-end: a real AgentWorker picks up a live budget/enforcement change
# ---------------------------------------------------------------------------

async def test_agent_worker_picks_up_shadow_enforcement_live(tmp_path):
    """The risk this guards against: every piece above (resolve_budget,
    update_budget, the /api/workers/{id}/budget route) can be unit-tested
    correctly while the actual poll loop still forgets to call them. This
    drives a real AgentWorker against the real app and checks its live
    ModelHarness's resolved enforcement actually changes.
    """
    import asyncio as _asyncio

    import httpx as _httpx

    from maasagentharness.runner import AgentWorker

    store = WorkerStore(str(tmp_path / "e2e_budget.db"))
    await store.connect()
    dashboard_app.app.state.worker_store = store
    dashboard_app.app.state.supervisor = Supervisor(store)
    transport = _httpx.ASGITransport(app=dashboard_app.app)

    worker = AgentWorker(user_id="budget-e2e", team_id="team-budget-e2e", dashboard_url="http://test")
    worker.control.transport = transport

    run_task = asyncio.ensure_future(worker.run_loop())
    try:
        # Let it register and take its first poll tick.
        for _ in range(20):
            if worker.budget is not None:
                break
            await asyncio.sleep(0.05)
        assert worker.budget is not None
        assert worker.budget.enforcement == "client_bucket"  # the bootstrapped global default

        async with _httpx.AsyncClient(transport=transport, base_url="http://test") as admin:
            await admin.put("/api/budgets/worker:budget-e2e", json={
                "scope": "worker", "scope_ref": "budget-e2e",
                "tokens_per_minute": 300, "enforcement": "shadow",
            })
            await admin.patch("/api/workers/budget-e2e/desired", json={"budget_id": "worker:budget-e2e"})

        converged = False
        for _ in range(40):
            if worker.harness is not None and worker.harness.budget is not None and worker.harness.budget.enforcement == "shadow":
                converged = True
                break
            await asyncio.sleep(0.1)
        assert converged, f"worker never picked up shadow enforcement: {worker.budget}"
        assert worker.harness._pacing_action() == "shadow"
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await store.close()
