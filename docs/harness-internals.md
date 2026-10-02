# How the Harness Works

This document covers the internal components of the harness: `ModelHarness`, `TokenBucket`, `TelemetryEmitter`, and how they fit together with `AgentWorker`.

---

## Component overview

```
┌─────────────────────────────────────────────────────────────┐
│  AgentWorker (runner.py)                                     │
│                                                              │
│  • Polls /control/{user_id} every second                    │
│  • Holds a persistent ModelHarness (survives cycle restarts) │
│  • Calls agent.run() each cycle                             │
│  • Respects start/stop from dashboard                       │
└──────────────────────┬──────────────────────────────────────┘
                       │ passes harness to
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  ModelHarness (harness.py)                                   │
│                                                              │
│  • invoke_with_telemetry() — blocking call                  │
│  • stream_with_telemetry() — streaming call                 │
│  • Calls TokenBucket.acquire() before each request (paced)  │
│  • Calls TelemetryEmitter.emit() after each request         │
└──────────────┬─────────────────────────┬────────────────────┘
               │                         │
               ▼                         ▼
┌──────────────────────┐   ┌─────────────────────────────────┐
│  TokenBucket         │   │  TelemetryEmitter               │
│  (pacer.py)          │   │  (telemetry.py)                 │
│                      │   │                                 │
│  Smooth token refill │   │  stdout JSON + POST /events     │
│  Sleeps if empty     │   │  Calls registered listeners     │
└──────────────────────┘   └─────────────────────────────────┘
```

---

## ModelHarness

`maasagentharness/harness.py` — the central object every agent uses to make model calls.

### Construction

```python
harness = ModelHarness(
    user_id="agent-1",         # used in telemetry and control polling
    team_id="my-team",         # used in telemetry
    agent_name="my_agent",     # used in telemetry (the agent type label)
    mode="paced",              # naive | paced | runaway
)
```

In `paced` mode, construction automatically creates a `TokenBucket` with `LIMIT_TPM × PACED_TARGET_RATIO` as its target.

The harness is created **once per agent run** in `AgentWorker.run_loop()` and reused across cycles. It is only recreated when the mode or agent type changes from the dashboard.

### `invoke_with_telemetry()`

Non-streaming call. Flow:

1. If `paced`, calls `TokenBucket.acquire(estimated_input_tokens)` — may sleep
2. Sends `POST /v1/chat/completions` via httpx with a 60s timeout
3. On success: extracts `usage.prompt_tokens` and `usage.completion_tokens` from the response
4. On 429: raises `RateLimitExceeded` after max_attempts (1 in runaway, 3 in naive/paced)
5. Emits a `TelemetryEvent` with actual token counts, latency, status, and rate-limit headers
6. Returns the response content string

**Parameters:**

| Parameter | Type | Description |
|---|---|---|
| `messages` | `List[BaseMessage]` | LangChain message list (SystemMessage, HumanMessage, AIMessage) |
| `node_name` | `str` | Label for this call — shown in dashboard Step column |
| `run_id` | `str` | Groups calls from one cycle — use same value per cycle |
| `estimated_input_tokens` | `int` | Used by paced bucket pre-deduction. Default: 500 |
| `max_attempts` | `int \| None` | Override retry count. Default: 1 (runaway) or 3 (naive/paced) |

### `stream_with_telemetry()`

Streaming call. Same flow as above but uses `client.stream()` and yields content tokens as they arrive. Telemetry is emitted once at the end of the stream with the total token counts (from the final `usage` chunk, or estimated from content length if not provided).

**Returns:** `AsyncIterator[str]` — individual content tokens.

### Rate-limit header extraction

After every response (success or failure), the harness extracts all headers containing `ratelimit`, `retry-after`, or `limit` (case-insensitive) and includes them in the telemetry event. This makes it easy to inspect exactly what the server is telling the client about its quota.

---

## TokenBucket

`maasagentharness/pacer.py` — a smooth async token bucket rate limiter.

### How it works

The bucket has a capacity equal to `target_tpm`. It refills at `target_tpm / 60` tokens per second (smooth continuous refill, not per-minute burst). Before each paced request, the agent calls `acquire(estimated_tokens)`:

1. Lock the bucket
2. Refill based on time elapsed since last update
3. If enough tokens are available: deduct and return immediately
4. If not: calculate how long to wait, release the lock, sleep up to 2s, then retry

