import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from maasagentharness.harness import ModelHarness, RateLimitExceeded


DATA_DIR = Path(__file__).parent.parent / "data"
LETTERS_DIR = DATA_DIR / "letters"


class DisputeFields(BaseModel):
    category: Optional[str] = None
    merchant: Optional[str] = None
    amount: Optional[str] = None
    date: Optional[str] = None
    summary: Optional[str] = None
    status: str = "success"  # success, throttled, failed


class TriageState(BaseModel):
    letter_text: str
    letter_id: str
    category: Optional[str] = None
    extracted_fields: Optional[DisputeFields] = None
    summary: Optional[str] = None
    run_id: str = Field(default_factory=lambda: f"t-{uuid.uuid4().hex[:6]}")


class TriageAgent:
    """Triage agent (batch queue pattern).
    Processes dispute letters: classify -> extract fields (amount, date, merchant, reason) -> draft one-line summary.
    Input heavy with steady long prompts and short outputs.
    """

    def __init__(self, harness: ModelHarness):
        self.harness = harness
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(TriageState)

        async def classify_node(state: TriageState):
            prompt = f"""Classify the following customer dispute letter into one of these categories:
- UNAUTHORISED_TRANSACTION
- GOODS_SERVICES_NOT_RECEIVED
- SUBSCRIPTION_BILLING_ISSUE
- DEFECTIVE_MERCHANDISE
- FEE_DISCREPANCY
- GENERAL_INQUIRY

Letter Text:
{state.letter_text}

Respond with ONLY the exact category name."""
            try:
                response = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a dispute intake triage classifier."),
                        HumanMessage(content=prompt),
                    ],
                    node_name="classify",
                    run_id=state.run_id,
                    estimated_input_tokens=700,
                )
                category = response.strip()
            except RateLimitExceeded:
                category = "THROTTLED_429"
            except Exception:
                category = "CLASSIFICATION_FAILED"

            return {"category": category}

        async def extract_fields_node(state: TriageState):
            prompt = f"""Extract key information from this dispute letter into JSON format with keys: merchant, amount, date, reason.

Letter Text:
{state.letter_text}

Output JSON only."""
            try:
                response = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a structured data extraction specialist."),
                        HumanMessage(content=prompt),
                    ],
                    node_name="extract_fields",
                    run_id=state.run_id,
                    estimated_input_tokens=800,
                )
                import re
                match = re.search(r"\{.*\}", response, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                else:
                    data = {"raw_extract": response}

                fields = DisputeFields(
                    category=state.category,
                    merchant=data.get("merchant"),
                    amount=data.get("amount"),
                    date=data.get("date"),
                    status="success"
                )
            except RateLimitExceeded:
                fields = DisputeFields(category=state.category, status="throttled")
            except Exception:
                fields = DisputeFields(category=state.category, status="failed")

            return {"extracted_fields": fields}

        async def draft_summary_node(state: TriageState):
            fields_desc = state.extracted_fields.model_dump_json() if state.extracted_fields else "N/A"
            prompt = f"""Draft a single concise one-line triage summary note for the case officer based on:
Category: {state.category}
Extracted Fields: {fields_desc}

Provide only the one-line summary."""
            try:
                summary = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a banking case file summariser."),
                        HumanMessage(content=prompt),
                    ],
                    node_name="draft_summary",
                    run_id=state.run_id,
                    estimated_input_tokens=400,
                )
            except RateLimitExceeded:
                summary = "[Summary unavailable: Throttled (429)]"
            except Exception as e:
                summary = f"[Summary failed: {str(e)}]"

            return {"summary": summary}

        workflow.add_node("classify", classify_node)
        workflow.add_node("extract_fields", extract_fields_node)
        workflow.add_node("draft_summary", draft_summary_node)

        workflow.set_entry_point("classify")
        workflow.add_edge("classify", "extract_fields")
        workflow.add_edge("extract_fields", "draft_summary")
        workflow.add_edge("draft_summary", END)

        return workflow.compile()

    async def run(self, letter_text: str, letter_id: str = "LET-0001") -> Dict[str, Any]:
        initial_state = TriageState(letter_text=letter_text, letter_id=letter_id)
        result = await self.graph.ainvoke(initial_state)
        return result
