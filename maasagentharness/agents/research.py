import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from maasagentharness.harness import ModelHarness, RateLimitExceeded, WorkerStopped


DATA_DIR = Path(__file__).parent.parent / "data"
POLICIES_DIR = DATA_DIR / "policies"


class SubQuestionResult(BaseModel):
    sub_question: str
    policy_reference: Optional[str] = None
    answer: str
    status: str = "success"  # success, throttled, failed


class ResearchState(BaseModel):
    query: str
    sub_questions: List[str] = Field(default_factory=list)
    worker_results: List[SubQuestionResult] = Field(default_factory=list)
    final_synthesis: Optional[str] = None
    run_id: str = Field(default_factory=lambda: f"r-{uuid.uuid4().hex[:6]}")


class ResearchAgent:
    """Research agent (fan-out pattern).
    Splits policy inquiry into 6-10 sub-questions, executes sequential worker queries,
    and synthesizes results even if partial workers fail or get rate limited (429).
    """

    def __init__(self, harness: ModelHarness):
        self.harness = harness
        self._load_policies_sample()
        self.graph = self._build_graph()

    def _load_policies_sample(self):
        index_file = DATA_DIR / "policies_index.json"
        if index_file.exists():
            self.policies_index = json.loads(index_file.read_text(encoding="utf-8"))
        else:
            self.policies_index = []

    def _build_graph(self):
        workflow = StateGraph(ResearchState)

        async def planner_node(state: ResearchState):
            prompt = f"""You are a bank policy analysis planner. Break down the following complex customer/policy question into 6 to 8 specific, focused sub-questions that need investigation across hardship, card disputes, fees, and fraud policies.

Question: {state.query}

Output only a valid JSON array of strings representing the sub-questions. Do not wrap in markdown tags if possible, just standard JSON."""
            try:
                response = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are an expert banking operations planner."),
                        HumanMessage(content=prompt),
                    ],
                    node_name="planner",
                    run_id=state.run_id,
                    estimated_input_tokens=400,
                )
                import re
                match = re.search(r"\[.*\]", response, re.DOTALL)
                if match:
                    sub_questions = json.loads(match.group(0))
                else:
                    sub_questions = [line.strip("- 1234567890.") for line in response.split("\n") if line.strip()][:8]
            except WorkerStopped:
                # Propagate — a Stop command, not a planner failure to fall back from.
                raise
            except Exception:
                sub_questions = [
                    "What are the eligibility criteria for hardship assistance?",
                    "What is the statutory response timeframe for fee dispute resolution?",
                    "Under what conditions are cardholder liabilities waived for fraud?",
                    "What documentation is mandatory for compassionate repayment relief?",
                    "What are the escalation rules for internal dispute resolution (IDR)?",
                    "How are direct debit dishonour fees assessed?",
                ]
            return {"sub_questions": sub_questions}

        async def execute_worker(sub_q: str, state: ResearchState) -> SubQuestionResult:
            # Pick a relevant policy text or excerpt
            policy_text = ""
            pol_files = list(POLICIES_DIR.glob("*.txt"))
            if pol_files:
                sample_file = pol_files[hash(sub_q) % len(pol_files)]
                policy_text = sample_file.read_text(encoding="utf-8")[:1200]

            worker_prompt = f"""Sub-question: {sub_q}

Policy Excerpt:
{policy_text}

Provide a concise, factual 2-sentence answer based strictly on the policy excerpt provided."""
            try:
                ans = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a banking policy compliance worker."),
                        HumanMessage(content=worker_prompt),
                    ],
                    node_name=f"worker_query",
                    run_id=state.run_id,
                    estimated_input_tokens=600,
                )
                return SubQuestionResult(sub_question=sub_q, answer=ans, status="success")
            except RateLimitExceeded:
                return SubQuestionResult(
                    sub_question=sub_q,
                    answer="[UNAVAILABLE: Request rate limited by MaaS gateway (429)]",
                    status="throttled",
                )
            except WorkerStopped:
                # Phase 0 fix #0.3/#0.4: a Stop command interrupted this
                # worker call. Propagate rather than recording it as a
                # "failed" result — the research cycle itself is being
                # aborted, not this one sub-question.
                raise
            except Exception as e:
                return SubQuestionResult(
                    sub_question=sub_q,
                    answer=f"[FAILED: Error processing sub-question - {str(e)}]",
                    status="failed",
                )

        async def parallel_workers_node(state: ResearchState):
            # Run workers sequentially — avoids token burst. A Stop command
            # raises WorkerStopped out of execute_worker (via the harness),
            # which propagates straight out of this node and the graph —
            # see docs/phase-0-hotfixes.md #0.3/#0.4. This replaces the old
            # per-iteration `stop_check` callback, which only ever saw
            # whatever control_state the background poller had last written
            # before the cycle started, not a live signal.
            results = []
            for sub_q in state.sub_questions:
                results.append(await execute_worker(sub_q, state))
            return {"worker_results": results}

        async def synthesizer_node(state: ResearchState):
            successful_answers = [r for r in state.worker_results if r.status == "success"]
            throttled_answers = [r for r in state.worker_results if r.status == "throttled"]

            summary_input = "\n\n".join([f"Q: {r.sub_question}\nA: {r.answer}" for r in state.worker_results])
            synth_prompt = f"""Synthesize the following policy inquiry answers into a coherent final briefing. Note that some sub-questions may have been throttled or failed; integrate available findings and highlight missing information.

Original Inquiry: {state.query}

Worker Findings:
{summary_input}

Provide an executive summary synthesis."""
            try:
                synthesis = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a senior banking executive synthesiser."),
                        HumanMessage(content=synth_prompt),
                    ],
                    node_name="synthesizer",
                    run_id=state.run_id,
                    estimated_input_tokens=1000,
                )
            except RateLimitExceeded:
                synthesis = f"Synthesis incomplete: Synthesizer throttled (429). {len(successful_answers)} of {len(state.worker_results)} sub-questions resolved successfully."
            except WorkerStopped:
                raise
            except Exception as e:
                synthesis = f"Synthesis incomplete: {str(e)}. {len(successful_answers)} of {len(state.worker_results)} sub-questions resolved."

            return {"final_synthesis": synthesis}

        workflow.add_node("planner", planner_node)
        workflow.add_node("parallel_workers", parallel_workers_node)
        workflow.add_node("synthesizer", synthesizer_node)

        workflow.set_entry_point("planner")
        workflow.add_edge("planner", "parallel_workers")
        workflow.add_edge("parallel_workers", "synthesizer")
        workflow.add_edge("synthesizer", END)

        return workflow.compile()

    async def run(self, query: str) -> Dict[str, Any]:
        initial_state = ResearchState(query=query)
        result = await self.graph.ainvoke(initial_state)
        return result