The lock prevents multiple concurrent coroutines (e.g. fan-out workers) from simultaneously over-drawing the bucket.

### Key properties

```python
bucket.capacity      # maximum token capacity (= target_tpm)
bucket.tokens        # current available tokens
bucket.refill_rate   # tokens per second = target_tpm / 60
```

### `update_target(target_tpm)`

Updates the capacity and refill rate without resetting the current token level. Called every second by `AgentWorker` when the dashboard TPM limit changes.

### Bucket reset on start

When an agent transitions from stopped → running, `AgentWorker` sets `harness.pacer.tokens = harness.pacer.capacity` to refill the bucket to full. Without this, the agent would have to wait up to 60 seconds for the bucket to refill naturally before making its first post-stop request.

---

## TelemetryEmitter

`maasagentharness/telemetry.py` — emits a structured event after every model call.

### What it emits

A `TelemetryEvent` contains:

| Field | Description |
|---|---|
| `ts` | ISO 8601 local timestamp |
| `user` | `user_id` from the harness |
| `team` | `team_id` from the harness |
| `agent` | `agent_name` from the harness |
| `mode` | Current mode at time of call |
| `run_id` | The run_id passed by the agent |
| `node` | The `node_name` passed by the agent |
| `status` | HTTP status code (200, 429, 500) |
| `prompt_tokens` | Actual prompt tokens from response usage |
| `completion_tokens` | Actual completion tokens from response usage |
| `latency_ms` | Round-trip time in milliseconds |
| `ratelimit_headers` | All rate-limit related response headers |
| `attempt` | Which retry attempt (1 = first try) |

### Emission flow

1. Prints JSON to stdout: `[TELEMETRY] {...}`
2. POSTs the event to `{DASHBOARD_URL}/events` (non-blocking — failures are silently ignored)
3. Calls all registered listeners with the event

### Listeners

Any component can register a callback to be called after every emit:

```python
from maasagentharness.telemetry import telemetry

async def my_callback(event: TelemetryEvent):
    print(f"Got event: {event.status} {event.prompt_tokens}pt")

telemetry.add_listener(my_callback)
```

Listener failures are silently caught and do not affect the main emit flow.

---

## AgentWorker

`maasagentharness/runner.py` — the main loop that drives an agent process.

### Startup sequence

1. Reads all configuration from `Settings` (`.env` / environment variables)
2. Calls `POST /register` on the dashboard with identity and metadata
3. Creates a persistent `ModelHarness` with the starting mode
4. Enters the main loop

### Main loop

Each iteration:

1. `poll_control()` — GET `/control/{user_id}`, update `control_state`
2. If `state != "running"`: sleep 1s, continue
3. On first `running` tick after a stopped period: refill token bucket
4. If `agent_type` or `mode` changed: recreate harness
5. If harness has a pacer: update its target from current `limit_tpm`
6. Run one agent cycle
7. `poll_control()` again immediately after the cycle (fast stop detection)
8. Sleep: 50ms (runaway), 1s (naive), 0.5s (paced)

### Stop responsiveness

The agent checks `control_state.state` at the start and end of every cycle. For long cycles (e.g. research with 6–8 sequential workers), the `stop_check` callback is passed to the agent so it can abort between worker calls. The worst-case stop latency is one LLM call duration (typically 1–5 seconds).

### Harness persistence

The harness (and its token bucket) is created once and reused across cycles. This is critical for paced mode — the bucket must persist to accurately track cumulative token consumption across multiple cycles. If the bucket were recreated each cycle, it would start full every time and provide no effective throttling.

---

## Control flow: dashboard ↔ agent

```
Dashboard (FastAPI)          Agent (AgentWorker)
─────────────────────        ──────────────────────────────
POST /register          ←    on startup
GET  /control/{id}      ←    every ~1 second
POST /control/{id}      →    when user clicks Start/Stop/mode/limit
POST /events            ←    after every model call
GET  /events/stream     →    browser SSE subscription (not agent)
```

The agent is entirely pull-based for control: it polls for its desired state rather than receiving pushed commands. This means:

- If the dashboard restarts, agents keep running with their last known state
- If an agent restarts, it re-registers and resumes from `stopped` state
- There is no persistent state — everything is in-memory on the dashboard process
