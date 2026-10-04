import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
import httpx
from pydantic import BaseModel

from maasagentharness.config import settings
from maasagentharness.harness import ModelHarness, WorkerStopped
from maasagentharness.agents.research import ResearchAgent
from maasagentharness.agents.triage import TriageAgent
from maasagentharness.agents.assistant import AssistantAgent

DATA_DIR = Path(__file__).parent / "data"


class AgentControlState(BaseModel):
    user: str
    state: str = "running"  # "running" or "stopped"
    agent: str = "triage"  # "triage", "research", "assistant"
    mode: str = "naive"  # "naive", "paced", "runaway"
    limit_tpm: int = 2000
    # Phase 0 fix #0.1: set by the dashboard when this user_id has never
    # called /register. Informational on this side — the dashboard already
    # encodes "never seen" as state="stopped", this just makes it explicit
    # for logging rather than silently indistinguishable from a real stop.
    unregistered: bool = False


class AgentWorker:
    def __init__(
        self,
        user_id: str = settings.user_id,
        team_id: str = settings.team_id,
        dashboard_url: str = settings.dashboard_url,
    ):
        self.user_id = user_id
        self.team_id = team_id
        self.dashboard_url = dashboard_url
        self.control_state = AgentControlState(
            user=self.user_id,
            state="stopped",
            agent=settings.agent_type,
            mode=settings.agent_mode,
            limit_tpm=settings.limit_tpm,
        )
        self.current_harness: Optional[ModelHarness] = None
        # Phase 0 fix #0.3/#0.4: this event is threaded into every
        # ModelHarness this worker creates. It is checked before every model
        # call, every pacer wait, and every retry/backoff sleep, so a Stop
        # command takes effect within milliseconds rather than at the end of
        # the current multi-call cycle (triage: 3 calls, research: 8+).
        # A background poll task (see `_poll_loop`) keeps it in sync with the
        # dashboard continuously — not just between cycles — which is what
        # makes mid-cycle interruption possible at all.
        self.stop_event = asyncio.Event()
        self.stop_event.set()  # start stopped, matches control_state default
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
        return defaults.get(self.control_state.agent, self.control_state.agent)

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

    async def register(self) -> None:
        """Register this agent with the dashboard on startup."""
        url = f"{self.dashboard_url.rstrip('/')}/register"
        payload = {
            "user": self.user_id,
            "team": self.team_id,
            "display_name": self.display_name,
            "task_description": self.task_description,
            "agent": self.control_state.agent,
            "mode": self.control_state.mode,
            "state": "stopped",
            "limit_tpm": settings.limit_tpm,
        }
        async with httpx.AsyncClient(timeout=3.0) as client:
            try:
                await client.post(url, json=payload)
                print(f"[WORKER] Registered '{self.display_name}' ({self.user_id}) with dashboard.", flush=True)
            except Exception as e:
                print(f"[WORKER] Could not register with dashboard: {e}", flush=True)

    async def poll_control(self) -> None:
        """Poll dashboard for desired state, update local state, and keep
        `stop_event` in sync so any in-flight model call sees the change
        immediately (see `_sync_stop_event`)."""
        url = f"{self.dashboard_url.rstrip('/')}/control/{self.user_id}"
        async with httpx.AsyncClient(timeout=1.5) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    self.control_state = AgentControlState(**data)
            except Exception:
                # Dashboard not reachable or running standalone
                pass
        self._sync_stop_event()

    def _sync_stop_event(self) -> None:
        if self._is_running():
            self.stop_event.clear()
        else:
            self.stop_event.set()

    def _is_running(self) -> bool:
        return self.control_state.state == "running" and not self.control_state.unregistered

    async def _poll_loop(self) -> None:
        """Runs concurrently with `run_loop`'s cycle execution — this is what
        lets a Stop command interrupt a cycle already in progress instead of
        only being noticed once the cycle finishes."""
        while True:
            await self.poll_control()
            await asyncio.sleep(1.0)

    async def run_loop(self):
        print(f"[WORKER] Starting agent worker for user '{self.user_id}' (team '{self.team_id}')...", flush=True)
        await self.register()
        self._poll_task = asyncio.create_task(self._poll_loop())

        step_idx = 0
        assistant_turn = 0
        assistant_instance: Optional[AssistantAgent] = None

        # Persistent harness — keeps the token bucket alive across cycles
        current_agent_type = self.control_state.agent
        current_mode = self.control_state.mode
        harness = ModelHarness(
            user_id=self.user_id,
            team_id=self.team_id,
            agent_name=current_agent_type,
            mode=current_mode,
            stop_event=self.stop_event,
        )

        was_running = False

        try:
            while True:
                is_running = self._is_running()

                if not is_running:
                    was_running = False
                    await asyncio.sleep(1.0)
                    continue

                # Just transitioned from stopped → running: refill the bucket so
                # the agent doesn't wait up to 60s for tokens to accumulate.
                if not was_running:
                    if harness.pacer is not None:
                        harness.pacer.tokens = harness.pacer.capacity
                    was_running = True

                agent_type = self.control_state.agent
                mode = self.control_state.mode

                # Recreate harness only when agent type or mode changes
                if agent_type != current_agent_type or mode != current_mode:
                    current_agent_type = agent_type
                    current_mode = mode
                    harness = ModelHarness(
                        user_id=self.user_id,
                        team_id=self.team_id,
                        agent_name=agent_type,
                        mode=mode,
                        stop_event=self.stop_event,
                    )
                    assistant_instance = None

                # Keep pacer target in sync with dashboard limit
                if harness.pacer is not None:
                    harness.pacer.update_target(
                        self.control_state.limit_tpm * settings.paced_target_ratio
                    )

                try:
                    if agent_type == "triage":
                        triage = TriageAgent(harness=harness)
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
                        research = ResearchAgent(harness=harness)
                        await research.run(query=q)
                        step_idx += 1

                    elif agent_type == "assistant":
                        if assistant_instance is None or assistant_turn >= 15:
                            assistant_instance = AssistantAgent(harness=harness)
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
