"""Phase 2: scoped rate-limit Budgets.

This replaces control/limits.py's dict-based stopgap (see that module's own
docstring, which named this file as its successor) and is the fix for
redesign-spec.md findings #7-#11: the TPM editor racing a 1s-stale SSE
snapshot, two unreconciled sources of truth (a per-worker dict and a
per-team dict that didn't actually nest), the displayed number not being
the enforced number, and limits being silently inert in naive/runaway mode.

Resolution order is worker-assignment > team > global > a safe, explicit
default — a worker is never silently unbudgeted, it's budgeted "off" and
that's visible, not ambiguous.
"""
import time
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict

EnforcementLiteral = Literal["off", "client_bucket", "shadow"]
ScopeLiteral = Literal["global", "team", "worker"]
ResolvedViaLiteral = Literal["worker_assignment", "team", "global", "default"]


class Budget(BaseModel):
    budget_id: str
    scope: ScopeLiteral
    scope_ref: Optional[str] = None
    tokens_per_minute: Optional[int] = None
    requests_per_minute: Optional[int] = None
    max_concurrency: Optional[int] = None
    headroom: float = 0.90
    enforcement: EnforcementLiteral = "client_bucket"
    updated_at: float = 0.0

    @property
    def effective_tokens_per_minute(self) -> Optional[float]:
        if self.tokens_per_minute is None:
            return None
        return self.tokens_per_minute * self.headroom

    @property
    def effective_requests_per_minute(self) -> Optional[float]:
        if self.requests_per_minute is None:
            return None
        return self.requests_per_minute * self.headroom


class BudgetUpdate(BaseModel):
    """PUT /api/budgets/{id} body — a full replace, not a patch. This is
    deliberately a form submission, not a field that syncs live against a
    stream (docs/phase-1-5-implementation-plan.md §2.4) — the race that
    made the old TPM +/- buttons drop clicks (finding #7/#8) was exactly a
    live-synced field fighting a 1s-stale snapshot.
    """
    model_config = ConfigDict(extra="forbid")

    scope: ScopeLiteral
    scope_ref: Optional[str] = None
    tokens_per_minute: Optional[int] = None
    requests_per_minute: Optional[int] = None
    max_concurrency: Optional[int] = None
    headroom: float = 0.90
    enforcement: EnforcementLiteral = "client_bucket"


# The fallback of last resort — never None, so "no budget configured"
# is a distinct, visible state (enforcement="off") rather than an
# exception or a silently-assumed number.
DEFAULT_BUDGET = Budget(
    budget_id="__default__",
    scope="global",
    scope_ref=None,
    tokens_per_minute=None,
    enforcement="off",
    updated_at=0.0,
)


class ResolvedBudget(BaseModel):
    budget: Budget
    resolved_via: ResolvedViaLiteral


def resolve_budget(worker, budgets: List[Budget]) -> ResolvedBudget:
    """worker-assignment > team > global > DEFAULT_BUDGET.

    "worker-assignment" means `worker.desired_budget_id` names a budget
    directly. Nothing auto-matches a budget to a worker just because
    scope=="worker" and scope_ref==worker_id — that would make resolution
    depend on which budgets happen to exist elsewhere in the store, which
    the explicit-assignment model avoids. Setting scope=="worker" on a
    budget is organizational only (so `GET /api/budgets` reads clearly);
    actually applying it to a worker still requires
    `PATCH /api/workers/{id}/desired {"budget_id": "..."}`.
    """
    by_id = {b.budget_id: b for b in budgets}

    if worker.desired_budget_id and worker.desired_budget_id in by_id:
        return ResolvedBudget(budget=by_id[worker.desired_budget_id], resolved_via="worker_assignment")

    for b in budgets:
        if b.scope == "team" and b.scope_ref == worker.team:
            return ResolvedBudget(budget=b, resolved_via="team")

    for b in budgets:
        if b.scope == "global":
            return ResolvedBudget(budget=b, resolved_via="global")

    return ResolvedBudget(budget=DEFAULT_BUDGET, resolved_via="default")


class BudgetView(BaseModel):
    """API response shape — `Budget` plus its precomputed effective rates,
    so a client (the UI, or a curious `curl`) never has to multiply by
    `headroom` itself and risk showing a different number than what's
    actually enforced (redesign-spec.md finding #10).
    """
    budget_id: str
    scope: ScopeLiteral
    scope_ref: Optional[str] = None
    tokens_per_minute: Optional[int] = None
    requests_per_minute: Optional[int] = None
    max_concurrency: Optional[int] = None
    headroom: float
    enforcement: EnforcementLiteral
    effective_tokens_per_minute: Optional[float] = None
    effective_requests_per_minute: Optional[float] = None
    updated_at: float

    @classmethod
    def from_budget(cls, b: Budget) -> "BudgetView":
        return cls(
            budget_id=b.budget_id,
            scope=b.scope,
            scope_ref=b.scope_ref,
            tokens_per_minute=b.tokens_per_minute,
            requests_per_minute=b.requests_per_minute,
            max_concurrency=b.max_concurrency,
            headroom=b.headroom,
            enforcement=b.enforcement,
            effective_tokens_per_minute=b.effective_tokens_per_minute,
            effective_requests_per_minute=b.effective_requests_per_minute,
            updated_at=b.updated_at,
        )


class ResolvedBudgetView(BudgetView):
    resolved_via: ResolvedViaLiteral

    @classmethod
    def from_resolved(cls, r: ResolvedBudget) -> "ResolvedBudgetView":
        base = BudgetView.from_budget(r.budget)
        return cls(**base.model_dump(), resolved_via=r.resolved_via)
