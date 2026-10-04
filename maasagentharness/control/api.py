"""Phase 1 control-plane API — mounted at /api/workers in dashboard/app.py.

See docs/phase-1-5-implementation-plan.md §Phase 1 for the full contract
table. This module only wires HTTP routes to WorkerStore/Supervisor calls;
it holds no state of its own.
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from maasagentharness.control.budgets import (
    BudgetUpdate,
    BudgetView,
    ResolvedBudgetView,
    resolve_budget,
)
from maasagentharness.control.models import (
    DesiredPatch,
    DesiredStateResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    WorkerRecord,
)
from maasagentharness.control.store import WorkerStore
from maasagentharness.metrics.store import default_store
from maasagentharness.control.supervisor import ManagedWorkerError, Supervisor

router = APIRouter(prefix="/api/workers", tags=["workers"])
budgets_router = APIRouter(prefix="/api/budgets", tags=["budgets"])
runs_router = APIRouter(prefix="/api/runs", tags=["runs"])


def _store(request: Request) -> WorkerStore:
    return request.app.state.worker_store


def _supervisor(request: Request) -> Supervisor:
    return request.app.state.supervisor


@router.post("", response_model=WorkerRecord)
async def register_worker(reg: RegisterRequest, request: Request):
    store = _store(request)
    return await store.register(
        worker_id=reg.worker_id,
        team=reg.team,
        display_name=reg.display_name or reg.worker_id,
        task_description=reg.task_description,
        managed=reg.managed,
        agent=reg.agent,
        mode=reg.mode,
    )


@router.get("", response_model=list[WorkerRecord])
async def list_workers(request: Request):
    return await _store(request).list()


@router.get("/{worker_id}/desired", response_model=DesiredStateResponse)
async def get_desired(worker_id: str, request: Request):
    store = _store(request)
    record = await store.get(worker_id)
    if record is None:
        return DesiredStateResponse(unregistered=True)
    budgets = await store.list_budgets()
    resolved = resolve_budget(record, budgets)
    limit_tpm = (
        int(resolved.budget.tokens_per_minute)
        if resolved.budget.tokens_per_minute is not None
        else None
    )
    return DesiredStateResponse(
        state=record.desired_state,
        agent=record.desired_agent,
        mode=record.desired_mode,
        budget_id=record.desired_budget_id,
        generation=record.generation,
        interrupt_token=record.interrupt_token,
        unregistered=False,
        limit_tpm=limit_tpm,
    )


@router.get("/{worker_id}/budget", response_model=ResolvedBudgetView)
async def get_worker_budget(worker_id: str, request: Request):
    store = _store(request)
    record = await store.get(worker_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker '{worker_id}'")
    budgets = await store.list_budgets()
    resolved = resolve_budget(record, budgets)
    return ResolvedBudgetView.from_resolved(resolved)


@router.patch("/{worker_id}/desired", response_model=WorkerRecord)
async def patch_desired(worker_id: str, patch: DesiredPatch, request: Request):
    record = await _store(request).patch_desired(
        worker_id, patch.model_dump(exclude_none=True)
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown worker '{worker_id}' — it must POST /api/workers before its desired state can be changed.",
        )
    return record


@router.post("/{worker_id}/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(worker_id: str, hb: HeartbeatRequest, request: Request):
    store = _store(request)
    existing = await store.get(worker_id)
    record = await store.heartbeat(
        worker_id,
        applied_generation=hb.applied_generation,
        phase=hb.phase.value,
        current_node=hb.current_node,
        iteration=hb.iteration,
        last_error=hb.last_error,
        applied_interrupt_token=hb.applied_interrupt_token,
        team=existing.team if existing else "unknown",
        display_name=existing.display_name if existing else worker_id,
    )
    default_store.set_dropped(worker_id, hb.dropped_iterations_total)
    return HeartbeatResponse(lease_expires_at=record.lease_expires_at or 0.0)


@router.post("/{worker_id}/interrupt", status_code=202)
async def interrupt_worker(worker_id: str, request: Request):
    record = await _store(request).request_interrupt(worker_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker '{worker_id}'")
    return {"worker_id": worker_id, "interrupt_token": record.interrupt_token}


@router.post("/{worker_id}/kill", status_code=202)
async def kill_worker(worker_id: str, request: Request):
    store = _store(request)
    supervisor = _supervisor(request)
    record = await store.get(worker_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker '{worker_id}'")
    if not record.managed:
        raise HTTPException(
            status_code=409,
            detail=f"Worker '{worker_id}' is not managed by this dashboard — it was started "
                   f"externally and cannot be killed via the API. Use Stop, or kill its process directly.",
        )
    try:
        await supervisor.kill(worker_id)
    except ManagedWorkerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"worker_id": worker_id, "status": "killed"}


@router.post("/{worker_id}/spawn", response_model=WorkerRecord)
async def spawn_worker(worker_id: str, request: Request):
    """Spawn a managed worker process that was registered with managed=true
    but isn't running yet. Separate from /register because registration
    (declaring the worker should exist) and spawning (actually starting the
    process) are different operations — you can register a managed worker's
    desired config before deciding to start it.
    """
    store = _store(request)
    supervisor = _supervisor(request)
    record = await store.get(worker_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker '{worker_id}'")
    if not record.managed:
        raise HTTPException(status_code=409, detail=f"Worker '{worker_id}' is not managed")
    try:
        pid = await supervisor.spawn(worker_id, {
            "USER_ID": worker_id,
            "TEAM_ID": record.team,
            "DISPLAY_NAME": record.display_name,
        })
    except ManagedWorkerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    updated = await store.get(worker_id)
    return updated


@router.delete("/{worker_id}", status_code=204)
async def forget_worker(worker_id: str, request: Request):
    store = _store(request)
    record = await store.get(worker_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker '{worker_id}'")
    if record.status not in ("stopped", "lost"):
        raise HTTPException(
            status_code=409,
            detail=f"Worker '{worker_id}' is {record.status} — stop it before forgetting it.",
        )
    await store.delete(worker_id)


# ---------------------------------------------------------------------------
# Phase 2: budgets
# ---------------------------------------------------------------------------

@budgets_router.get("", response_model=list[BudgetView])
async def list_budgets(request: Request):
    store = _store(request)
    return [BudgetView.from_budget(b) for b in await store.list_budgets()]


@budgets_router.get("/{budget_id}", response_model=BudgetView)
async def get_budget(budget_id: str, request: Request):
    """Phase 3: lets a scenario (or a direct `curl`) fetch one named budget
    by id instead of listing all of them client-side.
    """
    budget = await _store(request).get_budget(budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail=f"Unknown budget '{budget_id}'")
    return BudgetView.from_budget(budget)


@budgets_router.put("/{budget_id}", response_model=BudgetView)
async def put_budget(budget_id: str, update: BudgetUpdate, request: Request):
    """Full replace, not a patch — see BudgetUpdate's docstring. Upserts:
    PUT-ing an unknown `budget_id` creates it.
    """
    store = _store(request)
    budget = await store.put_budget(
        budget_id,
        scope=update.scope,
        scope_ref=update.scope_ref,
        tokens_per_minute=update.tokens_per_minute,
        requests_per_minute=update.requests_per_minute,
        max_concurrency=update.max_concurrency,
        headroom=update.headroom,
        enforcement=update.enforcement,
    )
    return BudgetView.from_budget(budget)


@budgets_router.delete("/{budget_id}", status_code=204)
async def delete_budget(budget_id: str, request: Request):
    store = _store(request)
    assigned = await store.count_workers_assigned_to_budget(budget_id)
    if assigned > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Budget '{budget_id}' is still assigned to {assigned} worker(s) — "
                   f"reassign or stop them before deleting it.",
        )
    deleted = await store.delete_budget(budget_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Unknown budget '{budget_id}'")


# ---------------------------------------------------------------------------
# Phase 3: runs — spawns one managed worker process per named scenario in
# a run file, via the Phase 1 supervisor. This is what
# docs/phase-1-5-implementation-plan.md §3.5 calls "the UI's launch
# scenario action" — Phase 5 builds the button; this is the route it hits.
# `uv run harness run scenario.yaml` (scenario/cli.py) does not need this
# route at all — it runs scenarios in-process directly. This route exists
# specifically for the case where you want each scenario to be its own
# independently manageable/killable worker, visible and controllable from
# the dashboard like any other.
# ---------------------------------------------------------------------------

class RunLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_path: str
    scenario_names: Optional[List[str]] = None  # defaults to every scenario in the file


class RunLaunchResponse(BaseModel):
    run: str
    spawned: List[str]  # worker_ids, one per launched scenario


@runs_router.post("", response_model=RunLaunchResponse)
async def launch_run(req: RunLaunchRequest, request: Request):
    # Local imports — the scenario package doesn't need to be on the
    # import path for every dashboard request, only this one route.
    from maasagentharness.scenario.loader import load_run_config
    from maasagentharness.scenario.schema import ScenarioConfigError

    try:
        run_config = load_run_config(req.scenario_path)
    except ScenarioConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))

    store = _store(request)
    supervisor = _supervisor(request)

    names = req.scenario_names or list(run_config.scenarios.keys())
    unknown = [n for n in names if n not in run_config.scenarios]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown scenario name(s): {', '.join(unknown)}")

    dashboard_url = str(request.base_url).rstrip("/")
    spawned: List[str] = []
    for name in names:
        scenario = run_config.scenarios[name]
        worker_id = f"{run_config.run}-{name}"
        await store.register(
            worker_id=worker_id,
            team=run_config.run,
            display_name=worker_id,
            task_description=f"scenario '{name}' ({scenario.executor.kind})",
            managed=True,
            agent=scenario.agent,
            mode=scenario.mode,
        )
        try:
            await supervisor.spawn(
                worker_id,
                env_overrides={"DASHBOARD_URL": dashboard_url},
                module="maasagentharness.scenario.cli",
                args=["run", req.scenario_path, "--scenario", name],
            )
        except ManagedWorkerError as e:
            raise HTTPException(status_code=409, detail=str(e))
        spawned.append(worker_id)

    return RunLaunchResponse(run=run_config.run, spawned=spawned)
