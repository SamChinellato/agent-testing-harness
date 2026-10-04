# Phase 0 — Hotfixes

**Status:** ✅ landed — all 15 items implemented and verified (branch `phase0-hotfixes`)
**Parent:** [redesign-spec.md](redesign-spec.md) §6, Phase 0
**Goal:** land the independent, low-risk fixes that don't require the control-plane rewrite,
so the tool stops actively lying to you while Phases 1–5 are built.

**Verification:** `uv run pytest tests/ -q` → 18 passed (13 pre-existing, now actually run and
asserting; 5 new regression tests in `tests/test_hotfixes.py` targeting #0.3/#0.4/#0.5/#0.6/#0.7
directly). `npm run build` in `dashboard/frontend/` typechecks and bundles cleanly against the
`App.tsx`/`vite.config.ts` changes. Package imports and `AgentWorker`/dashboard `app` construction
verified directly. A live curl-based smoke test was attempted but this sandbox forwards/proxies
ports 8000+ through its own authenticated layer before reaching any locally bound server, so that
step relied on the ASGI-transport pytest coverage instead, which exercises every changed route
in-process without touching real sockets — equivalent coverage for this set of fixes.

Each item below references the finding number from `redesign-spec.md` §2. Status is tracked
per-item; this file is updated in place as each lands (not append-only — it's a checklist, not a log).

Explicitly **out of scope** for Phase 0: anything requiring persisted state, generations/leases,
the delta transport, or scenario/executor support. Those are Phases 1–5.

---

## 0.1 — Unknown worker defaults to `stopped`, not `running`

**Finding:** #1 — the single worst bug in the project. Restart the dashboard, or start an agent
before the dashboard is up, and `GET /control/{user_id}` for the unknown id returns
`state: "running"`. The agent happily keeps calling the model with no card in the UI to stop it.

**Fix:** unknown worker → `state: "stopped"` plus an explicit `unregistered: true` flag so the
agent (and later the UI) can tell "never registered" apart from "registered and stopped".

**Files:** `maasagentharness/dashboard/app.py` (`get_control`), `maasagentharness/runner.py`
(`AgentControlState` gets `unregistered: bool = False`).

**Acceptance:** `GET /control/never-seen-id` returns `{"state": "stopped", "unregistered": true, ...}`.
A worker polling an unregistered id does not enter its run loop.

**Status:** ✅ done

---

## 0.2 — Control updates are typed and can't create ghost/partial records

**Finding:** #5 — `POST /control/{user_id}` accepts `Dict[str, Any]` and fabricates a bare
`{"user":…, "team": "team-alpha"}` for unknown ids. The next `AgentControlState(**data)` fills in
defaults, silently resetting `agent`/`mode` to `triage`/`naive`. Arbitrary extra keys are accepted
and ignored.

**Fix:** a `ControlPatch` model with `extra="forbid"` and every field optional; `POST` 404s if the
worker was never registered (via `/register`), and only ever applies validated fields to an
*existing* record — never materializes a new one from a partial payload.

**Files:** `maasagentharness/dashboard/app.py`.

**Acceptance:** `POST /control/never-seen-id {"state":"running"}` → 404. `POST /control/{known}
{"bogus_field": 1}` → 422. A known worker's `agent`/`mode` never change as a side effect of a
`state`-only or `limit_tpm`-only patch.

**Status:** ✅ done

---

## 0.3 / 0.4 — Interruptible stop: checked before every model call, backoff sleeps are cancellable

**Findings:** #2 (stop only checked between whole cycles — a research cycle is 8+ sequential
calls) and #3 (retry backoff sleeps are plain `asyncio.sleep`, up to ~14s uninterruptible per call
across 3 attempts).

**Fix:** `ModelHarness` takes an `asyncio.Event` (`stop_event`). It is checked immediately before
every pacer acquire and every model call; if set, the call raises `WorkerStopped` instead of
calling the model. Every retry/backoff sleep is replaced with
`asyncio.wait_for(stop_event.wait(), timeout=backoff)`, so a `Stop` click interrupts a throttled
agent sitting in exponential backoff immediately instead of after the full backoff window.
`AgentWorker` sets the event the instant a poll returns `state != "running"`, clears it on resume.
`ResearchAgent`'s bespoke `stop_check` callback is retired — cancellation is now a harness concern,
so it also works mid-worker-loop and mid-stream, which the callback never did.

