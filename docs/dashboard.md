# Dashboard Guide

The dashboard is the bench where you watch load tests and steer them. It shows every worker
against its rate limit, so you can see throttling as it happens and change a limit without
restarting anything.

Run it with `uv run dashboard`, then open http://localhost:8000.

---

## Layout

- **Top.** The name, a connection label, and **Stop all**.
- **Readouts.** Tokens per minute, throttled calls, running workers, dropped iterations.
- **Trace.** Each worker as a line, the fleet total, and each limit as a dashed line. Red ticks mark
  throttled calls. Hover for values.
- **Workers.** One row each: name, status, rate, a 60-second trace, **Start/Stop**, and a menu (⋮).
- **Right side.** Scenario files to launch, and the most recent calls.

## Worker rows

The menu (⋮) holds the rest: **Limit**, the two behaviours (**Obeys limit** or **Ignores limit**),
**Interrupt**, **Kill process** (only for processes the dashboard started) and **Forget** (only for
stopped or lost workers).

**Limit** opens a small form:

- **Tokens per minute.**
- **Applies to.** This worker, the worker's team, or everyone.
- **Measure only.** Records the overage without slowing agents down. Off means the limit is enforced.

**Save** applies the change. **Revert** puts back the saved values. If the server refuses, the reason
appears under the row and your values stay in the form.

Status shows as a label: **Running**, **Stopped**, **Applying** (a change has been sent and not yet
confirmed), **Lost** and similar. Colour is never the only signal.

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
