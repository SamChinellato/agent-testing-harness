import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from maasagentharness.harness import ModelHarness, RateLimitExceeded


DATA_DIR = Path(__file__).parent.parent / "data"
POLICIES_DIR = DATA_DIR / "policies"


class ChatMessage(BaseModel):
    role: str
    content: str


class AssistantState(BaseModel):
    history: List[ChatMessage] = Field(default_factory=list)
    current_user_query: str
    context_policy: Optional[str] = None
    response: Optional[str] = None
    run_id: str = Field(default_factory=lambda: f"a-{uuid.uuid4().hex[:6]}")


class AssistantAgent:
    """Assistant agent (multi-turn chat pattern).
    Simulates multi-turn interactive Q&A over the policy corpus with streaming on,
    keeping full conversation history so prompt tokens grow steadily with each turn.
    """

    def __init__(self, harness: ModelHarness):
        self.harness = harness
        self.history: List[ChatMessage] = []
        self._load_policies()

    def _load_policies(self):
        index_file = DATA_DIR / "policies_index.json"
        if index_file.exists():
            self.policies_index = json.loads(index_file.read_text(encoding="utf-8"))
        else:
            self.policies_index = []

    def get_full_messages(self, user_query: str) -> List[BaseMessage]:
        system_prompt = (
            "You are a helpful and precise banking compliance assistant. "
            "Assist the customer or staff member with questions about hardship, disputes, fraud, and account fees. "
            "Use the provided conversation history to maintain context."
        )
        messages: List[BaseMessage] = [SystemMessage(content=system_prompt)]
        
        for m in self.history:
            if m.role == "user":
                messages.append(HumanMessage(content=m.content))
            elif m.role == "assistant":
                messages.append(AIMessage(content=m.content))

        messages.append(HumanMessage(content=user_query))
        return messages

    async def stream_turn(self, user_query: str, run_id: Optional[str] = None) -> AsyncIterator[str]:
        """Runs one turn of conversation in streaming mode, emitting telemetry chunks and updating history."""
        r_id = run_id or f"a-{uuid.uuid4().hex[:6]}"
        messages = self.get_full_messages(user_query)

        # Estimate input token count based on accumulated history
        estimated_input_tokens = sum(len(m.content) // 4 for m in messages) + 150

        full_reply = []
        try:
            async for token in self.harness.stream_with_telemetry(
                messages=messages,
                node_name="chat_turn",
                run_id=r_id,
                estimated_input_tokens=estimated_input_tokens,
            ):
                full_reply.append(token)
                yield token

            # Record in history
            self.history.append(ChatMessage(role="user", content=user_query))
            self.history.append(ChatMessage(role="assistant", content="".join(full_reply)))

        except RateLimitExceeded:
            err = "[Stream interrupted: 429 Too Many Requests from MaaS Gateway]"
            yield err
            self.history.append(ChatMessage(role="user", content=user_query))
            self.history.append(ChatMessage(role="assistant", content=err))

    def reset(self):
        self.history = []
