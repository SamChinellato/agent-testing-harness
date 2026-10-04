import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from maasagentharness.config import settings
from maasagentharness.control.budgets import Budget
from maasagentharness.control.client import ControlClient
from maasagentharness.control.models import DesiredStateResponse, Phase
from maasagentharness.harness import ModelHarness, WorkerInterrupted, WorkerStopped
from maasagentharness.metrics.artifact import RunArtifact
from maasagentharness.scenario.executors.base import ExecutorStats
from maasagentharness.scenario.executors.factory import build_executor
from maasagentharness.scenario.schema import ScenarioConfig
from maasagentharness.telemetry import TelemetryEvent, telemetry
from maasagentharness.agents.research import ResearchAgent
from maasagentharness.agents.triage import TriageAgent
from maasagentharness.agents.assistant import AssistantAgent

DATA_DIR = Path(__file__).parent / "data"

SAMPLE_RESEARCH_QUERIES = [
    "What is our complete hardship and mortgage repayment moratorium policy under sudden income disruption?",
    "How do we handle unarranged overdraft fees and direct debit dishonours during financial distress?",
    "What are the investigation procedures and customer liability protections for SIM-swap and APP scams?",
    "What is the statutory chargeback timeframe for airline and hotel booking cancellations?",
]

SAMPLE_CHAT_QUESTIONS = [
    "Can you explain the difference between tier-1 and tier-2 dispute thresholds?",
    "What happens if the merchant doesn't respond within 14 days?",
    "Does this apply to small business accounts as well?",
    "What if the transaction was done via contactless chip-and-pin?",
    "Are late fees waived while the case is in moratorium?",
    "Who makes the final decision on internal dispute resolution appeals?",
]


