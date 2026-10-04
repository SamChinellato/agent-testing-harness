# API Reference

The dashboard serves one JSON API, one live stream, Prometheus metrics, and the built UI. Routes
under `/api/` are the supported surface. The v1 routes (`/register`, `/control/{id}`, `/act/{n}`,
`/reset`, `/limits/{team}`, `/events/stream`) were retired with the Phase 5 UI rebuild.

All examples assume the dashboard at `http://localhost:8000`.

---

## Live read model

### `GET /api/stream`

Server-sent events, one connection for the whole UI.

| Event | Payload | When |
|---|---|---|
| `snapshot` | the complete read model (below) | every second |
| `events` | an array of recent model calls | when there are new calls; its SSE `id` is the last `seq` sent |

On reconnect, the browser sends `Last-Event-ID`, and the server replays every call after that `seq`.
Without the header, a new client receives the most recent 50 calls.

`?once=1` returns one tick and closes. It exists for tests and scripts.

**Snapshot shape**

```json
{
  "revision": 412,
  "timestamp": 1791110000.2,
  "buckets": [1791109945, "... 12 bucket start times, 5 s apart"],
  "workers": [{
    "worker_id": "w1", "team": "banking-ops", "display_name": "Dispute triage",
    "managed": false, "pid": null,
    "desired":  {"state": "running", "agent": "triage", "mode": "paced", "budget_id": null, "generation": 3},
    "observed": {"status": "running", "phase": "running", "applied_generation": 3,
                 "current_node": "classify", "iteration": 812, "last_error": null,
                 "heartbeat_at": 1791110000.1, "lease_expires_at": 1791110005.1},
    "budget":   {"budget_id": "team:banking-ops", "scope": "team", "resolved_via": "team",
                 "tokens_per_minute": 3000, "effective_tokens_per_minute": 2700,
                 "enforcement": "client_bucket", "shared_by": 4, "shared_tokens_per_minute": 17783},
    "live":     {"tokens_per_minute": 2448, "calls_per_minute": 9, "throttled_last_minute": 0,
                 "latest_node": "classify", "latest_status": 200, "latency_ms": 1104},
    "dropped_total": 0,
    "series": [0, 0, 1200, "... 12 values: tokens over the 60 s ending at each bucket"],
    "series_throttled": [0, 0, 1, "... 12 values: throttled calls per bucket"]
  }],
  "budgets": [],
  "teams": ["banking-ops"],
  "totals": {"workers": 4, "running": 4, "tokens_per_minute": 17783,
             "throttled_last_minute": 12, "dropped_total": 0}
}
```

`resolved_via` is one of `worker_assignment`, `team`, `global`, `default`.
`status` is derived on the server. The UI never recomputes it.

### `GET /api/scenarios`

Scenario files under `SCENARIOS_DIR` (default `scenarios/`), each validated.

```json
[{"path": "scenarios/steady-vs-runaway.yaml", "file": "steady-vs-runaway.yaml",
  "run": "steady-vs-runaway", "valid": true, "error": null,
  "scenarios": [{"name": "runaway", "agent": "research", "mode": "runaway",
                 "executor": "ramping_arrival_rate", "budget": "team-shared"}]}]
```

An invalid file has `valid: false` and an `error` that says what to fix.

---

## Workers

Worker commands are the control plane. Changes take effect on the worker's next poll, usually
within a second.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/workers` | Register a worker. Idempotent. Re-registering never resets its desired state. |
| `GET` | `/api/workers` | List workers with their derived status. |
| `GET` | `/api/workers/{id}/desired` | What the worker should be doing. Polled by the worker. |
| `PATCH` | `/api/workers/{id}/desired` | Set `state`, `agent`, `mode` or `budget_id`. An empty `budget_id` clears the worker's own assignment, so it falls back to team or global. |
| `POST` | `/api/workers/{id}/heartbeat` | Worker reports what it has applied. Also carries dropped-iteration counts. |
| `GET` | `/api/workers/{id}/budget` | The budget that applies to this worker, and why. |
| `POST` | `/api/workers/{id}/interrupt` | Cut short the call in progress. The worker keeps running. |
| `POST` | `/api/workers/{id}/spawn` | Start a managed worker process. |
| `POST` | `/api/workers/{id}/kill` | Stop a managed worker process. Returns 409 for externally started workers. |
| `DELETE` | `/api/workers/{id}` | Forget a stopped or lost worker. Returns 409 otherwise. |

---

## Budgets

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/budgets` | All budgets, with effective rates. |
| `GET` | `/api/budgets/{id}` | One budget. |
| `PUT` | `/api/budgets/{id}` | Create or replace a budget. Full replace. |
| `DELETE` | `/api/budgets/{id}` | Delete a budget. Returns 409 while any worker is assigned to it. |

Body for `PUT`:

```json
{"scope": "team", "scope_ref": "banking-ops", "tokens_per_minute": 3000,
 "headroom": 0.9, "enforcement": "client_bucket"}
```

`scope` is `global`, `team` or `worker`. `enforcement` is `off`, `client_bucket` or `shadow`.

---

## Scenario runs

### `POST /api/runs`

Starts one managed worker process per scenario in a file.

```json
{"scenario_path": "scenarios/steady-vs-runaway.yaml", "scenario_names": ["runaway"]}
```

`scenario_names` is optional and defaults to every scenario in the file. Returns the worker ids
started. A file that does not validate returns 400 with the reason.

---

## Telemetry and metrics

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/events` | Agents post one event per model call. The server stamps arrival time. |
| `GET` | `/metrics` | Prometheus text format: request counts, token totals, latency and TTFT summaries, dropped iterations, clock skew. |

---

## UI

Anything else is served from the built UI (`dashboard/frontend/dist/`). If the UI has not been built,
the root path returns a page that says how to build it.
