# Dashboard Guide

The dashboard is the bench where you watch load tests and steer them. It shows every worker
against its rate limit, so you can see throttling as it happens and change a limit without
restarting anything.

Run it with `uv run dashboard`, then open http://localhost:8000.

---

## Layout

From top to bottom:

1. **Readouts.** Four figures for the whole fleet: tokens per minute, throttled calls in the
   last minute (with the share of calls), workers running, and dropped iterations. Dropped
   iterations are offered load that the system could not take in time.
2. **Throughput against each limit.** The one chart that matters. Each worker is a line, the
   fleet total is a neutral line, and each limit is a dashed line. Red ticks along the bottom mark
   throttled calls. Lines are labelled at their ends. Hover for values at any point in the last
   minute.
3. **Workers.** One row per worker, with its status, its rate, the latest step, a 60-second trace,
   and its controls. Shared limits sit above the rows, once each.
4. **Scenarios** (right column). Scenario files from the `scenarios/` folder, each checked when
   the page loads. Launch one to start its workers.
5. **Model calls** (right column). The most recent calls, newest first. Throttled calls are
   marked in red.

---

## Worker rows

| Element | Meaning |
|---|---|
| Status | **Running**, **Stopped**, **Stopping**, **Starting**, **Applying**, **No heartbeat**, **Lost**, or **Error**. Each has its own shape, so colour is never the only signal. **Applying** means a command was sent and the worker has not confirmed it yet. |
| Throttled | Shown while the worker has been throttled in the last minute. |
| Rate | Tokens per minute over the last 60 seconds. For a worker on a shared limit this is its own share. |
| Latest step | The last model call's step, status and latency. |
| Trace | The same 60 seconds as the chart. Red ticks are throttled buckets. |

**Controls**

- **Start / Stop.** Changes whether the worker runs. The status changes to *Applying* until the
  worker confirms the change.
- **Interrupt.** Cuts short the call the worker is making now. The worker keeps running.
- **Mode.** *Naive retry* retries at once. *Paced* obeys the limit. *Ignores limits* sends as
  fast as it can.
- **Budget.** Opens the budget editor in place (see below).
- **Kill process** appears only for workers the dashboard started itself.
- **Forget** removes a stopped or lost worker from the list.

---

## Budgets

Click **Budget** on a row to open the editor. It works on a draft. Nothing changes until you
click **Apply budget**, and **Revert** puts back the saved values.

- **Applies to.** *This worker only* creates an override for this worker. *Everyone in team X* and
  *Everyone (global)* change the shared limit, and the editor says how many other workers that
  reaches before you apply.
- **Tokens per minute** and **headroom**. The effective limit is shown as you type, after headroom.
- **When the limit is reached.** *Pace requests in paced mode* enforces the limit only for paced
  workers, which is what you want for most tests. *Measure only, never delay* records how far over
  the limit a worker would have gone, without slowing it. *No limit* turns the limit off.

If the server refuses a change, the editor says why and keeps your draft.

---

## Live updates

The page keeps one connection open to `/api/stream`. A snapshot arrives every second and replaces
the view as a whole. A snapshot older than the one on screen is ignored. Model calls arrive as they
happen and are merged by sequence number.

The header shows **Live** while updates arrive, **Reconnecting…** while the browser retries, and
**Offline** after five seconds without an update. When the connection comes back, the browser
resumes the model-call log from where it stopped, so nothing is missing or repeated.

---

## Themes

The dashboard follows your system's light or dark setting. Both themes use the same palette
structure, and the chart colours were checked for distinguishability and contrast against each
theme's background.

---

## Data retention

- The model-call log and the 60-second rates come from events kept for 15 minutes.
- `/metrics` counters are cumulative since the dashboard started. They are not cleared by anything
  in the UI.
- Run artifacts (per scenario) are written by the worker under `runs/`, not kept by the dashboard.
