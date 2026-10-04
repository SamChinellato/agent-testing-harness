# Redesign Spec: Control Plane, Load Model, and Observability

**Status:** draft for review
**Date:** 2026-10-04
**Scope:** `maasagentharness/` (harness, runner, dashboard) and `dashboard/frontend/`

---

## 1. Why redesign

The current build works as a demo and fails as a tool. The four symptoms you reported are not
four bugs; they are four views of one architectural problem:

> The dashboard has no authority over the agents, and no knowledge of whether they are alive.

Everything else follows. The dashboard is a suggestion box: it writes a desired value into an
in-memory dict and hopes an agent polls it. It cannot start a process, cannot abort an in-flight
request, cannot distinguish "stopped" from "dead", and loses the entire fleet on restart.

A second, quieter problem: **the tool cannot generate calibrated load.** Each agent process issues
strictly one request at a time, spacing cycles with a hardcoded `asyncio.sleep` chosen by mode
(`runaway` 0.05s / `naive` 1.0s / `paced` 0.5s — `runner.py:216-221`). There is no way to express
"60 requests per minute", "20 concurrent", or "ramp 1→50 over two minutes". The node named
`parallel_workers` runs sequentially (`research.py:125-133`). For a load-simulation harness this is
the central missing capability, and no amount of dashboard work fixes it.

---

## 2. Root-cause analysis of the reported symptoms

### 2.1 "State issues — starting an agent in the console, struggling to stop them"