class AgentWorker:
    """Phase 1: talks to the v2 control API (`/api/workers/...`) instead of
    the legacy `/register` + `/control/{id}` pair. The stop/interrupt
    mechanism from Phase 0 (docs/phase-0-hotfixes.md #0.3/#0.4) is
    unchanged — `stop_event` is still threaded through every ModelHarness
    and checked before every call/wait/backoff sleep. What's new here is:

    - a `generation`-aware poll loop, so "pending" is a real, derivable
      concept rather than something the old /control/{id} GET/POST pair
      had no way to express;
    - heartbeats, so the dashboard's SQLite store has an up-to-date,
      *persistent* picture of this worker's observed state — surviving a
      dashboard restart, which Phase 0 could not fix on its own
      (redesign-spec.md finding #6);
    - a second, single-shot `interrupt_event`, distinct from `stop_event`,
      wired the same way into ModelHarness — see WorkerInterrupted's
      docstring in harness.py for exactly what it does and doesn't cover.
    """

    def __init__(
        self,
        user_id: str = settings.user_id,
        team_id: str = settings.team_id,
        dashboard_url: str = settings.dashboard_url,
        control_transport=None,
    ):
        self.user_id = user_id
        self.team_id = team_id
        self.dashboard_url = dashboard_url

        self.control = ControlClient(
            dashboard_url=dashboard_url,
            worker_id=user_id,
            team_id=team_id,
            display_name=self.display_name,
            task_description=self.task_description,
            transport=control_transport,
        )

        self.desired = DesiredStateResponse(unregistered=True)
        self.stop_event = asyncio.Event()
        self.stop_event.set()  # start stopped
        self.interrupt_event = asyncio.Event()
        self._last_seen_interrupt_token = 0

        self.phase = Phase.STOPPED
        self.applied_generation = 0
        self.current_node: Optional[str] = None
        self.iteration = 0
        self.last_error: Optional[str] = None
        # Set in run_loop; `_poll_and_heartbeat_loop` keeps the harness's
        # pacer in sync with the resolved Budget (control/budgets.py) on
        # every poll tick via `self.harness.update_budget(...)`.
        self.harness: Optional[ModelHarness] = None
        self.budget: Optional[Budget] = None

        # Per-cycle state, shared between `run_loop`'s fixed cadence and
        # `run_scenario`'s executor-driven `iteration_fn` — see
        # `_run_one_cycle`, the single place both call into.
        self._step_idx = 0
        self._assistant_turn = 0
        self._assistant_instance: Optional[AssistantAgent] = None

        self._poll_task: Optional[asyncio.Task] = None
        self._artifact: Optional[RunArtifact] = None
        self._live_executor = None
        self._load_corpus()

    @property
    def display_name(self) -> str:
        if settings.display_name:
            return settings.display_name
        return self.user_id

    @property
    def task_description(self) -> str:
        if settings.task_description:
            return settings.task_description
        defaults = {"triage": "Processing dispute letters", "assistant": "Customer Q&A (multi-turn)", "research": "Policy research (fan-out)"}
        return defaults.get(settings.agent_type, settings.agent_type)

    def _load_corpus(self):
        letters_index_path = DATA_DIR / "letters_index.json"
        if letters_index_path.exists():
            self.letters = json.loads(letters_index_path.read_text(encoding="utf-8"))
        else:
            self.letters = []

        policies_index_path = DATA_DIR / "policies_index.json"
        if policies_index_path.exists():
            self.policies = json.loads(policies_index_path.read_text(encoding="utf-8"))
        else:
            self.policies = []

    def _is_running(self) -> bool:
        return self.desired.state == "running" and not self.desired.unregistered

    async def _on_telemetry_event(self, event: TelemetryEvent) -> None:
        """Registered as a telemetry listener — this is how `current_node`/
        `iteration`/`last_error` get populated for the heartbeat without
        threading extra bookkeeping through every agent call site.
        """
        self.current_node = event.node
        self.iteration += 1
        self.last_error = None if event.status == 200 else f"{event.node}: status {event.status}"

    def _sync_from_desired(self, desired: DesiredStateResponse) -> None:
        """Called every poll tick. This is the one place desired state is
        translated into stop_event/interrupt_event/phase — see
        docs/phase-1-5-implementation-plan.md §1.4.
        """
        self.desired = desired
        self.applied_generation = desired.generation

        running = self._is_running()
        if running:
            if self.stop_event.is_set():
                self.stop_event.clear()
            if self.phase in (Phase.STOPPED, Phase.STARTING):
                self.phase = Phase.RUNNING
        else:
            if not self.stop_event.is_set():
                # Just transitioned running -> stopped. If a cycle is
                # currently in flight, it's draining; the main loop will
                # flip this to STOPPED once that cycle actually exits.
                if self.phase == Phase.RUNNING:
                    self.phase = Phase.DRAINING
                self.stop_event.set()
            elif self.phase == Phase.RUNNING:
                self.phase = Phase.STOPPED

        if desired.interrupt_token != self._last_seen_interrupt_token:
            self._last_seen_interrupt_token = desired.interrupt_token
            self.interrupt_event.set()

    async def _poll_and_heartbeat_loop(self) -> None:
        """Runs concurrently with the main cycle loop — this is what lets a
        Stop or Interrupt command take effect mid-cycle instead of only
        being noticed once the current cycle finishes (Phase 0 fix
        #0.3/#0.4, now talking to the v2 API).
        """
        while True:
            desired = await self.control.get_desired()
            self._sync_from_desired(desired)

            # Phase 2: fetch the full resolved Budget (not just the
            # tokens_per_minute `desired` already carries) so enforcement
            # mode (off/client_bucket/shadow) stays current too, not only
            # the pacer's numeric target.
            self.budget = await self.control.get_resolved_budget()
            if self.harness is not None:
                self.harness.update_budget(self.budget)

            await self.control.heartbeat(
                applied_generation=self.applied_generation,
                phase=self.phase.value,
                current_node=self.current_node,
                iteration=self.iteration,
                last_error=self.last_error,
                applied_interrupt_token=self._last_seen_interrupt_token,
                dropped_iterations_total=self._dropped_total(),
            )
            await asyncio.sleep(1.0)

    async def _run_one_cycle(self, agent_type: str, mode: str) -> None:
        """Runs exactly one triage/research/assistant cycle using
        `self.harness`. Shared by `run_loop` (the dashboard-driven fixed
        cadence from Phase 0-2) and `run_scenario`'s executor-driven
        `iteration_fn` (Phase 3) — see
        docs/phase-1-5-implementation-plan.md §3.4: "iteration_fn becomes
        a closure running exactly one cycle, reusing the harness
        unchanged." Raises WorkerStopped/WorkerInterrupted straight
        through; callers decide what that means for their own loop.
        """
        if agent_type == "triage":
            triage = TriageAgent(harness=self.harness)
            letter_item = self.letters[self._step_idx % len(self.letters)] if self.letters else {"id": "LET-001"}
            letter_path = DATA_DIR / "letters" / f"letter_{int(letter_item['id'].replace('LET-', '')):04d}.txt"
            letter_text = letter_path.read_text(encoding="utf-8") if letter_path.exists() else "Dispute sample text"
            print(f"[{self.user_id}][TRIAGE] Processing {letter_item['id']} in {mode} mode...", flush=True)
            await triage.run(letter_text=letter_text, letter_id=letter_item["id"])
            self._step_idx += 1

        elif agent_type == "research":
            q = SAMPLE_RESEARCH_QUERIES[self._step_idx % len(SAMPLE_RESEARCH_QUERIES)]
            print(f"[{self.user_id}][RESEARCH] '{q[:60]}...' in {mode} mode", flush=True)
            research = ResearchAgent(harness=self.harness)
            await research.run(query=q)
            self._step_idx += 1

        elif agent_type == "assistant":
            if self._assistant_instance is None or self._assistant_turn >= 15:
                self._assistant_instance = AssistantAgent(harness=self.harness)
                self._assistant_turn = 0
            q = SAMPLE_CHAT_QUESTIONS[self._assistant_turn % len(SAMPLE_CHAT_QUESTIONS)]
            print(f"[{self.user_id}][ASSISTANT] Turn {self._assistant_turn + 1}: '{q}' in {mode} mode", flush=True)
            async for _chunk in self._assistant_instance.stream_turn(q):
                pass
            self._assistant_turn += 1

    async def run_loop(self):
        """The dashboard-driven fixed cadence from Phase 0-2: polls desired
        state, runs one cycle at a time, sleeps a fixed per-mode interval
        between cycles. Unchanged by Phase 3 — `run_scenario` below is
        additive, not a replacement, so the existing Start/Stop dashboard
        flow keeps working exactly as before. See
        docs/phase-1-5-implementation-plan.md §3.7 for why.
        """
        print(f"[WORKER] Starting agent worker for user '{self.user_id}' (team '{self.team_id}')...", flush=True)
        self.phase = Phase.STARTING
        await self.control.register(agent=settings.agent_type, mode=settings.agent_mode, managed=False)
        telemetry.add_listener(self._on_telemetry_event)
        self._poll_task = asyncio.create_task(self._poll_and_heartbeat_loop())

        current_agent_type = settings.agent_type
        current_mode = settings.agent_mode
        self.harness = ModelHarness(
            user_id=self.user_id,
            team_id=self.team_id,
            agent_name=current_agent_type,
            mode=current_mode,
            stop_event=self.stop_event,
            interrupt_event=self.interrupt_event,
            budget=self.budget,
        )

        was_running = False

        try:
            while True:
                is_running = self._is_running()

                if not is_running:
                    if self.phase != Phase.STOPPED:
                        self.phase = Phase.STOPPED
                    was_running = False
                    await asyncio.sleep(1.0)
                    continue

                if not was_running:
                    if self.harness.pacer is not None:
                        self.harness.pacer.tokens = self.harness.pacer.capacity
                    was_running = True
                    self.phase = Phase.RUNNING

                agent_type = self.desired.agent
                mode = self.desired.mode

                if agent_type != current_agent_type or mode != current_mode:
                    current_agent_type = agent_type
                    current_mode = mode
                    self.harness = ModelHarness(
                        user_id=self.user_id,
                        team_id=self.team_id,
                        agent_name=agent_type,
                        mode=mode,
                        stop_event=self.stop_event,
                        interrupt_event=self.interrupt_event,
                        budget=self.budget,
                    )
                    self._assistant_instance = None

                try:
                    await self._run_one_cycle(agent_type, mode)
                except WorkerStopped:
                    # Expected — a Stop command interrupted this cycle partway
                    # through. Don't log it as an error.
                    print(f"[{self.user_id}] Stopped mid-cycle.", flush=True)
                    self.phase = Phase.STOPPED
                except WorkerInterrupted:
                    # An Interrupt aborted the current call. The cycle is
                    # abandoned, but — unlike Stop — the worker keeps
                    # running and starts its next cycle normally.
                    print(f"[{self.user_id}] Interrupted mid-call — continuing.", flush=True)
                except Exception as e:
                    print(f"[{self.user_id}] Cycle exception: {e}", flush=True)

                if mode == "runaway":
                    await asyncio.sleep(0.05)
                elif mode == "naive":
                    await asyncio.sleep(1.0)
                else:
                    await asyncio.sleep(0.5)
        finally:
            if self._poll_task is not None:
                self._poll_task.cancel()
            if self.harness is not None:
                await self.harness.aclose()

    def _dropped_total(self) -> int:
        """Cumulative dropped iterations from the live executor, if one is running."""
        stats = getattr(self._live_executor, "live_stats", None)
        return stats.iterations_dropped if stats is not None else 0

    async def _on_artifact_event(self, event: TelemetryEvent) -> None:
        if self._artifact is not None:
            self._artifact.record(event.model_dump())

    async def _flush_artifact_loop(self) -> None:
        """Rewrites summary.json every couple of seconds while the run is
        live, so a crashed or killed run still leaves a recent summary."""
        while True:
            await asyncio.sleep(2.0)
            if self._artifact is not None:
                stats = getattr(self._live_executor, "live_stats", None)
                self._artifact.write_summary(stats.model_dump() if stats else None, finished=False)

    async def run_scenario(
        self,
        scenario: ScenarioConfig,
        run_name: str = "adhoc",
        scenario_name: Optional[str] = None,
        runs_root: Optional[Path] = None,
    ) -> ExecutorStats:
        """Phase 3: runs a declarative scenario — calibrated load generation
        via one of the executors in scenario/executors/, instead of
        `run_loop`'s fixed one-at-a-time cadence. Still registers,
        heartbeats, and responds to Stop/Interrupt via the same
        stop_event/interrupt_event `run_loop` uses — a scenario run shows
        up in the dashboard as an ordinary worker lane and can still be
        stopped from it, it just self-drives for its configured load shape
        instead of waiting on a dashboard Start click. See
        docs/phase-1-5-implementation-plan.md §3.4.

        Phase 4: also writes a run artifact (runs/<run>/<scenario>/<ts>/)
        containing the scenario, every model call as JSONL, and a summary
        rewritten periodically — see metrics/artifact.py.
        """
        scenario_name = scenario_name or self.user_id
        runs_root = runs_root or Path(os.getenv("HARNESS_RUNS_DIR", "runs"))

        print(f"[WORKER] Starting scenario run for user '{self.user_id}' (agent={scenario.agent}, "
              f"mode={scenario.mode}, executor={scenario.executor.kind})...", flush=True)
        self.phase = Phase.STARTING
        await self.control.register(agent=scenario.agent, mode=scenario.mode, managed=False)
        telemetry.add_listener(self._on_telemetry_event)
        self._artifact = RunArtifact.create(
            root=runs_root,
            run_name=run_name,
            scenario_name=scenario_name,
            scenario_config=scenario.model_dump(mode="json"),
            worker_id=self.user_id,
        )
        telemetry.add_listener(self._on_artifact_event)
        self._poll_task = asyncio.create_task(self._poll_and_heartbeat_loop())
        flush_task = asyncio.create_task(self._flush_artifact_loop())

        # A scenario names its budget explicitly, bypassing team/global
        # fallback — resolve_budget's precedence is for dashboard-driven
        # workers whose budget isn't pinned by the scenario itself.
        if scenario.budget:
            self.budget = await self.control.get_budget_by_id(scenario.budget)
        else:
            self.budget = await self.control.get_resolved_budget()

        self.harness = ModelHarness(
            user_id=self.user_id,
            team_id=self.team_id,
            agent_name=scenario.agent,
            mode=scenario.mode,
            stop_event=self.stop_event,
            interrupt_event=self.interrupt_event,
            budget=self.budget,
        )

        # Tell the dashboard this worker is live — a scenario run
        # self-drives rather than waiting for a Start click, but still
        # shows up as a normal lane and still honors a Stop from it.
        await self.control.patch_desired(state="running", agent=scenario.agent, mode=scenario.mode)
        self.stop_event.clear()
        self.phase = Phase.RUNNING

        async def iteration_fn() -> None:
            await self._run_one_cycle(scenario.agent, scenario.mode)

        executor = build_executor(scenario.executor)
        self._live_executor = executor
        try:
            stats = await executor.run(iteration_fn, self.stop_event)
        finally:
            self.phase = Phase.STOPPED
            await self.control.patch_desired(state="stopped")
            # A short run can finish well inside the 1s heartbeat
            # interval — without this, the poll task gets cancelled
            # before ever reporting STOPPED, leaving the store showing
            # whatever phase the last periodic heartbeat happened to
            # catch (e.g. still "starting" for a sub-second scenario).
            await self.control.heartbeat(
                applied_generation=self.applied_generation,
                phase=self.phase.value,
                current_node=self.current_node,
                iteration=self.iteration,
                last_error=self.last_error,
                applied_interrupt_token=self._last_seen_interrupt_token,
                dropped_iterations_total=self._dropped_total(),
            )
            flush_task.cancel()
            if self._poll_task is not None:
                self._poll_task.cancel()
            telemetry.remove_listener(self._on_artifact_event)
            telemetry.remove_listener(self._on_telemetry_event)
            self._artifact.write_summary(stats.model_dump(), finished=True)
            self._artifact.close()
            self._artifact = None
            self._live_executor = None
            if self.harness is not None:
                await self.harness.aclose()

        print(
            f"[WORKER] Scenario run complete for '{self.user_id}': "
            f"{stats.iterations_completed}/{stats.iterations_started} completed, "
            f"{stats.iterations_dropped} dropped, over {stats.duration_s:.1f}s.",
            flush=True,
        )
        return stats


def main():
    worker = AgentWorker()
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        print("\n[WORKER] Stopped.")


if __name__ == "__main__":
    main()
