"""SQLite-backed worker state store.

This is the fix for redesign-spec.md finding #6 ("no persistence") and the
foundation the rest of Phase 1 sits on. One file, WAL mode, one connection
guarded by an asyncio.Lock — comfortable well past the fleet sizes this
harness targets (see docs/phase-1-5-implementation-plan.md's scale
checkpoint note; revisit if fleets grow past ~50 workers on one machine).
"""
import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from maasagentharness.control.budgets import Budget
from maasagentharness.control.models import Phase, WorkerRecord, derive_status

# Seeded once, the first time a fresh store connects, so a new install
# behaves like the pre-Phase-2 default (every agent starts with an
# editable 2000 TPM budget) instead of silently unbudgeted. It's a real,
# ordinary Budget row after that — editable and deletable like any other,
# not special-cased anywhere except this one bootstrap check.
_GLOBAL_DEFAULT_BUDGET_ID = "global-default"
_GLOBAL_DEFAULT_TOKENS_PER_MINUTE = 2000

# Heartbeat cadence and lease TTL. LEASE_TTL must stay >= 3x HEARTBEAT_INTERVAL
# so that a single missed heartbeat (network blip) doesn't immediately read
# as "lost" — see derive_status's staleness check, which uses the same 3x
# multiple as its "unknown" threshold.
HEARTBEAT_INTERVAL = 1.0
LEASE_TTL = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    display_name TEXT NOT NULL,
    task_description TEXT NOT NULL DEFAULT '',
    managed INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,

    desired_state TEXT NOT NULL DEFAULT 'stopped',
    desired_agent TEXT NOT NULL DEFAULT 'triage',
    desired_mode TEXT NOT NULL DEFAULT 'naive',
    desired_budget_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    interrupt_token INTEGER NOT NULL DEFAULT 0,

    phase TEXT NOT NULL DEFAULT 'stopped',
    applied_generation INTEGER NOT NULL DEFAULT 0,
    applied_interrupt_token INTEGER NOT NULL DEFAULT 0,
    current_node TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    heartbeat_at REAL,
    lease_expires_at REAL,

    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workers_team ON workers(team);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    scope_ref TEXT,
    tokens_per_minute INTEGER,
    requests_per_minute INTEGER,
    max_concurrency INTEGER,
    headroom REAL NOT NULL DEFAULT 0.90,
    enforcement TEXT NOT NULL DEFAULT 'client_bucket',
    updated_at REAL NOT NULL
);
"""

_COLUMNS = [
    "worker_id", "team", "display_name", "task_description", "managed", "pid",
    "desired_state", "desired_agent", "desired_mode", "desired_budget_id",
    "generation", "interrupt_token",
    "phase", "applied_generation", "applied_interrupt_token", "current_node",
    "iteration", "last_error", "heartbeat_at", "lease_expires_at",
    "created_at", "updated_at",
]

_BUDGET_COLUMNS = [
    "budget_id", "scope", "scope_ref", "tokens_per_minute", "requests_per_minute",
    "max_concurrency", "headroom", "enforcement", "updated_at",
]


def _row_to_record(row: Any) -> WorkerRecord:
    d: Dict[str, Any] = dict(zip(_COLUMNS, row))
    d["managed"] = bool(d["managed"])
    record = WorkerRecord(**d, status="")
    record.status = derive_status(record)
    return record


def _row_to_budget(row: Any) -> Budget:
    return Budget(**dict(zip(_BUDGET_COLUMNS, row)))


class WorkerStore:
    """Async, single-connection SQLite store. Not safe to share across
    processes (one dashboard process owns the file); safe to share across
    asyncio tasks within that process via the internal lock.
    """

    def __init__(self, db_path: str = "harness_state.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await self._bootstrap_default_budget()

    async def _bootstrap_default_budget(self) -> None:
        """Seed a global 2000-TPM budget the first time this store's
        `budgets` table is empty, so a fresh install behaves like the
        pre-Phase-2 baseline (every agent starts with an editable limit)
        rather than silently unbudgeted. One-time; after this it's an
        ordinary row, editable/deletable through the normal budget API.
        """
        cur = await self._conn.execute("SELECT COUNT(*) FROM budgets")
        (count,) = await cur.fetchone()
        if count == 0:
            await self.put_budget(
                _GLOBAL_DEFAULT_BUDGET_ID,
                scope="global",
                scope_ref=None,
                tokens_per_minute=_GLOBAL_DEFAULT_TOKENS_PER_MINUTE,
                requests_per_minute=None,
                max_concurrency=None,
                headroom=0.90,
                enforcement="client_bucket",
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def wipe(self) -> None:
        """Test-only: drop all rows. Not exposed via any API route."""
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute("DELETE FROM workers")
            await self._conn.commit()

    async def register(
        self,
        worker_id: str,
        team: str,
        display_name: str = "",
        task_description: str = "",
        managed: bool = False,
        agent: str = "triage",
        mode: str = "naive",
    ) -> WorkerRecord:
        """Idempotent upsert. An existing worker's desired state, generation,
        and observed state are NEVER touched by re-registration — only
        display metadata. This is what makes a worker reconnecting after a
        network blip safe: it can't accidentally reset its own in-flight
        command by calling register() again.
        """
        assert self._conn is not None
        now = time.time()
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            )
            exists = await cur.fetchone() is not None

            if not exists:
                await self._conn.execute(
                    """INSERT INTO workers
                       (worker_id, team, display_name, task_description, managed,
                        desired_state, desired_agent, desired_mode, phase,
                        heartbeat_at, lease_expires_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 'stopped', ?, ?, 'stopped', ?, ?, ?, ?)""",
                    (worker_id, team, display_name, task_description, int(managed),
                     agent, mode, now, now + LEASE_TTL, now, now),
                )
            else:
                await self._conn.execute(
                    """UPDATE workers
                       SET team = ?, display_name = ?, task_description = ?, managed = ?,
                           updated_at = ?
                       WHERE worker_id = ?""",
                    (team, display_name, task_description, int(managed), now, worker_id),
                )
            await self._conn.commit()
        record = await self.get(worker_id)
        assert record is not None
        return record

    async def get(self, worker_id: str) -> Optional[WorkerRecord]:
        assert self._conn is not None
        cur = await self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM workers WHERE worker_id = ?", (worker_id,)
        )
        row = await cur.fetchone()
        return _row_to_record(row) if row else None

    async def list(self) -> List[WorkerRecord]:
        assert self._conn is not None
        cur = await self._conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM workers ORDER BY created_at ASC"
        )
        rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def patch_desired(
        self, worker_id: str, patch: Dict[str, Any]
    ) -> Optional[WorkerRecord]:
        """Apply a partial desired-state change, bumping `generation`.
        Returns None if the worker is unknown — the caller (api.py) turns
        that into a 404, never a fabricated record (the #0.2 discipline,
        carried into v2).
        """
        assert self._conn is not None
        if not patch:
            return await self.get(worker_id)

        now = time.time()
        field_map = {"state": "desired_state", "agent": "desired_agent",
                     "mode": "desired_mode", "budget_id": "desired_budget_id"}
        set_clauses = []
        values: List[Any] = []
        for key, value in patch.items():
            if key not in field_map:
                continue
            set_clauses.append(f"{field_map[key]} = ?")
            values.append(value)
        if not set_clauses:
            return await self.get(worker_id)

        async with self._lock:
            cur = await self._conn.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            )
            if await cur.fetchone() is None:
                return None
            set_clauses.append("generation = generation + 1")
            set_clauses.append("updated_at = ?")
            values.append(now)
            values.append(worker_id)
            await self._conn.execute(
                f"UPDATE workers SET {', '.join(set_clauses)} WHERE worker_id = ?",
                values,
            )
            await self._conn.commit()
        return await self.get(worker_id)

    async def request_interrupt(self, worker_id: str) -> Optional[WorkerRecord]:
        """Bump `interrupt_token` — the worker notices on its next desired-
        state poll (bounded by HEARTBEAT_INTERVAL) and cancels its in-flight
        call. See models.py's HeartbeatRequest docstring for why this is
        poll-based rather than an in-process task handle.
        """
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            )
            if await cur.fetchone() is None:
                return None
            await self._conn.execute(
                "UPDATE workers SET interrupt_token = interrupt_token + 1, updated_at = ? WHERE worker_id = ?",
                (time.time(), worker_id),
            )
            await self._conn.commit()
        return await self.get(worker_id)

    async def heartbeat(
        self,
        worker_id: str,
        applied_generation: int,
        phase: str,
        current_node: Optional[str],
        iteration: int,
        last_error: Optional[str],
        applied_interrupt_token: int = 0,
        team: str = "unknown",
        display_name: str = "",
    ) -> WorkerRecord:
        """Self-healing upsert: if `worker_id` doesn't exist — e.g. the
        dashboard restarted and lost its SQLite file, or this is the very
        first heartbeat racing a slow /register — re-create the row from
        the heartbeat payload itself rather than erroring. This is what
        keeps a running fleet from being orphaned by a control-plane
        restart (redesign-spec.md finding #6's other half).
        """
        assert self._conn is not None
        now = time.time()
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT worker_id FROM workers WHERE worker_id = ?", (worker_id,)
            )
            exists = await cur.fetchone() is not None
            if not exists:
                await self._conn.execute(
                    """INSERT INTO workers
                       (worker_id, team, display_name, task_description, managed,
                        desired_state, phase, applied_generation, applied_interrupt_token,
                        current_node, iteration, last_error,
                        heartbeat_at, lease_expires_at, created_at, updated_at)
                       VALUES (?, ?, ?, '', 0, 'stopped', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (worker_id, team, display_name, phase, applied_generation,
                     applied_interrupt_token, current_node, iteration, last_error,
                     now, now + LEASE_TTL, now, now),
                )
            else:
                await self._conn.execute(
                    """UPDATE workers
                       SET phase = ?, applied_generation = ?, applied_interrupt_token = ?,
                           current_node = ?, iteration = ?, last_error = ?,
                           heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                       WHERE worker_id = ?""",
                    (phase, applied_generation, applied_interrupt_token, current_node,
                     iteration, last_error, now, now + LEASE_TTL, now, worker_id),
                )
            await self._conn.commit()
        record = await self.get(worker_id)
        assert record is not None
        return record

    async def set_pid(self, worker_id: str, pid: Optional[int]) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                "UPDATE workers SET pid = ?, updated_at = ? WHERE worker_id = ?",
                (pid, time.time(), worker_id),
            )
            await self._conn.commit()

    async def mark_lost(self, worker_id: str, reason: str) -> None:
        """Used by the supervisor's reaper when a managed process dies
        without having been told to stop."""
        assert self._conn is not None
        async with self._lock:
            await self._conn.execute(
                "UPDATE workers SET phase = ?, last_error = ?, lease_expires_at = 0, updated_at = ? WHERE worker_id = ?",
                (Phase.LOST.value, reason, time.time(), worker_id),
            )
            await self._conn.commit()

    async def delete(self, worker_id: str) -> bool:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute(
                "DELETE FROM workers WHERE worker_id = ?", (worker_id,)
            )
            await self._conn.commit()
            return cur.rowcount > 0

    # -- Phase 2: budgets --------------------------------------------------

    async def put_budget(
        self,
        budget_id: str,
        scope: str,
        scope_ref: Optional[str],
        tokens_per_minute: Optional[int],
        requests_per_minute: Optional[int],
        max_concurrency: Optional[int],
        headroom: float,
        enforcement: str,
    ) -> Budget:
        """Full replace (upsert) — PUT semantics, matching
        docs/phase-1-5-implementation-plan.md §2.3's "this is a form
        submission, not a patch" framing.
        """
        assert self._conn is not None
        now = time.time()
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO budgets
                   (budget_id, scope, scope_ref, tokens_per_minute, requests_per_minute,
                    max_concurrency, headroom, enforcement, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(budget_id) DO UPDATE SET
                     scope = excluded.scope,
                     scope_ref = excluded.scope_ref,
                     tokens_per_minute = excluded.tokens_per_minute,
                     requests_per_minute = excluded.requests_per_minute,
                     max_concurrency = excluded.max_concurrency,
                     headroom = excluded.headroom,
                     enforcement = excluded.enforcement,
                     updated_at = excluded.updated_at""",
                (budget_id, scope, scope_ref, tokens_per_minute, requests_per_minute,
                 max_concurrency, headroom, enforcement, now),
            )
            await self._conn.commit()
        budget = await self.get_budget(budget_id)
        assert budget is not None
        return budget

    async def get_budget(self, budget_id: str) -> Optional[Budget]:
        assert self._conn is not None
        cur = await self._conn.execute(
            f"SELECT {', '.join(_BUDGET_COLUMNS)} FROM budgets WHERE budget_id = ?", (budget_id,)
        )
        row = await cur.fetchone()
        return _row_to_budget(row) if row else None

    async def list_budgets(self) -> List[Budget]:
        assert self._conn is not None
        cur = await self._conn.execute(f"SELECT {', '.join(_BUDGET_COLUMNS)} FROM budgets")
        rows = await cur.fetchall()
        return [_row_to_budget(r) for r in rows]

    async def delete_budget(self, budget_id: str) -> bool:
        assert self._conn is not None
        async with self._lock:
            cur = await self._conn.execute("DELETE FROM budgets WHERE budget_id = ?", (budget_id,))
            await self._conn.commit()
            return cur.rowcount > 0

    async def count_workers_assigned_to_budget(self, budget_id: str) -> int:
        """Used to refuse deleting a budget something still points at —
        see DELETE /api/budgets/{id}'s 409 case."""
        assert self._conn is not None
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM workers WHERE desired_budget_id = ?", (budget_id,)
        )
        (count,) = await cur.fetchone()
        return count