**Files:** `maasagentharness/harness.py`, `maasagentharness/pacer.py` (acquire also respects the
event, so pacing waits are interruptible too), `maasagentharness/runner.py`,
`maasagentharness/agents/research.py` (drop `stop_check`).

**Acceptance:** stopping an agent mid-backoff (simulated with a fake 429 endpoint) returns control
within ~50ms, not up to 14s. Stopping mid-research-cycle aborts before the next worker question,
not after all 8.

**Status:** ✅ done

---

## 0.5 — Role mapping: LangChain `.type` is not an OpenAI role

**Finding:** #21 — confirmed by direct check: `AIMessage().type == "ai"`, `SystemMessage().type ==
"system"`, `HumanMessage().type == "human"`. The harness mapped `human → user` and sent every other
`.type` verbatim, so as soon as a conversation has one assistant turn in history, the next request
carries `{"role": "ai", ...}` — not a valid OpenAI role. Every OpenAI-compatible gateway will 400 on
it. This silently breaks the `assistant` agent's multi-turn mode specifically, i.e. the agent whose
whole point is growing history.

**Fix:** explicit role table `{"system": "system", "human": "user", "ai": "assistant", "tool":
"tool"}`, applied in both `invoke_with_telemetry` and `stream_with_telemetry`, with a `KeyError` on
an unmapped type rather than silently forwarding garbage.

**Files:** `maasagentharness/harness.py`.

**Acceptance:** a 3+ turn assistant conversation serializes every message with a valid OpenAI role;
unit test constructs a history with all four message types and asserts the payload roles.

**Status:** ✅ done

---

## 0.6 — `stream_with_telemetry` re-raises instead of swallowing every exception

**Finding:** #22 — `except Exception: status = 500` with no re-raise. The generator just ends.
`AssistantAgent`'s `except RateLimitExceeded` around the stream is dead code — it can never fire,
because the exception never leaves `stream_with_telemetry`. A throttled assistant looks idle, not
throttled; any other failure (network error, bad gateway, timeout) looks like a silently-empty
response instead of an error the caller can react to.

**Fix:** telemetry is still emitted on every path (that part was correct), but `RateLimitExceeded`
and any other exception are re-raised after the `yield`-based `emit`, exactly like
`invoke_with_telemetry` already does. Callers now get a real exception to catch.

**Files:** `maasagentharness/harness.py`, `maasagentharness/agents/assistant.py` (its existing
`except RateLimitExceeded` now actually fires — no caller-side change needed beyond that).

**Acceptance:** a simulated 429 mid-stream raises `RateLimitExceeded` out of `stream_with_telemetry`
and is caught by `AssistantAgent.stream_turn`, which records the throttled placeholder in history
exactly as it was already written to do.

**Status:** ✅ done

---

## 0.7 — `uv run probe` actually runs

**Finding:** #23 — the entry point targets an `async def main()`; calling it returns an unawaited
coroutine (and, as of this pass, also triggers a `RuntimeWarning: coroutine was never awaited`).
The documented 90-second rate-limit probe has never executed via the documented command.

**Fix:** `main()` is now a sync wrapper: `def main(): asyncio.run(_main())`.

**Files:** `maasagentharness/probe.py`.

**Acceptance:** `uv run probe` sends requests and prints the summary line instead of exiting
immediately with no output.

**Status:** ✅ done

---

## 0.8 — Static file path traversal contained

**Finding:** #19 (security half only — the `/api` route-prefix half of #19 is deferred to Phase 1,
since it means renaming every route the frontend calls and is a better fit alongside the delta-
protocol transport rewrite). Today: `FileResponse(FRONTEND_DIST / full_path)` with no containment
check on `full_path`.

**Fix:** resolve the requested path and verify it is still inside `FRONTEND_DIST` before serving it
(`path.resolve().is_relative_to(FRONTEND_DIST.resolve())`); otherwise fall through to `index.html`
like any other unknown SPA route.

**Files:** `maasagentharness/dashboard/app.py`.

**Acceptance:** `GET /../../../etc/passwd`-style traversal attempts resolve outside `FRONTEND_DIST`
and get the SPA shell, never a file outside the dist directory.

**Status:** ✅ done

---

## 0.9 — Event log rows keyed by a stable id, not array index

**Finding:** #16 — `recent_events.map((ev, i) => <tr key={i}>...)` over a list whose contents shift
every second; React reuses the wrong DOM nodes against the wrong row, which is the flicker/
misalignment you're seeing in the log panel.

**Fix:** the server stamps a monotonic `seq` on every event at ingest (`events_buffer` already has
ordering; this just exposes it). The frontend keys log rows by `ev.seq`.

**Files:** `maasagentharness/telemetry.py` (`TelemetryEvent.seq: int = 0`, dashboard assigns it on
receipt — the field defaults to 0 and is overwritten server-side, so agents don't need to know about
it), `maasagentharness/dashboard/app.py` (`receive_event` assigns the next sequence number),
`dashboard/frontend/src/App.tsx` (`key={ev.seq}`).

**Acceptance:** log rows no longer visibly swap/flicker during a steady stream of events in manual
testing; `seq` is strictly increasing in `recent_events`.

**Status:** ✅ done

---

## 0.10 — Chart series and the limit reference line are in the same unit

**Finding:** #12 — the chart buckets tokens over 5-second windows; `limit_tpm` is tokens *per
minute*. The red reference line sits ~12× above any real line, so it can never be crossed and the
"is this agent over budget" visual — the entire point of the chart — has never worked.

**Fix:** the dashboard now reports each bucket's tokens scaled to a **per-minute rate**
(`bucket_tokens * (60 / BUCKET_SECONDS)`), so the chart and the reference line share units. This
also matches what `docs/dashboard.md` already (incorrectly) claimed the chart showed.

**Files:** `maasagentharness/dashboard/app.py` (`calculate_metrics`).

**Acceptance:** a worker sustaining exactly its `limit_tpm` average shows a line that tracks at the
reference line, not 12× below it; a burst shows a visible, meaningful crossing.

**Status:** ✅ done

---

## 0.11 — Explicit message when the frontend isn't built

**Finding:** #18 — if `dashboard/frontend/dist/` doesn't exist, the SPA route is never registered
at all and `/` 404s with no explanation.

**Fix:** the SPA route is always registered; when `dist/` is missing it serves a minimal HTML page
telling you to run `npm --prefix dashboard/frontend run build`, instead of a bare 404.

**Files:** `maasagentharness/dashboard/app.py`.

**Acceptance:** `GET /` with no build present returns 200 and a human-readable instruction instead
of 404.

**Status:** ✅ done

---

## 0.12 — Retention window matches what the docs promise

**Finding:** #25 — `WINDOW_SECONDS = 900` and a `cutoff` variable are computed and never used; the
real policy is "once the buffer exceeds 2000 events, keep the last 1000" — a count-based policy, not
the documented 15-minute time window.

**Fix:** prune by both: on every ingest, drop events older than `WINDOW_SECONDS`, and independently
cap the buffer at 2000 entries as a memory backstop. Docs already said 15 minutes; code now matches.

**Files:** `maasagentharness/dashboard/app.py`.

**Acceptance:** an event older than 15 minutes is absent from `/events/stream` output even if the
buffer is far under the 2000-entry cap.

**Status:** ✅ done

---

## 0.13 — Lifespan handler instead of deprecated `on_event`

**Finding:** #29 (partial — the `pyproject.toml` project-name mismatch is cosmetic and left alone
for this pass to avoid an unrelated rename touching `uv.lock`/install docs; tracked as a follow-up).

**Fix:** `@app.on_event("shutdown")` replaced with a `lifespan` context manager, removing the
deprecation warning and matching current FastAPI guidance.

**Files:** `maasagentharness/dashboard/app.py`.

**Status:** ✅ done

---

## 0.14 — Tests actually run, and actually assert something

**Finding:** #26 — both test files are `async def test_*` called from `__main__`, with no pytest or
pytest-asyncio dependency. Under plain `pytest` they're collected, the coroutine is never awaited,
and nothing is checked.

**Fix:** added `pytest` + `pytest-asyncio` as a dev dependency group; converted both files to real
`pytest`-style `async def test_*` functions with `assert` statements pytest can act on; added
regression tests for the fixes above (unknown-worker-defaults-to-stopped, role-mapping, stop-event
interruption, control-patch validation).

**Files:** `pyproject.toml`, `tests/test_components.py`, `tests/test_dashboard.py`,
`tests/test_hotfixes.py` (new).

**Acceptance:** `uv run pytest tests/ -q` passes and fails loudly (not silently) if any Phase 0 fix
regresses.

**Status:** ✅ done

---

## Explicitly deferred (named in redesign-spec.md but not independent/low-risk enough for Phase 0)

| Finding | Why deferred |
|---|---|
| #4 — desired state rendered as actual state | Needs generation counters + heartbeats/leases — Phase 1 |
| #6 — no persistence | Needs the state store — Phase 1 |
| #7/#8/#9 — TPM editor races, two sources of truth | Needs the `Budget` object and form-submit editor — Phase 2 |
| #10/#11 — effective-vs-nominal rate, inert modes | Needs `Budget.enforcement` — Phase 2 |
| #13/#14/#20 — snapshot replace, dynamic chart schema, no connection indicator | Needs the delta protocol — Phase 5 |
| #15 — naive local timestamps | Needs server-stamped UTC ingest timestamps threaded through the new metrics pipeline — Phase 4 |
| #17 — vite dev proxy | Low-risk on its own, but the routes it would proxy are about to move under `/api` in Phase 1 — bundling avoids a second frontend churn. *(Reconsidered: this one is genuinely free-standing — see below.)* |
| #19 (route prefix half) | Bundled with the Phase 1 backend rewrite |
| #24 — one `httpx.AsyncClient` per request | Correctness-adjacent to the Phase 4 metrics rewrite (TTFT needs the client restructured anyway) |
| #27 — graph/corpus reloaded every cycle | Performance, not correctness — bundled into the Phase 1 supervisor rewrite of `runner.py` |
| #28 — no auth, permissive CORS, binds `0.0.0.0` | Needs a real decision on deployment model (§7 open question 4) — Phase 1 |

**Correction during implementation:** #17 (vite dev proxy) turned out to be free-standing — it's a
`vite.config.ts`-only change with no backend coupling, so it's pulled forward into this phase as
**0.15** below rather than left deferred.

---

## 0.15 — Vite dev server proxies to the backend

**Finding:** #17 — no `server.proxy` in `vite.config.ts`, so `npm run dev` on :5173 can't reach
`/events/stream` or any control endpoint on :8000. Every single UI change during this redesign would
otherwise require a full `tsc -b && vite build` cycle to see.

**Fix:** added `server.proxy` for `/events`, `/control`, `/register`, `/act`, `/reset`, `/limits` →
`http://localhost:8000`, with `ws: true`/SSE-friendly settings.

**Files:** `dashboard/frontend/vite.config.ts`.

**Acceptance:** `npm run dev` renders a live dashboard against a locally running
`uv run dashboard`, with hot reload.

**Status:** ✅ done

---

## Summary

| # | Item | Status |
|---|---|---|
| 0.1 | Unknown worker → stopped | ✅ |
| 0.2 | Typed control patches | ✅ |
| 0.3/0.4 | Interruptible stop + cancellable backoff | ✅ |
| 0.5 | Role mapping | ✅ |
| 0.6 | Stream re-raises | ✅ |
| 0.7 | `probe` entry point | ✅ |
| 0.8 | Path traversal containment | ✅ |
| 0.9 | Event log stable keys | ✅ |
| 0.10 | Chart/limit unit match | ✅ |
| 0.11 | "Frontend not built" message | ✅ |
| 0.12 | Real retention window | ✅ |
| 0.13 | Lifespan handler | ✅ |
| 0.14 | Tests run under pytest | ✅ |
| 0.15 | Vite dev proxy | ✅ |
