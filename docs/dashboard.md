# Dashboard Guide

The dashboard gives you a live view of every connected agent — their token consumption, rate-limit status, current task step, and controls to start, stop, or change their behaviour.

---

## Layout overview

```
┌─────────────────────────────────────────────────────────┐
│  🐒 MaaS Chaos Monkey — Model Load & Governance Testing  │  Header
├─────────────────────────────────────────────────────────┤
│  Simulation Controls          [Normal Operations] [Runaway Agent] [Reset]  │  Control bar
├─────────────────────────────────────────────────────────┤
│  ● Agent Name                                  [RUNNING] │
│  Team: Banking Ops                                       │  Agent card
│  Task / Behaviour / Step / TPM / Throttled / TPM limit  │  (one per agent)
│  [Stop] [✓ Rate-aware]              [chart]             │
├─────────────────────────────────────────────────────────┤
│  Logs (Last 30 Model Invocations)                        │  Event log
└─────────────────────────────────────────────────────────┘
```

---

## Simulation Controls bar

The control bar at the top lets you set up scenarios across all connected agents at once.

### Normal Operations button

Sets all agents to `paced` mode and `running` state, except the first registered agent which is set to `stopped`. This gives you a clean baseline — multiple paced agents making steady requests without any throttling.

### Runaway Agent button

Sets the first registered agent to `runaway` mode and `running`. All other agents are set to `paced` mode and `running`. This creates the contrast scenario: one bad actor consuming the rate limit aggressively while well-behaved agents continue running.

### Reset button

- Stops all agents (sets `state: stopped`)
- Clears the entire event log and all chart data
- Restores each agent's mode to its registered default (the mode it started with)

Agents remain connected — they are not killed. Click **Start** on any card to resume.

---

## Agent cards

Each connected agent gets its own card. Cards are ordered by registration time (first agent to connect appears first).

### Card header

| Element | What it shows |
|---|---|
| Connectivity dot | Green = agent has sent at least one telemetry event or is marked running. Grey = agent registered but no activity yet. |
| Agent name | The `DISPLAY_NAME` set when the agent started. Falls back to `USER_ID`. |
| Team label | The `TEAM_ID` set when the agent started. |
| Status badge | `IDLE` (grey) — stopped, no activity. `RUNNING` (green) — actively making requests. `THROTTLED` (red) — one or more 429 responses in the last minute. |

The card's left border changes colour to indicate status: blue/green/purple (per agent colour) when running, red when throttled.

### Stats column (left)

| Stat | What it means |
|---|---|
| **Task** | The `TASK_DESCRIPTION` set at startup, or the default for the agent type. |
| **Behaviour** | Current mode: `Unrestricted` (runaway), `Rate-aware` (paced), `Naive retry` (naive). |
| **Current step** | The `node_name` of the most recent model call — e.g. `classify`, `worker_query`, `chat_turn`. Updates every second. |
| **Tokens/min (1m)** | Rolling count of tokens consumed in the last 60 seconds. Green when running normally, red when throttled. |
| **Throttled requests (1m)** | Number of `429` responses received in the last 60 seconds. Red when non-zero. |
| **TPM limit** | The token-per-minute limit currently in effect for this agent. Editable — see below. |

### TPM limit control

The `−` and `+` buttons adjust the agent's TPM limit by 50 tokens at a time. You can also type a value directly into the field. The new value takes effect on the agent's next control poll (within 1 second) and the red reference line on the chart updates immediately.

This is the client-side limit used by the paced token bucket. It does not change any server-side subscription limit — for that, you need to update your gateway configuration directly.

### Start / Stop button

- **Start** — sends `state: running` to the agent. The agent picks this up within 1 second and begins its next cycle.
- **Stop** — sends `state: stopped`. The agent finishes its current model call and stops. It does not kill the agent process.

### Mode toggle button

Cycles through the three modes: `⚡ Unrestricted` → `✓ Rate-aware` → `↺ Naive retry` → back to `⚡ Unrestricted`.

The agent switches mode on the next control poll. When switching to `paced`, the token bucket starts full so there is no delay.

### Token chart (right)

A 60-second rolling line chart showing tokens consumed in 5-second buckets. The red dashed line marks the current TPM limit.

- If the line stays below the red dashed line: the agent is within budget
- If the line crosses the red dashed line: the agent has exceeded the limit in that window
- If you see a flat line at zero with a `THROTTLED` badge: the agent is being rejected by the server before tokens are counted

---

## Event log

The bottom panel shows the last 30 model invocations across all agents, newest first.

| Column | What it shows |
|---|---|
| Timestamp | Local time of the model call (HH:MM:SS) |
| Agent | Display name of the agent that made the call |
| Task / Behaviour | The agent type and current mode at time of call |
| Step | The graph node name — e.g. `classify`, `planner`, `chat_turn` |
| Status | HTTP status code. `200` = success (grey badge). `429` = throttled (red badge). |
| Prompt / Output Tokens | Actual token counts reported by the model. `0 in / 0 out` on a 429 means no tokens were consumed. |
| Latency | Round-trip time in milliseconds from request sent to response received |
| Attempt | Which retry attempt this was. `#1` = first try. `#2` or higher = the request was retried after a previous failure. |

Rows with a `429` status are highlighted red. This makes it easy to see at a glance when and how often throttling is happening.

---

## "No agents connected" state

If no agents have registered, the dashboard shows a single card with "No agents connected". Start one or more agents (see [Getting Started](getting-started.md)) and their cards will appear automatically within a second of their startup.

---

## Dashboard data retention

The dashboard holds events in memory only. Data is lost when the dashboard restarts. The rolling window is 15 minutes — events older than 15 minutes are pruned. The chart shows the last 60 seconds. The log table shows the last 30 events.

Clicking **Reset** clears the event buffer immediately.
