import asyncio
import json
from pathlib import Path
from typing import Optional

from maasagentharness.config import settings
from maasagentharness.control.budgets import Budget
from maasagentharness.control.client import ControlClient
from maasagentharness.control.models import DesiredStateResponse, Phase
from maasagentharness.harness import ModelHarness, WorkerInterrupted, WorkerStopped
from maasagentharness.telemetry import TelemetryEvent, telemetry
from maasagentharness.agents.research import ResearchAgent
from maasagentharness.agents.triage import TriageAgent
from maasagentharness.agents.assistant import AssistantAgent

DATA_DIR = Path(__file__).parent / "data"


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

        self._poll_task: Optional[asyncio.Task] = None
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
            )
            await asyncio.sleep(1.0)

    async def run_loop(self):
        print(f"[WORKER] Starting agent worker for user '{self.user_id}' (team '{self.team_id}')...", flush=True)
        self.phase = Phase.STARTING
        await self.control.register(agent=settings.agent_type, mode=settings.agent_mode, managed=False)
        telemetry.add_listener(self._on_telemetry_event)
        self._poll_task = asyncio.create_task(self._poll_and_heartbeat_loop())

        step_idx = 0
        assistant_turn = 0
        assistant_instance: Optional[AssistantAgent] = None

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
                    assistant_instance = None

                try:
                    if agent_type == "triage":
                        triage = TriageAgent(harness=self.harness)
                        letter_item = self.letters[step_idx % len(self.letters)] if self.letters else {"id": "LET-001"}
                        letter_path = DATA_DIR / "letters" / f"letter_{int(letter_item['id'].replace('LET-', '')):04d}.txt"
                        letter_text = letter_path.read_text(encoding="utf-8") if letter_path.exists() else "Dispute sample text"
                        print(f"[{self.user_id}][TRIAGE] Processing {letter_item['id']} in {mode} mode...", flush=True)
                        await triage.run(letter_text=letter_text, letter_id=letter_item["id"])
                        step_idx += 1

                    elif agent_type == "research":
                        sample_queries = [
                            "What is our complete hardship and mortgage repayment moratorium policy under sudden income disruption?",
                            "How do we handle unarranged overdraft fees and direct debit dishonours during financial distress?",
                            "What are the investigation procedures and customer liability protections for SIM-swap and APP scams?",
                            "What is the statutory chargeback timeframe for airline and hotel booking cancellations?",
                        ]
                        q = sample_queries[step_idx % len(sample_queries)]
                        print(f"[{self.user_id}][RESEARCH] '{q[:60]}...' in {mode} mode", flush=True)
                        research = ResearchAgent(harness=self.harness)
                        await research.run(query=q)
                        step_idx += 1

                    elif agent_type == "assistant":
                        if assistant_instance is None or assistant_turn >= 15:
                            assistant_instance = AssistantAgent(harness=self.harness)
                            assistant_turn = 0
                        sample_chat_questions = [
                            "Can you explain the difference between tier-1 and tier-2 dispute thresholds?",
                            "What happens if the merchant doesn't respond within 14 days?",
                            "Does this apply to small business accounts as well?",
                            "What if the transaction was done via contactless chip-and-pin?",
                            "Are late fees waived while the case is in moratorium?",
                            "Who makes the final decision on internal dispute resolution appeals?",
                        ]
                        q = sample_chat_questions[assistant_turn % len(sample_chat_questions)]
                        print(f"[{self.user_id}][ASSISTANT] Turn {assistant_turn + 1}: '{q}' in {mode} mode", flush=True)
                        async for chunk in assistant_instance.stream_turn(q):
                            pass
                        assistant_turn += 1

                except WorkerStopped:
                    # Expected — a Stop command interrupted this cycle partway
                    # through. Don't advance step counters for a cycle that
                    # didn't complete, and don't log it as an error.
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


def main():
    worker = AgentWorker()
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        print("\n[WORKER] Stopped.")


if __name__ == "__main__":
    main()