| # | Cause | Location |
|---|---|---|
| 1 | **Unknown workers default to `running`.** `GET /control/{user_id}` returns `state: "running"` for any unregistered id. Registration happens *once*, at agent startup. So: restart the dashboard, and every connected agent silently resumes burning tokens — with no card in the UI, because `control_states` is empty. Same if you start an agent while the dashboard is down. The only way to stop it is to find the PID. | `dashboard/app.py:93-97` |
| 2 | **Stop is only checked between whole cycles.** A research cycle is planner + 6–8 workers + synthesiser. `poll_control()` runs before and after a cycle, never during. `ResearchAgent` is handed `stop_check=self._is_running`, but that reads `self.control_state`, which nothing refreshes mid-cycle — so the check is frozen for the cycle's entire duration. | `runner.py:130-221`, `research.py:130` |
| 3 | **Retry backoff is uninterruptible.** `await asyncio.sleep(backoff)` with `backoff = 2**attempt + jitter`, up to 3 attempts. A throttled agent can sit in unbreakable sleeps for ~14s per call, ×3 calls per triage cycle. | `harness.py:151-159` |
| 4 | **Desired state is displayed as actual state.** The UI renders `control[user].state`. Kill an agent process and its card stays green `RUNNING` forever (`isOnline = total_calls > 0 \|\| isRunning`). There is no heartbeat, no liveness, no deregistration, and no acknowledgement that a command was received. | `App.tsx:220-221`, `dashboard/app.py:68-75` |
| 5 | **Partial control records corrupt agent identity.** `POST /control/{user_id}` takes `Dict[str, Any]`, and for an unknown user creates `{"user":…, "team": "team-alpha"}` — no `state`, `agent`, or `mode`. The next poll builds `AgentControlState(**data)`, which fills defaults: the agent silently flips to `agent=triage, mode=naive, state=running`. Arbitrary keys are accepted and never validated. | `dashboard/app.py:100-105`, `runner.py:102` |
| 6 | **No persistence.** `control_states`, `agent_registry`, `team_limits`, `events_buffer` are module-level dicts. Dashboard restart = total amnesia, while the agents keep running (see #1). | `dashboard/app.py:33-42` |

### 2.2 "Tokens per minute challenging to set from the UI"

| # | Cause | Location |
|---|---|---|
| 7 | **Rapid clicks are lost.** `+`/`−` compute from `currentLimit`, which comes from the last SSE snapshot — up to 1s stale. Click `+` five times fast and you get +50, not +250, because `currentLimit` has not advanced yet. | `App.tsx:99-108` |
| 8 | **The input fights the stream.** A controlled `<input>` whose value is overwritten every second by SSE unless focused. `onBlur` silently discards anything that fails `>= 50`, snapping back with no feedback. | `App.tsx:90-104` |
| 9 | **Two unreconciled sources of truth.** `control_states[user].limit_tpm` (what the editor writes) and `team_limits[team]` (what `POST /limits/{team}` writes, clobbering every member). `team_limits` is seeded at registration and never updated from per-agent edits. | `dashboard/app.py:42,76-77,108-115` |
| 10 | **The number shown is not the number enforced.** The bucket targets `limit_tpm * paced_target_ratio` (0.90), and the ratio is read from the *agent's own* env — invisible and unsettable from the UI. | `runner.py:164-167`, `config.py:24` |
| 11 | **The limit is inert in two of three modes.** `naive` and `runaway` construct no pacer at all, so editing TPM does nothing and the UI gives no hint of that. | `harness.py:38-40` |
| 12 | **The limit line is 12× wrong.** The chart series is *tokens per 5-second bucket*; `ReferenceLine y={ctrl.limit_tpm}` is *tokens per minute*. The line therefore sits ~12× above any real traffic and can never be crossed. | `dashboard/app.py:162-196`, `App.tsx:325-330` |

### 2.3 "Dashboards not rendering properly"

| # | Cause | Location |
|---|---|---|
| 13 | **Full-snapshot replacement once per second.** `setData(parsed)` swaps the entire state object every tick — control, registry, metrics, 12 chart buckets, 30 log rows. Every chart and table remounts, recharts animations restart from zero, nothing is memoized. This is the animation thrash you are seeing. | `App.tsx:153-164` |
| 14 | **The chart schema changes when the fleet changes.** Series are per-user dynamic column names (`${userId}_tokens`). Register or lose an agent and every row's shape changes, remounting all `<Line>` elements. | `dashboard/app.py:167-170` |
| 15 | **Timestamps are naive local time, from the agent's clock.** `TelemetryEvent.ts` uses `datetime.now().isoformat()` (no tz); the dashboard reparses and buckets it against *its own* local clock, with bucket labels built by `datetime.fromtimestamp(...).strftime("%H:%M:%S")`. Any container/TZ/clock skew silently drops events off the chart. Parse failure falls back to `now`, mislabelling old events as current. | `telemetry.py:13`, `dashboard/app.py:166-196` |
| 16 | **Log rows keyed by array index.** `key={i}` over a list that shifts every second — React reuses the wrong DOM nodes, producing the flicker and apparent row misalignment. | `App.tsx:431` |
| 17 | **No dev proxy.** `vite.config.ts` has no `server.proxy`, so `npm run dev` on :5173 cannot reach `/events/stream` on :8000. Every UI change requires `tsc -b && vite build`. This alone makes the UI nearly unmaintainable. | `vite.config.ts` |
| 18 | **Silent 404 when the frontend is not built.** The SPA route is only registered `if FRONTEND_DIST.exists()`. Forget `npm run build` and `/` returns a bare 404 with no explanation. | `dashboard/app.py:241-250` |
| 19 | **Path traversal.** `@app.get("/{full_path:path}")` → `FileResponse(FRONTEND_DIST / full_path)` with no containment check. The catch-all also shadows any future API route added after it. | `dashboard/app.py:245-250` |
| 20 | No SSE `onerror` handling and no connection indicator; after a dashboard restart the browser silently reconnects to an empty fleet and the cards just vanish. | `App.tsx:153-164` |

### 2.4 Independent bugs found during review (worth fixing regardless of the redesign)

| # | Bug | Location |
|---|---|---|
| 21 | **The assistant agent sends an invalid role.** Messages are mapped with `m.type if m.type != "human" else "user"`. LangChain's `AIMessage.type` is `"ai"`, so as soon as history contains a reply the payload carries `role: "ai"` — not a valid OpenAI role; an OpenAI-compatible gateway rejects it with 400. Combined with #22 this surfaces as mysterious 500s with no content. *(Confirm once against your gateway — dependencies are not installed locally, so this is a static finding.)* | `harness.py:85`, `harness.py:192` |
| 22 | **`stream_with_telemetry` swallows every exception.** `except Exception: status = 500` and no re-raise, so the generator just ends. `AssistantAgent`'s `except RateLimitExceeded` is dead code, and a throttled assistant looks *idle* rather than throttled. | `harness.py:235-238`, `assistant.py:88-92` |
| 23 | **`uv run probe` does nothing.** The entry point `probe = "maasagentharness.probe:main"` targets an `async def main`; calling it returns a coroutine that is never awaited. The documented 90-second probe has never run. | `pyproject.toml:23`, `probe.py:10` |
| 24 | **A fresh `httpx.AsyncClient` per request.** A new TCP+TLS handshake is included in every latency measurement and caps throughput. For a tool whose output *is* latency numbers, this is a measurement-validity defect, not an optimisation. | `harness.py:81`, `harness.py:188` |
| 25 | **The documented 15-minute window does not exist.** `WINDOW_SECONDS` and `cutoff` are computed and unused; the real policy is "trim to the last 1000 events once the buffer passes 2000". `docs/dashboard.md` states a 15-minute retention window. | `dashboard/app.py:33,81-90` |
| 26 | **The tests do not run.** Both files are `async def test_*` invoked from `__main__`, with no pytest or pytest-asyncio dependency. Under `pytest` they are collected and skipped with a coroutine warning; nothing is asserted in CI. | `tests/`, `pyproject.toml` |
| 27 | Graph recompiled every cycle (`TriageAgent`/`ResearchAgent` constructed per iteration), `policies_index.json` re-read per cycle, and `POLICIES_DIR.glob()` + `read_text()` executed inside the event loop on every worker call. | `runner.py:171,188`, `research.py:90-93` |
| 28 | No auth on any endpoint, `CORSMiddleware(allow_origins=["*"], allow_credentials=True)`, bound to `0.0.0.0`. Fine on a laptop; not fine on a shared cluster. | `dashboard/app.py:25-31,255` |
| 29 | Deprecated `@app.on_event("shutdown")` (use lifespan); `pyproject.toml` name is `maas-agent-demo` while the repo is `agent-testing-harness`, and the README's quick start says `cd maas-agent-demo`. | `dashboard/app.py:21`, `pyproject.toml:2` |

---

## 3. How other projects solve this

### Locust — a master that *owns* the workers
The master runs the web UI and tells workers when to spawn and stop users; workers run the users and
report statistics back. The master runs no users itself. Control flows over ZMQ with an explicit
message vocabulary — `client_ready`, `spawn`, `stop`, `quit`, `stats`, **`heartbeat`**, **`ack`**,
`spawning_complete`. Two things to steal: start/stop is a *lifecycle operation on a process the
master controls*, and there are first-class heartbeat and ack messages, so the master always knows
which workers are alive and which have applied a command.

Its web UI is also instructive: Vite + React + **Redux Toolkit**, with state deliberately split into
a `swarm` slice (test configuration — a form you submit) and a `ui` slice (runtime metrics — polled
every 2s). Configuration and telemetry are different data with different update semantics. The
current `App.tsx` merges both into one `useState` snapshot replaced wholesale every second, which is
exactly why the TPM input fights the stream.

### k6 — scenarios, executors, and graceful stop
k6 makes the load shape a first-class, declarative object. Closed-model executors (`constant-vus`,
`ramping-vus`) hold concurrency fixed; open-model executors (`constant-arrival-rate`,
`ramping-arrival-rate`) hold the *arrival rate* fixed and add VUs as needed. This distinction
matters directly for your use case: in a closed model, when the endpoint slows down your agents slow
down with it, **hiding the very saturation you are trying to expose** (coordinated omission). Only
an open model keeps offering load on schedule and reports *dropped iterations* when it cannot — the
clearest saturation signal there is, and one the current harness cannot produce at all.

For stopping, k6 separates "stop starting new iterations" from "interrupt what is running":
`gracefulStop` (default 30s) lets in-flight iterations finish before forced interruption, because
interrupting mid-iteration skews metrics. That is the three-phase model adopted in §4.2.

k6 also **deprecated and removed its own web UI** — the maintainers' stated reasons were that nobody
used it, it "kinda sucks", and it added a lot of build time. The replacement direction was a thinner
control surface plus real metrics backends: results stream to Prometheus/Mimir/Datadog/Timescale and
are visualised in Grafana, which buys historical comparison across runs, correlation with
application telemetry, and alerting — none of which a bespoke SSE dashboard will ever provide.
The lesson is not "delete your UI"; it is **stop making your UI the analysis tool.**

### GuideLLM / genai-perf / vLLM `benchmark_serving` — LLM-specific measurement
GuideLLM (now under the vLLM project) uses a Scheduler with pluggable scheduling strategies —
constant rate, concurrency-based, and **Poisson** arrivals. Its `sweep` rate-type first runs a
synchronous benchmark (one request at a time) for a latency floor, then a throughput benchmark
(maximum parallelism) to find the RPS ceiling, then fills in intermediate rates — which is a
principled version of what `probe.py` was reaching for. Reported metrics are RPS, concurrency,
**TTFT** (time to first token), **ITL** (inter-token latency), and end-to-end latency, as
distributions rather than means. The field has converged on these as the metrics that matter for LLM
serving; the current harness reports none of them. Note also the architectural split the ecosystem
has settled into: single-process tools for micro-benchmarks, multi-process frameworks (GuideLLM,
Inference Perf, NVIDIA AI Perf) for production-scale load.

### Kubernetes-style reconciliation — the control-plane fix
Instead of the control plane *sending commands* (lost commands, queued stale commands, ack/retry/
rollback complexity), it *publishes desired state*; each node observes, diffs, and reconciles toward
it. The loop is **level-triggered**: correctness depends on current state, not on having seen every
event, which makes it idempotent and crash-safe. Heartbeat loss makes a worker *suspect* and stops
new assignments, but does not by itself revoke its lease; reassignment waits for lease expiry.

Your current design is already pull-based, which is the right instinct — it just lacks every
mechanism that makes pull-based control correct: a generation counter, reported observed state,
heartbeats, leases, and a safe default for unknown workers. Note the direction of the default:
unknown worker ⇒ **do nothing**. Today it is unknown worker ⇒ `running` (bug #1).

---

## 4. Target architecture

```
                        ┌───────────────────────────────────────────────┐
                        │  harness daemon  (one process)                │
                        │                                               │
   scenario.yaml  ──────▶  Scenario loader                              │
                        │        │                                      │
                        │        ▼                                      │
                        │  Supervisor ──spawn/signal──▶ Worker procs    │
                        │        │                       (N, isolated)  │
                        │        ▼                              │       │
                        │  State store (SQLite)  ◀──heartbeat───┤       │
                        │   · desired + generation              │       │
                        │   · observed + applied_gen            │       │
                        │   · lease / heartbeat_at              │       │
                        │   · budgets (scoped)                  │       │
                        │        │                              │       │
                        │  Metrics store  ◀────request records──┘       │
                        │   · histograms (live)                         │
                        │   · raw rows → run artifact                   │
                        └───────┬───────────────────┬───────────────────┘
                                │                   │
                   /api + /stream (deltas)      /metrics (Prometheus)
                                │                   │
                                ▼                   ▼
                        Control UI (React)       Grafana
                        · fleet + lifecycle      · percentiles, history,
                        · budgets (forms)          cross-run comparison
                        · live sparklines
```

Three deliberate separations:

1. **Control plane ≠ telemetry sink ≠ presentation.** Today all three are one FastAPI module and
   four module-level dicts.
2. **Live control surface ≠ analysis tool.** The built-in UI answers "what is running right now and
   make it stop". Grafana answers "what were the p99s and how does that compare to yesterday".
3. **Desired state ≠ observed state.** Never rendered as the same thing again.

### 4.1 Control plane

**Worker record** (one row per worker, persisted):

```python
class WorkerRecord(BaseModel):
    worker_id: str                  # stable; survives restart
    team: str
    display_name: str
    managed: bool                   # spawned by this supervisor, or attached externally
    pid: int | None

    # desired — written only by the control plane
    desired: DesiredState           # state, agent_type, executor, budget_ref
    generation: int                 # bumped on every desired-state change

    # observed — written only by the worker, via heartbeat
    phase: Phase                    # see 4.2
    applied_generation: int         # which generation the worker has actually applied
    current_node: str | None
    iteration: int
    heartbeat_at: datetime          # UTC, server-stamped
    lease_expires_at: datetime
    last_error: str | None
```

Rules:

- `GET /api/workers/{id}/desired` for an unknown worker returns **`state: "stopped"`, `unregistered: true`** — never `running`. (Fixes #1.)
- The worker **re-registers on every heartbeat** (idempotent upsert), so a daemon restart self-heals the fleet instead of orphaning it. (Fixes #6.)
- Desired-state writes use a typed patch model with `extra="forbid"`, applied only to an existing record. Never materialise a partial record. (Fixes #5.)
- Every write bumps `generation` and returns it. The worker echoes `applied_generation` in its next heartbeat.
- The UI derives, rather than displays, status:
  - `applied_generation < generation` → **"pending…"** with the age of the pending command.
  - `now > lease_expires_at` → **`lost`**; the card greys out and offers *Forget*.
  - Heartbeat stale but lease valid → **`unknown`**; no new commands are issued.
  (Fixes #4 — and it is what makes "I clicked Stop and nothing happened" impossible to experience, because the UI shows the command as accepted-but-pending with a reason.)
- Heartbeat interval 1s, lease TTL 5s. Both configurable; lease TTL ≥ 3× interval.

### 4.2 Lifecycle — three-phase stop

Phases: `starting → running → draining → stopped`, plus `unknown` / `lost` / `errored`.

| Action | Semantics |
|---|---|
| **Start** | Managed: supervisor spawns the process. Attached: desired `running`; worker begins the next iteration. |
| **Stop** (graceful) | Stop starting new iterations; let the in-flight iteration finish, bounded by `graceful_stop` (default 30s). Phase shows `draining` with a countdown and the node it is waiting on. |
| **Interrupt** | Cancel in-flight requests immediately — `asyncio.Task.cancel()`, which aborts the `httpx` request. Partial iterations are tagged `interrupted: true` and excluded from latency aggregates. |
| **Kill** | Managed workers only: `SIGTERM`, then `SIGKILL` after a timeout. This is the escape hatch the dashboard has never had. |

Cancellation plumbing — the specific fix for "struggling to stop them":

- A `stop_event: asyncio.Event` is threaded into `ModelHarness` and checked **before every model call**, not between cycles. (Fixes #2.)
- Every sleep becomes interruptible. Replace `await asyncio.sleep(backoff)` with
  `await asyncio.wait_for(stop_event.wait(), timeout=backoff)` wrapped for `TimeoutError` — so a
  throttled agent in exponential backoff stops instantly rather than after ~14s. (Fixes #3.)
- Agents stop needing custom `stop_check` callbacks (`research.py`); cancellation is a harness
  concern, uniform across every agent, including inside the research worker loop and mid-stream.

### 4.3 Budgets (replaces `limit_tpm` + `team_limits`)

One first-class, named, scoped object. **Single source of truth.** (Fixes #9.)

```python
class Budget(BaseModel):
    budget_id: str
    scope: Literal["global", "team", "worker"]
    scope_ref: str | None
    tokens_per_minute: int | None
    requests_per_minute: int | None
    max_concurrency: int | None
    headroom: float = 0.90           # server-side and visible — replaces paced_target_ratio
    enforcement: Literal["off", "client_bucket", "shadow"] = "client_bucket"
```

- Most specific scope wins (`worker` > `team` > `global`). The UI shows **which scope is binding**
  and the **effective enforced rate** (`tokens_per_minute × headroom`) next to the nominal one, so
  the displayed number is never a different number from the enforced one. (Fixes #10.)
- `enforcement: "off"` makes inertness explicit instead of silent: the UI shows "not enforced
  (runaway)" rather than a live-looking editable field that does nothing. (Fixes #11.)
- `enforcement: "shadow"` is new and useful — compute what the bucket *would* have delayed and
  report it as `shadow_queue_wait_ms` without actually delaying. You get "this agent is 3× over
  budget" without altering the load you are measuring.
- **UI semantics change from live-sync to form-submit** (Locust's `swarm` slice): a local draft with
  explicit *Apply* / *Revert* and a dirty indicator. The server is told once, returns the applied
  budget and new generation, and the UI reconciles. No `useEffect` syncing against a 1s stream, no
  arithmetic on stale snapshots. (Fixes #7, #8.)
- Budget changes are also recorded in the run's event timeline, so a chart can annotate *"limit
  changed 2000 → 4000 here"* — currently impossible to reconstruct.

### 4.4 Load model — scenarios and executors

Scenario as a version-controlled file; the UI can launch it and override parameters, replacing the
two hardcoded `/act/{1,2}` buttons.

```yaml
run: runaway-vs-paced
endpoint: { base_url: ${MAAS_ENDPOINT}, model: granite-3.1-2b-instruct }
budgets:
  - { budget_id: team-pool, scope: team, scope_ref: banking-ops, tokens_per_minute: 6000 }
scenarios:
  steady-triage:
    agent: triage
    executor: constant_arrival_rate      # open model
    rate: 30                             # iterations/min — offered regardless of latency
    duration: 10m
    max_vus: 50
    budget: team-pool
    graceful_stop: 30s
  runaway-research:
    agent: research
    executor: ramping_vus                # closed model
    stages: [{ target: 1, duration: 1m }, { target: 10, duration: 2m }]
    start_time: 2m
    budget: { enforcement: off }
thresholds:
  - p95(total_latency_ms) < 8000
  - rate(status_429) < 0.05
```

Executors:

| Executor | Model | Use |
|---|---|---|
| `constant_vus` | closed | N concurrent agent loops — the current behaviour, N=1 |
| `ramping_vus` | closed | capacity planning against concurrency limits |
| `constant_arrival_rate` | open | steady offered load; **exposes saturation** |
| `ramping_arrival_rate` | open | realistic traffic shapes |
| `poisson_arrival_rate` | open | bursty arrivals (GuideLLM-style) |
| `sweep` | derived | synchronous floor → throughput ceiling → intermediate rates, auto-stopping on threshold breach. Replaces `probe.py`. |

Required new metric: **dropped iterations** — every time an open-model executor cannot start an
iteration on schedule. This is the coordinated-omission signal and the single most informative
number a load tool produces. It is currently unobservable because the closed-model `sleep` loop
simply slows down with the endpoint.

### 4.5 Metrics

Per-request record (the atom; written once, never mutated):

```
run_id, scenario, worker_id, iteration, node, attempt, interrupted,
ts_ingest_utc_ms,          # server-stamped — authoritative for bucketing
ts_agent_utc_ms,           # agent-stamped — kept only to measure clock skew
status, prompt_tokens, completion_tokens,
ttft_ms, itl_ms_mean, total_ms,
queue_wait_ms,             # time spent waiting on the pacer (or shadow equivalent)
ratelimit_headers
```

All timestamps **UTC epoch milliseconds, stamped at ingest**. (Fixes #15.)

Aggregates: RPS, tokens/s in and out separately, p50/p90/p95/p99 of `total_ms` and `ttft_ms`,
status-code breakdown, 429 rate, retry count, dropped iterations, and **achieved vs target rate**.
Percentiles from streaming histograms (HDR or t-digest) so memory is bounded, with raw rows written
to the run artifact for offline analysis. Current code reports only a 60s token sum and a 429
count — no percentiles, no TTFT, no ITL, no throughput.

**Run artifacts.** Every run gets `runs/<run_id>/` containing `scenario.yaml` (resolved),
`summary.json`, `requests.parquet`, and the git SHA. This is what makes two runs comparable; today
nothing survives the process and the question "was yesterday better?" is unanswerable.

**`GET /metrics`** in Prometheus format, so Grafana handles percentiles, history, cross-run
comparison, and alerting, while the built-in UI stays a control surface. Replaces the ambition of
building an analysis UI by hand.

### 4.6 Transport — three data classes, three update semantics

The root of the rendering problems is that everything travels as one snapshot replaced every second.
(Fixes #13, #14, #16, #20.)

| Data | Shape | Transport |
|---|---|---|
| **State** (workers, desired/observed, budgets, scenario) | small, low-frequency, must be correct | `GET /api/state` once, then deltas keyed by a monotonic `revision` over one WebSocket (or SSE). Client re-fetches on revision gap. |
| **Metrics** (time series) | append-only | frames *since the client's cursor* only. Client keeps a ring buffer and appends. History is never resent. |
| **Events** (log) | append-only | monotonic `seq`; client appends and trims. **Rows keyed by `seq`**, never by index. |

Fixed long-format series schema, so it never changes as the fleet changes:

```json
{ "t": 1759600000000, "worker_id": "w1", "tokens_in": 740, "tokens_out": 31,
  "requests": 1, "errors_429": 0, "dropped": 0 }
```

One row per worker per bucket. Charts select by `worker_id` instead of relying on dynamically named
columns, so registering a worker no longer remounts every line. Charts plot **normalised
tokens/min** so the budget reference line is directly comparable. (Fixes #12.)

Client: a real store (Zustand or Redux Toolkit) with `useSyncExternalStore`, memoised chart data,
`isAnimationActive={false}` for live series, and a visible connection indicator driven by
`onerror`/`onopen`.

### 4.7 Serving and dev loop

- **Prefix every API route with `/api/`** and mount the SPA on one explicit route, so the catch-all
  can never shadow an endpoint. (Fixes #19.)
- Replace the hand-rolled catch-all with `StaticFiles(..., html=True)`, or resolve and verify
  containment (`path.resolve().is_relative_to(dist)`). (Fixes #19.)
- Always register the SPA route; when `dist/` is missing, serve a small page saying
  *"frontend not built — run `npm --prefix dashboard/frontend run build`"*. (Fixes #18.)
- Add `server.proxy` to `vite.config.ts` for `/api` and `/stream` → `http://localhost:8000`, so
  `npm run dev` gives hot reload against the live daemon. (Fixes #17.)
- Lifespan handler instead of `@app.on_event`; optional bearer token on `/api/*`; bind `127.0.0.1`
  by default. (Fixes #28, #29.)

### 4.8 Harness correctness fixes

- **Role mapping table** instead of `m.type`: `{system: system, human: user, ai: assistant, tool: tool}`. (Fixes #21.)
- `stream_with_telemetry` emits telemetry **and re-raises**; `RateLimitExceeded` propagates so a throttled stream reports as throttled. (Fixes #22.)
- One `httpx.AsyncClient` per worker, reused, with explicit `httpx.Limits(max_connections=…, keepalive_expiry=…)`. Record TTFT from the first streamed byte. (Fixes #24.)
- `probe` entry point → sync wrapper calling `asyncio.run(main())`, or delete it once `sweep` exists. (Fixes #23.)
- Compile each agent graph **once** per worker; load corpora once at startup; move file reads off the event loop (`asyncio.to_thread`) or preload into memory. (Fixes #27.)
- Add `pytest` + `pytest-asyncio` as a dev group, convert the two scripts to real assertions, and add regression tests for: unknown-worker default is `stopped`; interrupt cancels within N ms; generation/applied-generation convergence; budget scope resolution; role mapping. (Fixes #26.)
- Make the retention policy real and align the docs with it. (Fixes #25.)

---

## 5. Decision: what happens to the React dashboard

**Recommendation: keep a browser UI, but demote it to a control surface, and rebuild it on the
delta protocol in §4.6.**

Rationale: the screenshots in your README indicate this tool is demo-facing, and a live browser view
is genuinely the right medium for "watch one runaway agent starve three well-behaved ones". But k6's
experience is the warning — a bespoke UI that tries to be the analysis tool accrues exactly the
failures you are hitting. So: the UI owns the fleet, lifecycle, budgets, and live sparklines; Grafana
over `/metrics` owns percentiles, history, and cross-run comparison. That split removes most of what
makes the current UI fragile, because the hard rendering problems all live in the analysis half.

Alternatives considered:

- **Drop the web UI for a TUI** (Textual/Rich) + Grafana. Cheapest to maintain, excellent for a
  single operator, no build step, no SSE, no React. Rejected as the default because it is weak for
  demos and screen-sharing — but worth it if you find you are the only user. The §4.1–4.5 core is
  unchanged either way, so this stays switchable later.
- **Keep the current UI and patch it.** Rejected: issues #13–#16 are protocol-shaped, not
  component-shaped. Snapshot-replace-per-second cannot be patched into append-only semantics, and
  the dynamic per-user column schema has to change server-side anyway.

---

## 6. Phasing

**Phase 0 — hotfixes.** ✅ **Landed** — see [phase-0-hotfixes.md](phase-0-hotfixes.md) for the
full per-item spec, implementation, and verification. Mostly independent of everything below and
worth landing immediately: unknown-worker default → `stopped` (#1); interruptible backoff (#3);
stop checks between model calls (#2); role mapping (#21); re-raise from the stream (#22); `probe`
entry point (#23); vite dev proxy (#17); path-traversal containment (the security half of #19 — the
route-prefix half is bundled into Phase 1, see that doc for why); log rows keyed by `seq` (#16);
chart reference line unit fix (#12); plus four more found useful to bundle in: typed/validated
control patches (#5), real retention window (#25), "frontend not built" message (#18), and tests
that actually run under pytest (#26).

**Phases 1–5 — the full rewrite.** Detailed, file-by-file implementation plan (schemas, API
contracts, module layout, task breakdown per phase) is in
[phase-1-5-implementation-plan.md](phase-1-5-implementation-plan.md). Summary:

**Phase 1 — control plane v2.** SQLite state store; typed desired-state patches; generations and
`applied_generation`; heartbeats and leases; `starting/running/draining/stopped/unknown/lost` phases;
supervisor that spawns and signals managed workers; three-phase stop. *Kills symptom 2.1 entirely.*

**Phase 2 — budgets.** Scoped `Budget` objects, effective-rate resolution, `shadow` enforcement,
form-submit editor with Apply/Revert. *Kills symptom 2.2.*

**Phase 3 — scenarios and executors.** Scenario file, executor set, dropped-iteration accounting,
`sweep`. Retire `/act/{1,2}` and `probe.py`. *Delivers the missing core capability from §1.*

**Phase 4 — metrics v2.** Per-request records, streaming histograms, TTFT/ITL, run artifacts,
`/metrics` + a starter Grafana dashboard.

**Phase 5 — UI rebuild.** Delta protocol, real store, fixed long-format series, connection
indicator, pending-command affordances. *Kills symptom 2.3.*

Phases 1 and 2 are the ones that make the tool feel fixed; Phase 3 is the one that makes it a load
tool. Phase 5 is deliberately last — rebuilding the UI before the protocol changes underneath it
would be wasted work.

---

## 7. Open questions

1. **Managed vs attached workers.** Should the daemon spawn workers (real kill, real start, one
   command) or keep them externally launched (better for multi-machine and for driving load from
   outside the box)? The spec supports both; the default matters for the UX. Recommendation: managed
   by default, attached supported.
2. **Is the TPM budget meant to be per-agent or a shared pool?** Pacing is currently per-process, so
   N agents sharing one API key get N× the limit. If your gateway's limit is per-team, client-side
   pacing needs a shared budget — which means the daemon must hand out tokens, not just a number.
   This changes §4.3 materially.
3. **Scale target.** Tens of workers on one machine, or hundreds across machines? Below ~50 the
   SQLite + HTTP design is ample; above that, the Locust ZMQ-style split matters.
4. **Multi-tenancy / auth.** Laptop-only, or shared cluster? Determines whether §4.7's bearer token
   is sufficient.
5. **Agent realism.** Should the included agents keep using LangGraph, or should the load path be
   decoupled from LangGraph so you can measure endpoint behaviour without framework overhead in the
   latency numbers?

---

## Sources

- [Locust — distributed load generation](https://docs.locust.io/en/stable/running-distributed.html)
- [locustio/locust architecture (DeepWiki)](https://deepwiki.com/locustio/locust)
- [k6 — open and closed models](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/)
- [k6 — executors](https://grafana.com/docs/k6/latest/using-k6/scenarios/executors/)
- [k6 — graceful stop](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/graceful-stop/)
- [Coordinated omission: why open-loop workloads are vital](https://k6.wiki/k6-coordinated-omission-open-vs-closed-workloads)
- [grafana/k6 #300 — deprecate and remove the current Web UI](https://github.com/grafana/k6/issues/300)
- [GuideLLM — run a benchmark](https://vllm-project.github.io/guidellm/0.7.4/getting-started/benchmark/)
- [GuideLLM — evaluate LLM deployments for real-world inference (Red Hat)](https://developers.redhat.com/articles/2025/06/20/guidellm-evaluate-llm-deployments-real-world-inference)
- [Reconciliation loops (arc42 quality)](https://quality.arc42.org/approaches/reconciliation-loops)
- [Kubernetes operators: how operators work (Red Hat)](https://developers.redhat.com/articles/2021/06/22/kubernetes-operators-101-part-2-how-operators-work)
- [Introduction to LLM inference benchmarking](https://rudeigerc.dev/posts/en/llm-inference-benchmarking/)
