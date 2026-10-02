# API Reference

The dashboard exposes a REST API used by agents and the browser UI. All endpoints are on the same FastAPI process (`http://localhost:8000` by default).

---

## Agent endpoints

These are called by agent processes.

---

### `POST /register`

Called by an agent on startup to register itself with the dashboard. Creates a lane in the UI.

**Request body:**

```json
{
  "user": "agent-1",
  "team": "Banking Ops",
  "display_name": "Dispute Triage Bot",
  "task_description": "Processing dispute letters",
  "agent": "triage",
  "mode": "paced",
  "state": "stopped",
  "limit_tpm": 2000
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `user` | string | Yes | Unique agent identifier. Used as routing key. |
| `team` | string | Yes | Team label shown in UI. |
| `display_name` | string | No | Human-readable name for the UI lane title. Defaults to `user`. |
| `task_description` | string | No | One-line description shown on the agent card. |
| `agent` | string | No | Agent type label (`triage`, `research`, `assistant`, or custom). |
| `mode` | string | No | Initial mode (`naive`, `paced`, `runaway`). |
| `state` | string | No | Initial state. Should always be `stopped`. |
| `limit_tpm` | int | No | Initial TPM limit. Default: 2000. |

**Response:**
```json
{"status": "registered", "user": "agent-1"}
```

---

### `POST /events`

Called by an agent after every model call to deliver a telemetry event.

**Request body:** A `TelemetryEvent` object:

```json
{
  "ts": "2024-01-15T14:23:45.123456",
  "user": "agent-1",
  "team": "Banking Ops",
  "agent": "triage",
  "mode": "paced",
  "run_id": "t-abc123",
  "node": "classify",
  "status": 200,
  "prompt_tokens": 712,
  "completion_tokens": 24,
  "latency_ms": 1340,
  "ratelimit_headers": {},
  "attempt": 1
}
```

**Response:**
```json
{"status": "ok"}
```

---

### `GET /control/{user_id}`

Called by an agent every second to get its desired state from the dashboard.

**Response:**
```json
{
  "user": "agent-1",
  "team": "Banking Ops",
  "state": "running",
  "agent": "triage",
  "mode": "paced",
  "limit_tpm": 2000
}
```

If the `user_id` has not been registered, returns a default running state with `team-alpha` defaults.

---

## UI / control endpoints

These are called by the browser dashboard when you interact with the controls.

---

### `POST /control/{user_id}`

Updates an agent's control state. The agent picks up the change on its next poll (within ~1 second).

**Request body** (partial update — any fields you want to change):

```json
{"state": "stopped"}
```

```json
{"mode": "runaway"}
```

```json
{"limit_tpm": 3000}
```

**Response:** The full updated control state for that agent.

---

### `POST /limits/{team_id}`

Updates the TPM limit for all agents on a team simultaneously.

**Request body:**
```json
{"limit_tpm": 4000}
```

**Response:**
```json
{"team": "team-alpha", "limit_tpm": 4000}
```

---

### `POST /act/{act_num}`

Sets up a predefined scenario across all connected agents.

| `act_num` | Effect |
|---|---|
| `1` | Normal Operations: first registered agent → stopped; all others → `paced` + `running` |
| `2` | Runaway Agent: first registered agent → `runaway` + `running`; all others → `paced` + `running` |

**Response:** Current control and limits state for all agents.

---

### `POST /reset`

Stops all agents, clears the event buffer, and restores each agent's mode to its registered default.

**Response:**
```json
{"status": "reset", "control": {...}, "limits": {...}}
```

---

## Streaming endpoint

### `GET /events/stream`

Server-sent events (SSE) stream consumed by the browser. Emits one JSON payload per second.

**Content-Type:** `text/event-stream`

**Event format:**
```
data: {"timestamp": 1705320000.0, "control": {...}, "limits": {...}, "agent_registry": {...}, "user_metrics": {...}, "chart_series": [...], "recent_events": [...]}
```

**Payload fields:**

| Field | Description |
|---|---|
| `timestamp` | Unix timestamp of this snapshot |
| `control` | Map of `user_id` → control state for all registered agents |
| `limits` | Map of `team_id` → current TPM limit |
| `agent_registry` | Map of `user_id` → identity metadata (display_name, team, task_description, etc.) |
| `user_metrics` | Map of `user_id` → rolling 1-minute metrics (tpm, status_429_count, total_calls, latest_status, latest_node, latency_ms) |
| `chart_series` | Array of 12 objects (5-second buckets, last 60 seconds), each with `time` and `{user_id}_tokens` fields |
| `recent_events` | Last 30 telemetry events as received, oldest first |

The stream runs indefinitely. The browser reconnects automatically if the connection drops. On server shutdown, the stream exits cleanly within 1 second.
