# Phase 1–5 — Implementation Plan

**Status:** Phases 1–4 ✅ **implemented** — see each phase's "Implementation notes" subsection for
what shipped and where it deviated from this plan. Phase 5 still draft.
**Parent:** [redesign-spec.md](redesign-spec.md) §4, §6
**Purpose:** this is the actionable, file-by-file companion to the architecture in
`redesign-spec.md` §4. That document explains *why*; this one specifies *what to build*: concrete
schemas, API contracts, module layout, and a task breakdown per phase, so each phase can be picked
up and implemented without re-deriving the design.

Each phase is independently shippable and independently useful — you don't need Phase 3 to benefit
from Phase 1. Land them in order; each one's data model is a dependency of the next.

---

## Phase 1 — Control plane v2

**Delivers:** real start/stop, real liveness, survives a dashboard restart. Kills redesign-spec.md
§2.1 entirely (findings #1, #2, #3, #4, #5, #6).

### 1.1 New module: `maasagentharness/control/`

```
maasagentharness/control/
├── __init__.py
├── store.py          # SQLite-backed state store
├── models.py          # WorkerRecord, Phase, DesiredPatch — typed, extra="forbid"
├── supervisor.py       # spawns/signals managed worker processes
└── api.py              # FastAPI router: mounted at /api/workers in dashboard/app.py
```

### 1.2 Schema (SQLite — one file, `harness_state.db`, WAL mode)

```sql
CREATE TABLE workers (
    worker_id TEXT PRIMARY KEY,
    team TEXT NOT NULL,
    display_name TEXT NOT NULL,
    task_description TEXT NOT NULL DEFAULT '',
    managed INTEGER NOT NULL DEFAULT 0,   -- 1 if this daemon spawned it
    pid INTEGER,

    -- desired (control-plane-owned)
    desired_state TEXT NOT NULL DEFAULT 'stopped',   -- running | stopped
    desired_agent TEXT NOT NULL DEFAULT 'triage',
    desired_mode TEXT NOT NULL DEFAULT 'naive',
    desired_budget_id TEXT,
    generation INTEGER NOT NULL DEFAULT 0,

    -- observed (worker-owned, written only via heartbeat)
    phase TEXT NOT NULL DEFAULT 'stopped',   -- starting|running|draining|stopped|unknown|lost|errored
    applied_generation INTEGER NOT NULL DEFAULT 0,
    current_node TEXT,
    iteration INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    heartbeat_at REAL,                 -- unix epoch seconds, server-stamped
    lease_expires_at REAL,

    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_workers_team ON workers(team);
```

`generation` and `applied_generation` are the whole mechanism: every desired-state write bumps
`generation`; the worker echoes `applied_generation` on its next heartbeat. `applied_generation <
generation` is exactly "pending" — no separate status field needed for that case.

### 1.3 API contract (`maasagentharness/control/api.py`)

| Method | Path | Body | Response | Notes |
|---|---|---|---|---|
| `POST` | `/api/workers` | `{worker_id, team, display_name, task_description, managed}` | `WorkerRecord` | Idempotent upsert — replaces `/register`. Re-registering an existing id is a no-op on desired state (don't reset a running worker's desired state just because it reconnected). |
| `GET` | `/api/workers/{id}/desired` | — | `{state, agent, mode, budget_id, generation}` or `{state:"stopped", unregistered:true}` | Replaces `GET /control/{id}`. Polled by workers every 1s. |
| `PATCH` | `/api/workers/{id}/desired` | `DesiredPatch` (`extra="forbid"`, all optional) | updated record, new `generation` | 404 on unknown id. This is the UI's Start/Stop/mode button. |
| `POST` | `/api/workers/{id}/heartbeat` | `{applied_generation, phase, current_node, iteration, last_error}` | `{lease_expires_at}` | Called every 1s by the worker. Upserts `heartbeat_at`, extends the lease, and — this is the self-healing part — re-creates the worker record from scratch if it was somehow missing, so a dashboard restart doesn't orphan a running fleet. |
| `POST` | `/api/workers/{id}/interrupt` | — | 202 | Cancels the worker's current in-flight call immediately (see 1.5). |
| `POST` | `/api/workers/{id}/kill` | — | 202 or 409 | Managed workers only — `SIGTERM` then `SIGKILL` after `kill_timeout` (default 10s). 409 if `managed=false`. |
| `GET` | `/api/workers` | — | `[WorkerRecord, ...]` | Fleet listing for the UI; derives `status` (`pending`/`lost`/`unknown`/phase) server-side so the UI never recomputes staleness from raw timestamps itself. |
| `DELETE` | `/api/workers/{id}` | — | 204 | "Forget" — only when `phase in (stopped, lost)`. |

**Status derivation** (done once, server-side, in `api.py`, not duplicated in the frontend):

```python
def derive_status(w: WorkerRecord, now: float) -> str:
    if now > w.lease_expires_at:
        return "lost"
    if w.applied_generation < w.generation:
        return "pending"
    if now - w.heartbeat_at > HEARTBEAT_INTERVAL * 3:
        return "unknown"
    return w.phase
```

### 1.4 Worker side: `maasagentharness/runner.py` rewrite

Replace `poll_control`/`register` with a `ControlClient`:

```python
class ControlClient:
    async def register(self) -> WorkerRecord: ...
    async def poll_desired(self) -> DesiredState: ...          # GET .../desired, every 1s
    async def heartbeat(self, phase, current_node, iteration, applied_generation, last_error) -> None: ...
```

`AgentWorker` keeps the Phase 0 `stop_event` + background poll task design (that part of Phase 0
is not thrown away — it's the mechanism Phase 1 builds on), but:
- `_sync_stop_event` now compares against `desired.generation` vs the worker's own
  `applied_generation`, and the worker bumps its local `applied_generation` only after it has
  actually observed and acted on a new desired state — closing the gap where a command could be
  "received" but not yet "applied".
- Phase (`starting`/`running`/`draining`/`stopped`) is tracked locally and sent on every heartbeat.

### 1.5 Three-phase stop

| Action | New behavior |
|---|---|
| **Stop** | `PATCH .../desired {state: "stopped"}`. Worker sees it, enters `draining`, lets the current harness call/cycle finish (bounded by `graceful_stop`, default 30s — see §4.2 of the spec), then reports `phase: "stopped"`. |
| **Interrupt** | `POST .../interrupt`. The daemon holds one `asyncio.Task` handle per managed worker's current call (for attached workers, this is a no-op the UI greys out — you can't cancel a process you don't control at the task level). Calling `.cancel()` on it aborts the in-flight `httpx` request immediately. The resulting partial result is tagged `interrupted: true` in telemetry and excluded from latency aggregates. |
| **Kill** | `POST .../kill`. Managed only. `SIGTERM`, wait `kill_timeout`, `SIGKILL`. |

### 1.6 Supervisor (`maasagentharness/control/supervisor.py`)

For `managed: true` workers only:

```python
class Supervisor:
    async def spawn(self, worker_id: str, env: dict) -> int: ...   # returns pid
    async def signal(self, worker_id: str, sig: signal.Signals) -> None: ...
    async def reap(self) -> None: ...   # periodic: clean up dead PIDs, mark phase=lost
```

Spawns via `asyncio.create_subprocess_exec(sys.executable, "-m", "maasagentharness.runner", env=…)`.
The dashboard process becomes the parent of every managed worker — this is what makes **Start** and
**Kill** real operations instead of hopeful dict writes.

### 1.7 Migration / compatibility

- Old `/register`, `/control/{id}`, `/events` routes stay mounted (thin shims calling the new store)
  for one release, logging a deprecation warning, so a half-upgraded fleet doesn't silently break.
- Phase 0's `AgentControlState` (`runner.py`) becomes `DesiredState` in `control/models.py` — same
  shape, same field names, so the diff is additive.

### 1.8 Task breakdown

1. `control/models.py` — `WorkerRecord`, `DesiredPatch`, `Phase` enum.
2. `control/store.py` — SQLite schema + CRUD, `upsert_heartbeat` (the self-healing re-create path).
3. `control/api.py` — the 8 routes above, mounted in `dashboard/app.py`.
4. `runner.py` — `ControlClient`, phase tracking, heartbeat loop.
5. `control/supervisor.py` + a `POST /api/workers/{id}/spawn` route for `managed=true`.
6. Compatibility shims for the 3 old routes.
7. Tests: generation convergence, lease expiry → `lost`, self-healing re-register on heartbeat,
   interrupt cancels within ~100ms (fake slow endpoint), kill actually terminates a spawned PID.

### 1.9 Implementation notes (what actually shipped, and where it deviated)

**Files added:** `control/__init__.py`, `control/models.py`, `control/store.py`,
`control/client.py` (worker-side HTTP client — not in the original §1.8 list, split out of
`runner.py` so the transport is independently testable), `control/supervisor.py`, `control/api.py`,
`control/limits.py` (new — see below). **Files rewritten:** `runner.py`, `dashboard/app.py`.
**Tests added:** `tests/test_control_plane.py` (store, supervisor, API routes, and a full
`AgentWorker` run driven against the real FastAPI app over `httpx.ASGITransport` — no real sockets,
no port to bind). **Verified:** `uv run pytest tests/ -q` → 39 passed (up from Phase 0's 18),
including a real subprocess spawn+`SIGTERM`+kill and an end-to-end Start→running→Stop→stopped
convergence test against the live app.

**Deviation 1 — "Interrupt" is poll-based, not an in-process task handle.** The original §1.5 table
said Interrupt works by holding "one `asyncio.Task` handle per managed worker's current call" and
calling `.cancel()` on it. That's only possible if the worker runs *in the same process* as the
API server. It doesn't, even for `managed=true` workers — the supervisor spawns them as a *separate
process*, so the dashboard has a PID, not a Task. What actually shipped: `WorkerRecord.interrupt_token`
(an incrementing counter, bumped by `POST /interrupt`) is returned on every `GET /desired` poll;
the worker compares it to the last token it saw and, on a change, sets a local `interrupt_event`
that `ModelHarness` checks at the same points as `stop_event` (see `WorkerInterrupted`'s docstring
in `harness.py`) — except retry/backoff sleeps, which only `stop_event` interrupts. This bounds
Interrupt to the ~1s poll interval rather than making it instant, and it's explicitly single-shot
(consuming the token clears the local event, so it can't abort a call that starts after the
interrupt was requested but before the next poll — except for one known edge case: if no call was
in flight *at all* when the interrupt was requested, the event stays set and aborts whichever call
starts next, instead of being discarded as "nothing to interrupt." Documented as acceptable for
Phase 1 in `WorkerInterrupted`'s docstring; revisit if it proves surprising in practice.

**Deviation 2 — a TPM-limit bridge that wasn't in the original schema.** `WorkerRecord` deliberately
has no `limit_tpm` field (that's Phase 2's `Budget`). But the *currently shipped* React dashboard's
TPM editor still needs to reach a worker's pacer somehow, and rewiring `runner.py` onto the v2
`/desired` endpoint — which doesn't carry `limit_tpm` — would have silently disconnected that editor
from the pacer it used to update (the editor would keep returning 200; nothing downstream would
change). Fixed with `control/limits.py`: two plain dicts (`team_limits`, `worker_limits`) shared
between the legacy v1 routes in `dashboard/app.py` and the v2 `/desired` route in `control/api.py`
(which now returns an extra, explicitly-transitional `limit_tpm` field), plus a resolution helper
`effective_limit_tpm` using the same worker > team precedence Phase 2's real `Budget` scoping will
use. `runner.py` applies it to `self.harness.pacer.update_target(...)` on every poll tick. This is
a deliberate stopgap, not scope creep — the alternative was shipping Phase 1 with a working control
plane that quietly broke rate-limit testing, which is the other half of what this whole redesign is
for.

**Deviation 3 — the legacy v1 routes aren't just "shims kept for old agents," they're load-bearing.**
§1.7 described `/register`/`/control/{id}` as compatibility shims for agents still running old code.
What shipped goes further: `/register`, `/control/{id}`, `/act/{N}`, `/reset`, and `/limits/{team}`
in `dashboard/app.py` now all delegate to the *same* `WorkerStore` the new `/api/workers` routes use
— there is exactly one source of truth for fleet state. This matters because the *shipped* React
dashboard (not rebuilt until Phase 5) still calls these routes exclusively; without this, Phase 1
would have fixed persistence and liveness only for a `/api/workers` API nothing uses yet, while the
dashboard you actually open in a browser kept reading from a dict that resets on restart. Now it
doesn't — findings #1, #5, and #6 are fixed for the UI you have today, not just for the one Phase 5
builds. The actual `/api` URL-prefix migration for these routes (the other half of finding #19) is
still deferred to Phase 5, as planned.

**Not yet done from §1.6:** the `spawn` route exists and is tested (real subprocess, real
`SIGTERM`/`SIGKILL`), but nothing yet calls it automatically — there's no "managed worker" creation
flow in the UI (that's Phase 5's `ScenarioLauncher`/`FleetView`). `managed=true` workers today can
only be spawned by calling `POST /api/workers/{id}/spawn` directly.

---

## Phase 2 — Budgets

**Delivers:** one source of truth for rate limits, scoped, with a real UI editor. Kills
redesign-spec.md §2.2 (findings #7, #8, #9, #10, #11).

### 2.1 Schema addition

```sql
CREATE TABLE budgets (
    budget_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,              -- global | team | worker
    scope_ref TEXT,                   -- team name or worker_id; NULL for global
    tokens_per_minute INTEGER,
    requests_per_minute INTEGER,
    max_concurrency INTEGER,
    headroom REAL NOT NULL DEFAULT 0.90,
    enforcement TEXT NOT NULL DEFAULT 'client_bucket',  -- off | client_bucket | shadow
    updated_at REAL NOT NULL
);
```

`workers.desired_budget_id` (added in Phase 1's table, nullable) overrides team/global resolution
for that one worker.

### 2.2 Resolution (`control/budgets.py`)

```python
def resolve_budget(worker: WorkerRecord, budgets: list[Budget]) -> Budget:
    """worker-scoped > team-scoped > global. Returns a default (enforcement=off)
    if nothing matches, so a worker is never silently unbudgeted and unexplained."""
```

Effective enforced rate = `tokens_per_minute * headroom` — computed once here, returned alongside
the nominal rate in every API response that mentions a budget, so the UI never has to recompute it
and can never show a different number than what's enforced (redesign-spec.md finding #10).

### 2.3 API

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/budgets` | All budgets, each with `effective_tokens_per_minute` precomputed. |
| `PUT` | `/api/budgets/{id}` | Full replace — this is a form submission, not a live-sync field. |
| `DELETE` | `/api/budgets/{id}` | Only if unreferenced by any worker's `desired_budget_id`. |
| `GET` | `/api/workers/{id}/budget` | The *resolved* budget for this worker + which scope won. |

`enforcement: "shadow"` — the worker's `TokenBucket` still runs the acquire logic and records how
long it *would* have waited (`shadow_queue_wait_ms`) in its telemetry, but never actually awaits
that sleep. Lets you see "this agent is 3× over budget" without altering the load being measured.

### 2.4 Frontend — form-submit editor, not live-sync

Replace `LimitEditor` (`App.tsx`) with a `BudgetEditor` that:
- Loads the resolved budget once via `GET /api/workers/{id}/budget` on mount.
- Holds a local draft (`useState`), **never overwritten by the SSE/WS stream** while mounted.
- Shows a dirty indicator and explicit **Apply**/**Revert** buttons (this is Locust's `swarm`-slice
  pattern — config is submitted, not synced).
- On Apply: `PUT /api/budgets/{id}`, then re-fetch and reconcile.
- Displays nominal vs. effective rate side by side, and which scope (`global`/`team`/`worker`) is
  binding.

### 2.5 Event annotation

Every budget change is appended to the events table (Phase 4) as a synthetic `node="budget_change"`
row, so a chart can mark *"limit changed 2000 → 4000 here"*.

### 2.6 Task breakdown

1. `control/budgets.py` — schema, `resolve_budget`, effective-rate computation.
2. API routes above.
3. `pacer.py` — add `shadow` mode: compute `would_have_waited_ms`, don't actually sleep.
4. `BudgetEditor.tsx` replacing `LimitEditor`.
5. Tests: scope precedence (worker > team > global), shadow mode never delays, effective rate
   matches `tokens_per_minute * headroom` exactly.

### 2.7 Implementation notes (what actually shipped, and where it deviated)

**Files added:** `control/budgets.py` (`Budget`, `BudgetUpdate`, `BudgetView`, `ResolvedBudgetView`,
`resolve_budget`, `DEFAULT_BUDGET`). **Files extended:** `control/store.py` (budgets table + CRUD +
a one-time bootstrap), `control/api.py` (`budgets_router`, `GET /api/workers/{id}/budget`,
`GET /api/workers/{id}/desired` now resolves a real Budget instead of a dict), `control/client.py`
(`get_resolved_budget`), `pacer.py` (`shadow=True` mode), `harness.py` (`budget` param,
`_pacing_action`, `update_budget`), `runner.py` (fetches the resolved budget every poll tick,
alongside desired state), `dashboard/app.py` (legacy `/control`, `/limits` now write real Budget
rows). **File deleted:** `control/limits.py` — the Phase 1 dict bridge, fully superseded.
**Tests added:** `tests/test_budgets.py` (22 tests: pure `resolve_budget` precedence, pacer shadow
semantics, `ModelHarness._pacing_action` across every mode×enforcement combination, all
`/api/budgets` + `/api/workers/{id}/budget` routes, the legacy-route integration, and a full
`AgentWorker` end-to-end test proving a live enforcement change actually reaches the running
harness — not just that the unit pieces are individually correct). **Verified:**
`uv run pytest tests/ -q` → 61 passed.

**Deviation 1 — mode and enforcement are two independent axes, not one.** §2's schema is unchanged,
but nothing in the original plan said how `Budget.enforcement` interacts with the pre-existing
`naive`/`paced`/`runaway` agent mode. The naive answer — "paced mode always obeys whatever budget is
configured" — would have broken the `runaway` scenario's entire purpose (it exists specifically to
ignore limits). What shipped: `client_bucket` enforcement only actually blocks when `mode=="paced"`;
`naive`/`runaway` never block on it, preserving their defining behavior. `shadow` enforcement runs
*regardless of mode* — which turns out to be the more interesting case: measuring exactly how far a
`naive` or `runaway` agent overshoots a budget it's deliberately not obeying, without altering its
behavior. See `ModelHarness._pacing_action`'s docstring.

**Deviation 2 — `TokenBucket.acquire()` now returns a float.** Not specified in §2.3/§2.6, but
needed: shadow mode has to report "how long would this have waited," and the only place that
computation naturally happens is inside the same refill/deduct logic a real acquire already runs.
Returns `0.0` on the normal blocking path (unchanged behavior, now with an explicit no-op return
value instead of implicit `None`). Shadow mode lets `tokens` go negative — a negative balance *is*
the "how far over budget" signal, not a bug to clamp away.

**Deviation 3 — the legacy `/control` and `/limits` routes don't just read the new system, they
changed its behavior for the better.** The pre-Phase-2 bridge's `/limits/{team}` bulk-overwrote
*every* worker's individual limit, including ones already edited one-by-one — it was a "team limit"
in name only. Real scoped resolution means a worker-level override now survives a later team-level
edit, which is what "team limit" should have meant all along. `tests/test_budgets.py`'s
`test_worker_level_override_beats_team_update` is the regression test for exactly this. Also: a
freshly registered worker no longer gets an automatic per-worker budget snapshot of its own
`LIMIT_TPM` env var (the old design's actual defect — that snapshot is what made team/global
resolution meaningless, since every worker already had its own entry that nothing could fall back
past). It now resolves against team/global like any unconfigured worker, until something explicit
overrides it.

**Known limitation carried forward:** the legacy `/control/{id}` response still shows a plain
`limit_tpm` integer with no way to display "unenforced" (enforcement="off") — it falls back to
`2000` as a placeholder in that one case, since the pre-Phase-5 UI has no field for it.
`GET /api/workers/{id}/budget` and `GET /api/budgets` show the honest resolved state, including
`enforcement` and `resolved_via`, for anything that reads them instead.

**Not yet done:** `BudgetEditor.tsx` (§2.4) — the shipped React dashboard still uses the old
`LimitEditor`, which calls `/control/{id}` with `limit_tpm` exactly as before and therefore still
works (now against real Budget rows under the hood), but doesn't yet expose enforcement mode,
scope, or the worker-vs-team-vs-global distinction. That's explicitly bundled into Phase 5's UI
rebuild rather than done piecemeal against a frontend about to be replaced.

---

## Phase 3 — Scenarios and executors

**Delivers:** the actual missing capability — calibrated, declarative load generation. This is the
highest-value phase; everything before it is fixing what's broken, this is adding what's missing.

### 3.1 New module: `maasagentharness/scenario/`

```
maasagentharness/scenario/
├── __init__.py
├── schema.py        # pydantic models for the YAML — Scenario, Executor, Threshold
├── loader.py        # YAML -> validated Scenario, with ${ENV_VAR} interpolation
├── executors/
│   ├── base.py       # Executor protocol: async def run(iteration_fn, stop_event) -> ExecutorStats
│   ├── vus.py         # constant_vus, ramping_vus (closed model)
│   └── arrival.py     # constant_arrival_rate, ramping_arrival_rate, poisson_arrival_rate (open model)
└── sweep.py          # derived executor: synchronous floor -> throughput ceiling -> fill rates
```

### 3.2 Scenario schema (`scenario/schema.py`)

```python
class Stage(BaseModel):
    target: int
    duration: str   # "2m", "30s" — parsed with a duration validator

class ExecutorConfig(BaseModel):
    kind: Literal["constant_vus", "ramping_vus", "constant_arrival_rate",
                   "ramping_arrival_rate", "poisson_arrival_rate", "sweep"]
    rate: Optional[float] = None          # iterations/min, for *_arrival_rate
    vus: Optional[int] = None             # for constant_vus
    stages: Optional[List[Stage]] = None  # for ramping_*
    max_vus: Optional[int] = None         # cap on open-model concurrency
    duration: Optional[str] = None

class ScenarioConfig(BaseModel):
    agent: Literal["triage", "research", "assistant"]
    executor: ExecutorConfig
    budget: Optional[str] = None          # budget_id, or inline Budget
    graceful_stop: str = "30s"
    start_time: str = "0s"

class RunConfig(BaseModel):
    run: str
    endpoint: Dict[str, str]
    budgets: List[BudgetConfig] = []
    scenarios: Dict[str, ScenarioConfig]
    thresholds: List[str] = []            # e.g. "p95(total_latency_ms) < 8000"
```

### 3.3 Executor contract (`executors/base.py`)

```python
class ExecutorStats(BaseModel):
    iterations_started: int
    iterations_completed: int
    iterations_dropped: int   # open-model only — couldn't start on schedule. THE key metric.

class Executor(Protocol):
    async def run(
        self,
        iteration_fn: Callable[[], Awaitable[None]],
        stop_event: asyncio.Event,
        duration_s: float,
    ) -> ExecutorStats: ...
```

- **`constant_vus`** — N tasks, each loops `await iteration_fn()` back-to-back. This *is* the
  current `runner.py` behavior with N=1; generalizing it to N is the whole fix.
- **`ramping_vus`** — adjusts N on a schedule by spawning/cancelling tasks at each stage boundary.
- **`constant_arrival_rate`** — a scheduler loop fires `asyncio.create_task(iteration_fn())` on a
  fixed-interval clock (`rate` iterations/min) regardless of how long previous iterations take,
  capped at `max_vus` concurrent in-flight; exceeding the cap counts as a **dropped iteration**.
  This is the open-model executor — the one that exposes saturation instead of hiding it.
- **`ramping_arrival_rate`** — same scheduler, rate interpolated across `stages`.
- **`poisson_arrival_rate`** — same scheduler, inter-arrival times drawn from `Exponential(1/rate)`
  instead of fixed spacing (GuideLLM-style bursty arrivals).
- **`sweep`** (`sweep.py`) — not a primitive executor; it runs `constant_arrival_rate` three times
  in sequence: (1) `rate=1` as a synchronous latency floor, (2) a short burst at a very high rate to
  find the throughput ceiling (first rate where `iterations_dropped > 0` or a threshold breaches),
  (3) 3–5 intermediate rates between floor and ceiling. Replaces `probe.py` with a principled
  version of the same idea.

### 3.4 Wiring into the worker

`AgentWorker.run_loop` is replaced by `AgentWorker.run_scenario(scenario: ScenarioConfig)`:
`iteration_fn` becomes a closure running exactly one triage/research/assistant cycle (reusing the
Phase 0 `WorkerStopped`-aware harness unchanged — the executor layer sits *above* the harness, not
inside it). The executor's `stop_event` is the same one Phase 0/1 already thread through.

### 3.5 Runner CLI

```bash
uv run harness run scenario.yaml                 # runs it
uv run harness run scenario.yaml --dry-run        # validates + prints the resolved plan, no calls
```

Retires `/act/1` and `/act/2` (`dashboard/app.py`) — replaced by the UI's "launch scenario" action
hitting `POST /api/runs {scenario_path, overrides}`, which spawns the worker set the scenario names
via the Phase 1 supervisor.

### 3.6 Task breakdown

1. `scenario/schema.py` + `loader.py` (incl. `${ENV_VAR}` interpolation, duration parsing).
2. `executors/vus.py` (constant + ramping — closed model, simplest).
3. `executors/arrival.py` (constant + ramping + poisson — open model, with drop accounting).
4. `sweep.py` on top of `arrival.py`.
5. `AgentWorker.run_scenario`, retiring the hardcoded per-mode `sleep()` cadence.
6. `POST /api/runs` + CLI entry point; retire `/act/{1,2}`.
7. Tests: dropped-iteration counting under an artificially slow fake endpoint; ramping_vus hits
   each stage's target concurrency; sweep finds a ceiling against a fake endpoint with a known
   breaking point; threshold parser.

### 3.7 Implementation notes (what actually shipped, and where it deviated)

**Files added:** `scenario/schema.py`, `scenario/loader.py`, `scenario/executors/{base,vus,arrival,factory}.py`,
`scenario/sweep.py`, `scenario/cli.py`. **Files extended:** `runner.py` (`run_scenario`, plus a
`_run_one_cycle` extraction shared with `run_loop`), `control/supervisor.py` (`spawn()` generalized
to take `module`/`args`), `control/api.py` (`runs_router`, `POST /api/runs`, `GET /api/budgets/{id}`),
`control/client.py` (`get_budget_by_id`, `patch_desired`). **Tests added:** `tests/test_scenario.py`
(28 tests — duration/threshold parsing, loader + `${ENV_VAR}` interpolation, every executor against
fake iteration functions, sweep against a fake endpoint with a simulated breaking point) and
`tests/test_scenario_integration.py` (7 tests — a real `AgentWorker.run_scenario()` against the real
app, including a live Stop command cutting a 10s scenario short, and `POST /api/runs` spawning real
subprocesses). **Verified:** `uv run pytest tests/ -q` → 96 passed (up from Phase 2's 61), plus a
manual CLI smoke test (`uv run harness run demo.yaml --dry-run`) against a real multi-scenario YAML
file with `${ENV_VAR}` interpolation, inline budgets, and both an arrival-rate and a ramping-vus
scenario.

**Deviation 1 — `run_scenario` is additive, not a replacement.** §3.4 said "`AgentWorker.run_loop`
is replaced by `run_scenario`." Taken literally, that would have deleted the dashboard-driven
Start/Stop flow Phases 0-2 spent three phases making reliable. What shipped: `run_loop` is
untouched (verified — the Phase 1 end-to-end test still exercises it unmodified and still passes),
and `run_scenario` is a new, separate entry point for declarative load generation. The two share
`_run_one_cycle` (a new extraction — the triage/research/assistant dispatch body, previously inlined
in `run_loop`, pulled into one method both call into) so there's no duplicated agent-dispatch logic,
but they're genuinely two different drivers for two different use cases: dashboard-clicked
Start/Stop vs. `uv run harness run scenario.yaml`.

**Deviation 2 — `mode` was missing from `ScenarioConfig` in the plan.** §3.2's schema had
`agent`/`executor`/`budget`/`graceful_stop`/`start_time` but no way to pick naive/paced/runaway —
an orthogonal axis (retry/backoff strategy, whether `client_bucket` enforcement actually blocks)
that a load-generation scenario clearly still needs to control. Added, defaulting to `"naive"`.

**Deviation 3 — `Executor.run()` takes no `duration_s` parameter.** §3.3's protocol signature
included one; it doesn't fit a `Stage`-based ramp (`ramping_vus`, `ramping_arrival_rate`) uniformly
with a flat-duration executor. Each concrete executor carries its own schedule from construction
instead — `build_executor(cfg: ExecutorConfig)` is the factory that resolves an `ExecutorConfig`
into a fully-configured instance of the right shape.

**Deviation 4 — `/act/{1,2}` were NOT retired.** §3.5 said to retire them once `POST /api/runs`
existed. They still work, unchanged, for the same reason Phase 1 kept `run_loop` and Phase 2 kept
`LimitEditor`: the shipped dashboard has no "launch scenario" button yet (that's Phase 5's
`ScenarioLauncher`), so deleting the only interactive scenario-trigger the current UI has, before
its replacement exists, would be a pure regression. `POST /api/runs` is real and tested, just not
yet wired to anything the shipped frontend calls.

**Deviation 5 — threshold evaluation is necessarily partial.** `ThresholdSpec.parse()` and `.check()`
are fully implemented and tested, but real evaluation of `"p95(total_latency_ms) < 8000"`-style
expressions against actual percentiles needs Phase 4's histogram store, which doesn't exist yet.
`sweep.py`'s ceiling-finding uses the one breach signal available today without it:
`iterations_dropped > 0`. Scenario files can declare `thresholds` now and have them parse and
validate; full enforcement arrives with Phase 4.

**Addition beyond the plan — `AgentWorker.run_scenario` sends an explicit final heartbeat.** Found
during manual testing: a scenario shorter than the 1s heartbeat interval could finish, get cancelled,
and leave the store showing a stale `phase` (e.g. still `"starting"`) because the periodic heartbeat
loop never got a second tick before being cancelled. Fixed by sending one heartbeat synchronously in
`run_scenario`'s `finally` block before cancelling the poll task. Covered by
`test_run_scenario_completes_and_reports_stats`.

---

## Phase 4 — Metrics v2

**Delivers:** the LLM-relevant metrics (TTFT, ITL, percentiles) and run artifacts that make two runs
comparable. Also fixes redesign-spec.md finding #15 (naive local timestamps) and #24 (per-request
`httpx.AsyncClient`).

### 4.1 New module: `maasagentharness/metrics/`

```
maasagentharness/metrics/
├── __init__.py
├── record.py        # RequestRecord — the atom, written once, never mutated
├── histogram.py      # streaming percentile estimator (t-digest)
├── store.py          # in-memory ring buffer (live) + Parquet writer (artifact)
├── prometheus.py      # GET /metrics renderer
└── artifact.py        # runs/<run_id>/ writer: scenario.yaml, summary.json, requests.parquet
```

### 4.2 `RequestRecord` (replaces the ad-hoc fields on `TelemetryEvent`)

```python
class RequestRecord(BaseModel):
    run_id: str
    scenario: str
    worker_id: str
    iteration: int
    node: str
    attempt: int
    interrupted: bool = False

    ts_ingest_utc_ms: int    # server-stamped — authoritative for every bucket/chart
    ts_agent_utc_ms: int     # agent-stamped — kept ONLY to compute clock skew

    status: int
    prompt_tokens: int
    completion_tokens: int
    ttft_ms: Optional[int]        # time to first streamed token/byte
    itl_ms_mean: Optional[float]  # mean inter-token latency
    total_ms: int
    queue_wait_ms: int             # time spent in the pacer (or shadow equivalent)
    ratelimit_headers: Dict[str, str]
```

`TelemetryEvent` (Phase 0's version, with `seq`) becomes a thin wire-format alias that the
dashboard converts to `RequestRecord` on ingest — timestamping happens *here*, server-side, fixing
finding #15 (the agent's own clock is never trusted for bucketing, only recorded for skew).

### 4.3 TTFT/ITL capture — harness change

`ModelHarness.stream_with_telemetry` records the monotonic time of the *first* yielded chunk
(`ttft`) and the gaps between subsequent chunks (`itl` samples, averaged). `invoke_with_telemetry`
(non-streamed) reports `ttft_ms = null` — it's not measurable without streaming, and the metrics
schema must distinguish "not applicable" from "zero."

While here: fix finding #24 — `ModelHarness` gets a persistent `httpx.AsyncClient` (one per
worker process, `httpx.Limits(max_connections=20, keepalive_expiry=30)`), constructed once and
reused across calls instead of one-per-request. This matters specifically because Phase 4 is where
latency numbers start being trusted for TTFT comparisons; a fresh TCP+TLS handshake inside every
measurement was never acceptable for that.

### 4.4 Percentiles without unbounded memory

`histogram.py` wraps a t-digest (or HDR) per `(worker_id, node)` pair, fed every `RequestRecord`,
queried for p50/p90/p95/p99 on demand. Bounded memory regardless of run length; raw records still
go to the Parquet artifact for exact offline analysis.

### 4.5 Run artifacts

```
runs/<run_id>/
├── scenario.yaml      # resolved (env vars substituted) — reproducibility
├── summary.json       # aggregates: RPS, tokens/s in+out, percentiles, 429 rate, dropped, git SHA
└── requests.parquet   # every RequestRecord, raw
```

Written incrementally during the run (append to Parquet, flush `summary.json` every few seconds) so
a crashed run still leaves a usable partial artifact.

### 4.6 `GET /metrics` (Prometheus exposition format)

```
agent_harness_requests_total{worker="w1",status="200"} 142
agent_harness_tokens_total{worker="w1",direction="in"} 98213
agent_harness_request_duration_ms{worker="w1",quantile="0.95"} 1830
agent_harness_dropped_iterations_total{worker="w1"} 3
agent_harness_ttft_ms{worker="w1",quantile="0.95"} 410
```

Scrape this from Prometheus; a starter Grafana dashboard JSON ships in
`dashboard/grafana/starter-dashboard.json` with panels for RPS, token throughput, percentile
latency, 429 rate, and dropped iterations — the "analysis tool" half that redesign-spec.md §5
deliberately keeps out of the bespoke UI.

### 4.7 Task breakdown

1. `metrics/record.py`, `metrics/histogram.py`.
2. Harness: persistent client, TTFT/ITL capture.
3. `metrics/store.py` (ring buffer + Parquet writer), `metrics/artifact.py`.
4. `metrics/prometheus.py` + `GET /metrics` route.
5. Starter Grafana dashboard JSON.
6. Tests: TTFT/ITL measured correctly against a fake streaming endpoint with known delays;
   percentile estimator within tolerance of exact on a synthetic dataset; artifact survives a
   simulated mid-run crash (partial `summary.json` is still valid JSON).

---

### 4.8 Implementation notes (what actually shipped, and where it deviated)

**Files added:** `metrics/histogram.py` (`LogHistogram`), `metrics/aggregate.py` (`Aggregate`, shared
by the live store and the on-disk artifact so they can never disagree), `metrics/store.py`
(`MetricsStore`, `default_store`), `metrics/prometheus.py` (`render`), `metrics/artifact.py`
(`RunArtifact`), `dashboard/grafana/starter-dashboard.json`, `tests/test_metrics.py` (23 tests),
`tests/conftest.py` (routes run artifacts to a temp dir so the suite never writes into the repo).
**Files extended:** `harness.py` (persistent HTTP client, TTFT/ITL capture), `telemetry.py`
(`ts_ingest_ms`, `ttft_ms`, `itl_ms_mean`, `remove_listener`), `runner.py` (run artifact, drop
reporting, client release), `scenario/executors/*` (`live_stats`), `control/models.py` +
`client.py` + `api.py` (drops carried on heartbeat), `dashboard/app.py` (ingest stamping, `/metrics`).
**Verified:** `uv run pytest tests/ -q` → 113 passed (up from Phase 3's 96). A real scenario run
produces a finished `summary.json` with the git SHA, executor stats, and one JSONL line per model
call.

**Deviation 1 — log-bucketed histogram instead of t-digest.** A `gamma=1.02` log histogram gives
≤1% relative error with memory bounded by bucket count (a few hundred), with no dependency. Tested
against exact percentiles on 20k lognormal samples.

**Deviation 2 — JSON Lines instead of Parquet.** Parquet needs pyarrow and a finalised file; JSONL
appends per line, so a crashed run keeps every record it got to. Convert offline if you want
columnar analysis. `summary.json` is rewritten atomically (temp file + `os.replace`) every two
seconds, so readers never see a half-written summary.

**Deviation 3 — artifacts are written by the worker, not the dashboard.** The record of a run
survives the dashboard being down, and each scenario's artifact lives next to the process that ran
it. The dashboard's `/metrics` is a separate, live, in-memory view fed from the same events.

**Deviation 4 — aggregation is per worker, not per (worker, node).** The plan's §4.4 said per
`(worker_id, node)`. Per-node latency distributions are useful but multiply series cardinality, and
the first consumer (the starter Grafana dashboard) doesn't need them yet. Easy to add later by
keying `MetricsStore.by_worker` on the pair.

**Deviation 5 — `RequestRecord` was not introduced.** `TelemetryEvent` stayed as the single record
type, extended with the Phase 4 fields, rather than adding a parallel model that would need
converting at every boundary. The atom is still written once and never mutated.

**Deviation 6 — `interrupted` is not recorded.** An interrupt raised at a checkpoint before the HTTP
call emits no telemetry event (existing behaviour), so there is no record to flag. Recording
interrupted calls would mean emitting events from the checkpoints too; deferred.

**Deviation 7 — `/reset` does not clear metrics.** `/reset` clears the live event buffer for the UI,
but `/metrics` counters are cumulative since process start. A Prometheus counter that goes backwards
looks like a restart and corrupts `rate()`.

**Finding #15 (naive local timestamps) — fixed for bucketing.** Every event is stamped with
`ts_ingest_ms` on arrival; buckets, windows and `/metrics` use that. The agent's own `ts` is used only
to record clock skew per worker (`agent_harness_clock_skew_ms`), so a misconfigured agent clock is
visible rather than silently mis-bucketing its traffic.

**Finding #24 (fresh HTTP client per call) — fixed.** Each `ModelHarness` keeps one persistent
`AsyncClient` with a connection pool, created lazily and released with `aclose()`. Covered by a test
asserting the same client is reused across calls.

**TTFT/ITL caveat.** Measured at the generator boundary, so consumer-side delay between yields is
included in ITL. For a load harness this is the honest number (it's what the caller experiences),
but it is not a pure server-side figure.

---

## Phase 5 — UI rebuild

**Delivers:** the delta-protocol frontend. Deliberately last — rebuilding against a protocol that's
still changing underneath it would be wasted work. Kills redesign-spec.md §2.3 (findings #13, #14,
#16 — already partially fixed in Phase 0 — #20).

### 5.1 Transport (`dashboard/app.py` additions)

Three independent channels, matching redesign-spec.md §4.6:

| Channel | Protocol | Payload |
|---|---|---|
| `GET /api/state` then `WS /api/state/stream` | WebSocket, delta | `{revision, patch: [...]}` (JSON Patch — RFC 6902 — against the fleet/budget/scenario state) |
| `WS /api/metrics/stream?since=<cursor>` | WebSocket, append-only | `{cursor, rows: [{t, worker_id, tokens_in, tokens_out, requests, errors_429, dropped}, ...]}` |
| `WS /api/events/stream?since_seq=<n>` | WebSocket, append-only | `{rows: [RequestRecord, ...]}`, keyed by the existing `seq` |

Client reconnect: on any drop, re-`GET /api/state` (full resync) and re-request metrics/events
`since` its last known cursor/seq — never re-sent as a full history, never silently gapped either.

### 5.2 Frontend state (`dashboard/frontend/src/store/`)

Zustand (small, no boilerplate, fits a dashboard this size better than full Redux Toolkit) with
three slices mirroring the three channels:

```
store/
├── fleetSlice.ts      # patched from /api/state deltas — workers, budgets, scenarios
├── metricsSlice.ts     # append-only ring buffer per worker, fixed-capacity
└── eventsSlice.ts      # append-only log, keyed by seq, fixed-capacity
```

Components read via `useSyncExternalStore`-based selectors; charts receive memoized, stable-identity
data arrays so recharts doesn't remount on every tick (redesign-spec.md finding #13's actual fix —
Phase 0's fix was only the chart/limit unit bug, not the remount-thrash).

### 5.3 Components

- `FleetView` — one card per worker; Start/Stop/Interrupt/Kill wired to the Phase 1 API; status
  badge driven by the server-derived `status` field (never recomputed client-side); explicit
  **"pending…"** state with elapsed time when `applied_generation < generation`.
- `BudgetPanel` — the Phase 2 form-submit editor.
- `ScenarioLauncher` — lists scenario YAML files, shows the resolved plan (`--dry-run` output) before
  launch, replaces the `/act/1` / `/act/2` buttons.
- `LiveChart` — per-worker sparkline from `metricsSlice`, normalized tokens/min, `isAnimationActive
  ={false}`.
- `EventLog` — virtualized (e.g. `react-window`) so a long-running log doesn't bloat the DOM; rows
  keyed by `seq` (Phase 0's fix, carried forward unchanged).
- `ConnectionBadge` — subscribes to all three WS connection states; shows reconnecting/connected/
  stale, driven by `onopen`/`onerror`/`onclose`, fixing finding #20 (silently empty fleet on drop).

### 5.4 Dev loop

Phase 0's vite proxy (`/events`, `/control`, etc.) is replaced with a proxy for `/api` and the three
`/api/*/stream` WebSocket paths — a smaller, cleaner config now that Phase 1 moved everything under
one prefix (closing the other half of redesign-spec.md finding #19).

### 5.5 Task breakdown

1. `GET /api/state` + `WS /api/state/stream` (JSON Patch deltas) in `dashboard/app.py`.
2. `WS /api/metrics/stream`, `WS /api/events/stream`, both cursor-based.
3. Zustand store + three slices + reconnect-and-resync logic.
4. Component rebuild: `FleetView`, `BudgetPanel`, `ScenarioLauncher`, `LiveChart`, `EventLog`,
   `ConnectionBadge`.
5. Vite proxy update.
6. Tests: a Playwright (or equivalent) smoke test driving Start → see status flip to `running` →
   Stop → see `draining` then `stopped`, against a real (not mocked) backend — this is the
   regression test for "state issues... struggling to stop them" specifically.

---

## Cross-phase notes

- **Backward compatibility window:** Phase 1's shims for `/register`/`/control`/`/events` should be
  removed once Phase 5 ships (nothing should still be calling them by then).
- **Scale checkpoint:** if fleet size grows past ~50 workers on one machine, revisit §7 open question
  3 in `redesign-spec.md` — SQLite + HTTP polling is comfortable below that; past it, a push-based
  (WebSocket-from-worker) or ZMQ-style (Locust) transport for heartbeats is worth it.
- **Don't let Phase 3 block on Phase 1/2 being "finished":** the executor/scenario layer only needs
  `WorkerStopped`/`stop_event` (Phase 0) and a harness to drive — it doesn't need budgets or the
  supervisor. If sequencing pressure appears, Phase 3 can run in parallel with Phase 2.
