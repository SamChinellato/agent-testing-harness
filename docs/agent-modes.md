# Agent Modes

Every agent runs in one of three modes: `naive`, `paced`, or `runaway`. The mode controls how the agent handles rate limits, retries, and pacing between requests.

You set the starting mode with `AGENT_MODE` in the environment. The mode can also be toggled live from the dashboard without restarting the agent.

---

## naive

The agent makes requests as fast as its cycle allows, with a 1-second sleep between cycles. When it receives a `429 Too Many Requests` response, it retries up to 3 times with a 100ms delay between attempts.

**Token bucket:** None. No client-side throttling.

**On 429:**
1. Waits 100ms
2. Retries immediately (up to 3 attempts total)
3. If all attempts fail, logs the error and moves to the next cycle

**Use when:**
- You want to observe natural throttling behaviour from the server
- Testing what happens when an agent has no awareness of rate limits
- Baseline comparison against paced mode

**Dashboard appearance:** Status badge shows `RUNNING`. When 429s start appearing, the throttle counter increments and the card turns red.

---

## paced

The agent uses a **client-side token bucket** to self-limit its request rate. Before each model call, it checks whether enough tokens have accumulated in the bucket. If not, it waits. This prevents requests from ever reaching the server's hard rate limit under normal conditions.

**Token bucket:** Initialised at startup with `LIMIT_TPM × PACED_TARGET_RATIO` as its target (default: 90% of limit). The bucket refills smoothly at `target_tpm / 60` tokens per second.

**On 429 (if it still occurs):**
1. Reads the `Retry-After` header if present and waits exactly that long
2. Falls back to exponential backoff with jitter: `2^attempt + random(0.1, 1.0)` seconds
3. Retries up to 3 times

**On start/restart:** The token bucket is refilled to capacity immediately so the agent doesn't stall waiting for tokens to accumulate after a stop.

**On limit change:** If you update the TPM limit in the dashboard, the bucket's target is updated on the next control poll (within 1 second). It takes effect from the next request.

**Use when:**
- You want the agent to run sustainably without hitting the hard limit
- Comparing well-behaved vs poorly-behaved agents side by side
- Production-like load testing where you want to stay within budget

**Dashboard appearance:** TPM stays at or below the red reference line. Status badge shows `RUNNING` throughout.

---

## runaway

The agent loops as fast as possible — only a 50ms sleep between cycles, no token bucket, and no retry on 429. The first 429 it receives is treated as terminal for that cycle and it immediately starts the next one.

**Token bucket:** None.

**On 429:** Raises `RateLimitExceeded` immediately. The cycle ends and a new one starts after 50ms. The agent does not back off.

**Use when:**
- Demonstrating what an unrestricted agent does to a shared quota
- Testing whether your gateway correctly throttles a badly-behaved client
- Showing the contrast with a paced agent running on the same subscription

**Dashboard appearance:** The chart spikes sharply, the throttle counter climbs rapidly, and the card turns red with `THROTTLED` badge.

---

## Comparing modes side by side

The most useful test is running two agents on the same API key/subscription simultaneously:

- **Agent A** in `runaway` — consumes the budget aggressively
- **Agent B** in `paced` — self-limits to stay within budget

You should observe:
- Agent A hits 429s and its lane goes red
- Agent B either stays green (if the subscription has per-user isolation) or also gets throttled (if it is a shared pool limit)

This is the core test the harness was built for — understanding exactly where your gateway enforces limits and what happens to well-behaved agents when a bad actor is on the same subscription.

---

## Changing modes at runtime

You can toggle the mode of any running agent from the dashboard without restarting it:

1. Click the mode button on the agent card (shows the current mode: `⚡ Unrestricted`, `✓ Rate-aware`, or `↺ Naive retry`)
2. The agent polls for its new control state within 1 second and switches mode on the next cycle

When switching to `paced` from another mode, a new token bucket is created with a full capacity so the agent starts immediately rather than waiting.

---

## Mode summary table

| Mode | Sleeps between cycles | Token bucket | On 429 | Retries |
|---|---|---|---|---|
| `naive` | 1s | None | 100ms wait, retry | Up to 3 |
| `paced` | 0.5s | Yes — 90% of LIMIT_TPM | Retry-After or exponential backoff | Up to 3 |
| `runaway` | 50ms | None | Fails immediately | None |
