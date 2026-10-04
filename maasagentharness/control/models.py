"""Typed models for the Phase 1 control plane.

See docs/phase-1-5-implementation-plan.md §Phase 1 for the design rationale.
The core fix this package exists to deliver: desired state (what the control
plane wants) and observed state (what the worker last reported) are two
different things, tracked separately, reconciled through a generation
counter — never rendered or treated as the same value. That is what Phase 0
could not fix without this.
"""
import time
from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Phase(StrEnum):
    """Observed lifecycle phase, reported by the worker itself via heartbeat.

    `unknown` and `lost` are never set BY the worker — they are derived
    server-side (see `derive_status`) from heartbeat recency and lease
    expiry. A worker only ever reports starting/running/draining/
    stopped/errored.
    """
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    ERRORED = "errored"
    # Derived-only, never set by a worker directly:
    UNKNOWN = "unknown"
    LOST = "lost"


DesiredStateLiteral = Literal["running", "stopped"]
AgentTypeLiteral = str  # "triage" | "research" | "assistant" | custom
ModeLiteral = Literal["naive", "paced", "runaway"]


class RegisterRequest(BaseModel):
    """POST /api/workers body — called once at worker startup, and again on
    every reconnect. Idempotent: re-registering an existing worker_id updates
    its display metadata but never touches desired state, so a worker that
    restarts (or merely reconnects after a network blip) doesn't get its
    in-flight command silently reset.
    """
    model_config = ConfigDict(extra="forbid")

    worker_id: str
    team: str
    display_name: str = ""
    task_description: str = ""
    managed: bool = False
    agent: str = "triage"
    mode: ModeLiteral = "naive"


class DesiredPatch(BaseModel):
    """PATCH /api/workers/{id}/desired body.

    `extra="forbid"` plus every field optional means this can only ever
    change fields it's explicitly given — it is impossible to use this model
    to accidentally construct a brand-new, partially-filled record the way
    the pre-Phase-1 `POST /control/{id}` could (docs/phase-0-hotfixes.md #0.2
    fixed that for the v1 API; this is the same discipline for v2).
    """
    model_config = ConfigDict(extra="forbid")

    state: Optional[DesiredStateLiteral] = None
    agent: Optional[str] = None
    mode: Optional[ModeLiteral] = None
    budget_id: Optional[str] = None  # Phase 2 — reserved, unused until then


class HeartbeatRequest(BaseModel):
    """POST /api/workers/{id}/heartbeat body — sent every ~1s by a running
    worker. `applied_generation` is the mechanism that makes "pending" a
    derived fact rather than a status the UI has to infer: if it's behind
    the record's `generation`, the worker hasn't acted on the latest command
    yet.
    """
    model_config = ConfigDict(extra="forbid")

    applied_generation: int
    phase: Phase
    current_node: Optional[str] = None
    iteration: int = 0
    last_error: Optional[str] = None
    # Interrupt is poll-based, not an in-process task handle, because the
    # control plane and the worker are different processes talking over
    # HTTP — see docs/phase-1-5-implementation-plan.md's "Interrupt"
    # reconciliation note. The worker echoes the last interrupt_token it
    # acted on so the API can tell "interrupt requested" apart from
    # "interrupt delivered".
    applied_interrupt_token: int = 0


class HeartbeatResponse(BaseModel):
    lease_expires_at: float


class DesiredStateResponse(BaseModel):
    """GET /api/workers/{id}/desired response — polled every ~1s by a
    worker. Deliberately small and cheap: this is the hot path.
    """
    state: DesiredStateLiteral = "stopped"
    agent: str = "triage"
    mode: ModeLiteral = "naive"
    budget_id: Optional[str] = None
    generation: int = 0
    interrupt_token: int = 0
    unregistered: bool = False
    # Transitional — see control/limits.py's module docstring. Phase 2
    # replaces this with real Budget resolution; until then it's how the
    # worker's pacer stays in sync with the (still-shipped) dashboard's TPM
    # editor after Phase 1 moved desired-state polling off the legacy
    # /control/{id} endpoint that used to carry this field.
    limit_tpm: Optional[int] = None


class WorkerRecord(BaseModel):
    """Full record as returned by GET /api/workers and GET /api/workers/{id}.

    `status` is ALWAYS computed server-side by `derive_status` at read time —
    never stored, never recomputed by a client. This is deliberate: Phase 0's
    predecessor bug (finding #4) was exactly a client rendering desired state
    as if it were observed state. Here there is only one place status logic
    can live.
    """
    worker_id: str
    team: str
    display_name: str
    task_description: str
    managed: bool
    pid: Optional[int] = None

    desired_state: DesiredStateLiteral
    desired_agent: str
    desired_mode: ModeLiteral
    desired_budget_id: Optional[str] = None
    generation: int
    interrupt_token: int = 0

    phase: Phase
    applied_generation: int
    applied_interrupt_token: int = 0
    current_node: Optional[str] = None
    iteration: int = 0
    last_error: Optional[str] = None
    heartbeat_at: Optional[float] = None
    lease_expires_at: Optional[float] = None

    created_at: float
    updated_at: float

    status: str = ""  # populated by derive_status before this leaves the store


def derive_status(record: WorkerRecord, now: Optional[float] = None) -> str:
    """The single place "is this worker actually okay?" is decided.

    Order matters: a lease that has expired always wins (the worker is gone,
    full stop — anything else is secondary), THEN pending-command detection,
    THEN staleness, and only once all of those are clear do we trust the
    worker's own self-reported phase.
    """
    now = now if now is not None else time.time()
    if record.lease_expires_at is None or now > record.lease_expires_at:
        return Phase.LOST
    if record.applied_generation < record.generation:
        return "pending"
    if record.heartbeat_at is None:
        return Phase.UNKNOWN
    from maasagentharness.control.store import HEARTBEAT_INTERVAL
    if now - record.heartbeat_at > HEARTBEAT_INTERVAL * 3:
        return Phase.UNKNOWN
    return str(record.phase)
