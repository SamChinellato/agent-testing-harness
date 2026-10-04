"""Populate a running dashboard with a believable fleet and live traffic.

Useful for looking at the UI without a model endpoint:

    uv run dashboard                       # terminal 1
    uv run python scripts/demo_fleet.py    # terminal 2 (Ctrl-C to stop)

It registers four workers in one team, sets a shared team budget, and then
posts synthetic model-call telemetry for each one. The runaway worker ignores
the limit and gets throttled. The paced workers stay under it.
"""
import argparse
import random
import sys
import time
from datetime import datetime

import httpx

WORKERS = [
    # worker_id, display name, agent, mode, calls per second, throttle share
    ("demo-triage", "Dispute triage", "triage", "paced", 0.10, 0.00),
    ("demo-research", "Policy research", "research", "paced", 0.08, 0.00),
    ("demo-assistant", "Customer assistant", "assistant", "naive", 0.10, 0.12),
    ("demo-runaway", "Runaway load", "research", "runaway", 0.55, 0.42),
]
TEAM = "banking-ops"


def setup(client: httpx.Client, base: str) -> None:
    for wid, name, agent, mode, _, _ in WORKERS:
        client.post(f"{base}/api/workers", json={
            "worker_id": wid, "team": TEAM, "display_name": name,
            "task_description": f"{agent} traffic", "agent": agent, "mode": mode,
        }).raise_for_status()
        client.patch(f"{base}/api/workers/{wid}/desired", json={"state": "running"}).raise_for_status()
    client.put(f"{base}/api/budgets/team:{TEAM}", json={
        "scope": "team", "scope_ref": TEAM, "tokens_per_minute": 30000,
        "headroom": 0.9, "enforcement": "client_bucket",
    }).raise_for_status()


def event(wid: str, agent: str, mode: str, throttled: bool) -> dict:
    node = {"triage": "classify", "research": "planner", "assistant": "chat_turn"}[agent]
    if throttled:
        status, prompt, completion, latency = 429, 0, 0, random.randint(40, 120)
    else:
        status = 200
        prompt = random.randint(480, 820)
        completion = random.randint(18, 60)
        latency = random.randint(600, 1900)
    return {
        "ts": datetime.now().isoformat(), "user": wid, "team": TEAM, "agent": agent,
        "mode": mode, "run_id": f"demo-{random.randint(1000, 9999)}", "node": node,
        "status": status, "prompt_tokens": prompt, "completion_tokens": completion,
        "latency_ms": latency, "attempt": 1,
    }


def heartbeat(client: httpx.Client, base: str) -> None:
    """Real workers heartbeat every second. Without this the fleet reads as lost."""
    for wid, *_ in WORKERS:
        desired = client.get(f"{base}/api/workers/{wid}/desired").json()
        client.post(f"{base}/api/workers/{wid}/heartbeat", json={
            "applied_generation": desired["generation"],
            "phase": "running" if desired["state"] == "running" else "stopped",
            "current_node": None, "iteration": 0, "last_error": None,
            "applied_interrupt_token": desired["interrupt_token"],
        })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--seconds", type=float, default=0, help="0 runs until interrupted")
    args = parser.parse_args()

    with httpx.Client(timeout=5.0) as client:
        try:
            setup(client, args.base)
        except httpx.HTTPError as e:
            sys.exit(f"Couldn't reach the dashboard at {args.base}: {e}")
        print(f"Fleet registered at {args.base}. Posting traffic…", flush=True)

        started = time.monotonic()
        last = started
        next_beat = 0.0
        while args.seconds == 0 or time.monotonic() - started < args.seconds:
            now = time.monotonic()
            dt = now - last
            last = now
            if now >= next_beat:
                heartbeat(client, args.base)
                next_beat = now + 1.0
            for wid, _, agent, mode, rate, throttle_share in WORKERS:
                calls = rate * dt
                n = int(calls) + (1 if random.random() < calls - int(calls) else 0)
                for _ in range(n):
                    throttled = random.random() < throttle_share
                    client.post(f"{args.base}/events", json=event(wid, agent, mode, throttled))
            time.sleep(0.2)


if __name__ == "__main__":
    main()
