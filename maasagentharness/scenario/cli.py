"""CLI entry point for Phase 3 scenario runs.

    uv run harness run scenario.yaml                  # runs every scenario in the file, concurrently
    uv run harness run scenario.yaml --dry-run         # validates + prints the resolved plan, no calls
    uv run harness run scenario.yaml --scenario NAME   # runs just one named scenario — this is what
                                                        # a dashboard-spawned managed worker process
                                                        # uses (see control/api.py's POST /api/runs),
                                                        # so each named scenario is its own independently
                                                        # manageable/killable worker.
"""
import argparse
import asyncio
import os
import sys
from typing import List, Optional

import httpx

from maasagentharness.config import settings
from maasagentharness.scenario.loader import load_run_config
from maasagentharness.scenario.schema import RunConfig, ScenarioConfigError


def _print_plan(run: RunConfig) -> None:
    print(f"Run: {run.run}")
    if run.endpoint.base_url or run.endpoint.model:
        print(f"  endpoint: base_url={run.endpoint.base_url!r} model={run.endpoint.model!r}")
    for b in run.budgets:
        print(
            f"  budget '{b.budget_id}': scope={b.scope} scope_ref={b.scope_ref} "
            f"tokens_per_minute={b.tokens_per_minute} enforcement={b.enforcement} headroom={b.headroom}"
        )
    for name, sc in run.scenarios.items():
        ex = sc.executor
        stages = [(s.target, s.duration) for s in ex.stages] if ex.stages else None
        print(
            f"  scenario '{name}': agent={sc.agent} mode={sc.mode} executor={ex.kind} "
            f"rate={ex.rate} vus={ex.vus} max_vus={ex.max_vus} duration={ex.duration} "
            f"stages={stages} budget={sc.budget or '<resolved: worker/team/global>'} "
            f"graceful_stop={sc.graceful_stop} start_time={sc.start_time}"
        )
    for t in run.thresholds:
        try:
            parsed = run.parsed_thresholds
        except ScenarioConfigError:
            parsed = None
        print(f"  threshold: {t}" + ("" if parsed else "  [UNPARSEABLE]"))


async def _upsert_budgets(dashboard_url: str, run: RunConfig) -> None:
    """Inline budgets make a scenario file self-contained and reproducible
    — they don't need to already exist in the store before this run.
    """
    if not run.budgets:
        return
    async with httpx.AsyncClient(timeout=5.0) as client:
        for b in run.budgets:
            try:
                await client.put(
                    f"{dashboard_url.rstrip('/')}/api/budgets/{b.budget_id}",
                    json={
                        "scope": b.scope,
                        "scope_ref": b.scope_ref,
                        "tokens_per_minute": b.tokens_per_minute,
                        "requests_per_minute": b.requests_per_minute,
                        "max_concurrency": b.max_concurrency,
                        "headroom": b.headroom,
                        "enforcement": b.enforcement,
                    },
                )
            except Exception as e:
                print(f"[harness] Warning: could not upsert budget '{b.budget_id}': {e}", file=sys.stderr, flush=True)


async def _run(run: RunConfig, only: Optional[str], dashboard_url: str) -> int:
    # Local import — avoids a module-level import cycle (runner.py doesn't
    # need the CLI, but importing it eagerly at module load time would
    # still work; done lazily anyway to keep `--dry-run`'s import graph
    # minimal and fast).
    from maasagentharness.runner import AgentWorker

    if run.endpoint.base_url:
        settings.maas_endpoint = run.endpoint.base_url
    if run.endpoint.model:
        settings.model_name = run.endpoint.model

    await _upsert_budgets(dashboard_url, run)

    names: List[str] = [only] if only else list(run.scenarios.keys())
    unknown = [n for n in names if n not in run.scenarios]
    if unknown:
        print(f"[harness] Unknown scenario name(s): {', '.join(unknown)}", file=sys.stderr, flush=True)
        return 2

    async def run_one(name: str):
        scenario = run.scenarios[name]
        worker_id = f"{run.run}-{name}"
        worker = AgentWorker(user_id=worker_id, team_id=run.run, dashboard_url=dashboard_url)
        stats = await worker.run_scenario(scenario, run_name=run.run, scenario_name=name)
        return name, stats

    results = await asyncio.gather(*(run_one(n) for n in names))

    exit_code = 0
    for name, stats in results:
        print(
            f"[harness] '{name}': {stats.iterations_completed}/{stats.iterations_started} completed, "
            f"{stats.iterations_dropped} dropped, {stats.duration_s:.1f}s",
            flush=True,
        )
        if stats.iterations_dropped > 0:
            exit_code = 1  # non-zero on any drops — useful for CI gating
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness", description="Run a declarative load-test scenario file.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="Run a scenario file")
    run_parser.add_argument("scenario_path")
    run_parser.add_argument("--dry-run", action="store_true", help="Validate and print the resolved plan without making any calls")
    run_parser.add_argument("--scenario", default=None, help="Run only this one named scenario from the file")
    run_parser.add_argument("--dashboard-url", default=None, help="Defaults to $DASHBOARD_URL / settings.dashboard_url")

    args = parser.parse_args()

    try:
        run_config = load_run_config(args.scenario_path)
    except ScenarioConfigError as e:
        print(f"[harness] {e}", file=sys.stderr, flush=True)
        sys.exit(2)

    if args.dry_run:
        _print_plan(run_config)
        return

    dashboard_url = args.dashboard_url or os.environ.get("DASHBOARD_URL") or settings.dashboard_url
    only = args.scenario or os.environ.get("SCENARIO_NAME")
    exit_code = asyncio.run(_run(run_config, only, dashboard_url))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
