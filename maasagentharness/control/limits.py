"""Phase 1 -> Phase 2 bridge: TPM limits have no home in the v2
`WorkerRecord` schema yet (see `desired_budget_id`, reserved and unused,
in control/models.py). Phase 2 replaces both of these dicts with a proper
scoped `Budget` object — docs/phase-1-5-implementation-plan.md §Phase 2.

Until then, this is the ONE place both the legacy v1 routes
(dashboard/app.py's /control/{id}, /limits/{team}) and the v2 /desired
endpoint (control/api.py) read and write a worker's effective TPM limit.
Without this bridge, rewiring runner.py onto the v2 API would have quietly
disconnected the dashboard's TPM editor from the worker's pacer — the
editor would still return 200, but nothing downstream would change,
reintroducing a shape of redesign-spec.md finding #10 ("the number shown
is not the number enforced") instead of fixing it.
"""
from typing import Dict

team_limits: Dict[str, int] = {}
worker_limits: Dict[str, int] = {}


def effective_limit_tpm(worker_id: str, team: str, default: int = 2000) -> int:
    """Worker-level override wins over team-level, matching the resolution
    order Phase 2's real `Budget` scoping will use (worker > team > global).
    """
    return worker_limits.get(worker_id, team_limits.get(team, default))
