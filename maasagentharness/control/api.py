"""Phase 1 control-plane API — mounted at /api/workers in dashboard/app.py.

See docs/phase-1-5-implementation-plan.md §Phase 1 for the full contract
table. This module only wires HTTP routes to WorkerStore/Supervisor calls;
it holds no state of its own.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from maasagentharness.control.models import (
    DesiredPatch,
    DesiredStateResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    WorkerRecord,
)
from maasagentharness.control.limits import effective_limit_tpm
from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import ManagedWorkerError, Supervisor

router = APIRouter(prefix="/api/workers", tags=["workers"])


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
    record = await _store(request).get(worker_id)
    if record is None:
        return DesiredStateResponse(unregistered=True)
    return DesiredStateResponse(
        state=record.desired_state,
        agent=record.desired_agent,
        mode=record.desired_mode,
        budget_id=record.desired_budget_id,
        generation=record.generation,
        interrupt_token=record.interrupt_token,
        unregistered=False,
        limit_tpm=effective_limit_tpm(record.worker_id, record.team),
    )


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
