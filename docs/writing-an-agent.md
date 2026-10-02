# Writing an Agent

This guide explains how to write a new agent that integrates with the harness. Your agent gets automatic telemetry, rate-limit handling, pacing, start/stop control, and live dashboard visibility without any extra wiring.

---

## What an agent is

An agent is any Python class that:

1. Accepts a `ModelHarness` instance
2. Makes one or more LLM calls via `harness.invoke_with_telemetry()` or `harness.stream_with_telemetry()`
3. Is called from a cycle in `runner.py`

That is the entire contract. The harness handles everything else.

---

## Minimal agent example

```python
# maasagentharness/agents/my_agent.py

import uuid
from typing import Any, Dict
from langchain_core.messages import HumanMessage, SystemMessage

from maasagentharness.harness import ModelHarness, RateLimitExceeded


class MyAgent:
    def __init__(self, harness: ModelHarness):
        self.harness = harness

    async def run(self, input_text: str) -> Dict[str, Any]:
        run_id = f"my-{uuid.uuid4().hex[:6]}"

        try:
            response = await self.harness.invoke_with_telemetry(
                messages=[
                    SystemMessage(content="You are a helpful assistant."),
                    HumanMessage(content=input_text),
                ],
                node_name="respond",        # shown in dashboard Step column
                run_id=run_id,              # groups calls from one cycle together
                estimated_input_tokens=300, # used by paced token bucket
            )
            return {"response": response}

        except RateLimitExceeded:
            return {"response": "[throttled]"}
```

---

## Registering the agent in runner.py

Open `maasagentharness/runner.py` and add an `elif` branch in `run_loop()`:

```python
# In run_loop(), after the existing elif branches:

elif agent_type == "my_agent":
    my_queries = ["Hello, what is 2+2?", "Summarise the concept of rate limiting."]
    q = my_queries[step_idx % len(my_queries)]
    print(f"[{self.user_id}][MY_AGENT] Running: '{q}'", flush=True)
    agent = MyAgent(harness=harness)
    await agent.run(input_text=q)
    step_idx += 1
```

Then import your agent at the top of `runner.py`:

```python
from maasagentharness.agents.my_agent import MyAgent
```

Start your agent with `AGENT_TYPE=my_agent` and it will appear in the dashboard immediately.

---

## Using a LangGraph graph

The built-in agents use [LangGraph](https://github.com/langchain-ai/langgraph) to define multi-step workflows. This is optional — you can use any control flow. But if you want a graph with multiple nodes, here is the pattern:

```python
import uuid
from typing import Any, Dict, Optional
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from maasagentharness.harness import ModelHarness, RateLimitExceeded


class MyState(BaseModel):
    input: str
    step_one_output: Optional[str] = None
    final_output: Optional[str] = None
    run_id: str = Field(default_factory=lambda: f"my-{uuid.uuid4().hex[:6]}")


class MyMultiStepAgent:
    def __init__(self, harness: ModelHarness):
        self.harness = harness
        self.graph = self._build_graph()

    def _build_graph(self):
        workflow = StateGraph(MyState)

        async def step_one(state: MyState):
            try:
                result = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are an analyst."),
                        HumanMessage(content=f"Analyse: {state.input}"),
                    ],
                    node_name="step_one",           # shows in dashboard Step column
                    run_id=state.run_id,
                    estimated_input_tokens=400,
                )
            except RateLimitExceeded:
                result = "[step_one throttled]"
            return {"step_one_output": result}

        async def step_two(state: MyState):
            try:
                result = await self.harness.invoke_with_telemetry(
                    messages=[
                        SystemMessage(content="You are a summariser."),
                        HumanMessage(content=f"Summarise: {state.step_one_output}"),
                    ],
                    node_name="step_two",
                    run_id=state.run_id,
                    estimated_input_tokens=300,
                )
            except RateLimitExceeded:
                result = "[step_two throttled]"
            return {"final_output": result}

        workflow.add_node("step_one", step_one)
        workflow.add_node("step_two", step_two)
        workflow.set_entry_point("step_one")
        workflow.add_edge("step_one", "step_two")
        workflow.add_edge("step_two", END)

        return workflow.compile()

    async def run(self, input_text: str) -> Dict[str, Any]:
        return await self.graph.ainvoke(MyState(input=input_text))
```

Each node name you pass as `node_name` appears in the **Step** column of the dashboard log, so you can see exactly which node was executing when a 429 occurred.

---

## Using streaming

For agents that benefit from streaming (e.g. chat interfaces, long responses), use `stream_with_telemetry` instead:

```python
async def my_streaming_method(self, query: str):
    run_id = f"s-{uuid.uuid4().hex[:6]}"
    messages = [
        SystemMessage(content="You are an assistant."),
        HumanMessage(content=query),
    ]
    estimated_tokens = len(query) // 4 + 100

    full_reply = []
    async for token in self.harness.stream_with_telemetry(
        messages=messages,
        node_name="stream_response",
        run_id=run_id,
        estimated_input_tokens=estimated_tokens,
    ):
        full_reply.append(token)
        # optionally yield token here if your caller consumes it

    return "".join(full_reply)
```

Telemetry is emitted once at the end of the stream, including actual token counts from the server's `usage` chunk (or an estimate if the server does not return usage in the stream).

---

## The `node_name` and `run_id` fields

These two fields are cosmetic in terms of harness behaviour but important for observability:

**`node_name`** — a short label for the current operation. Appears in the **Step** column of the dashboard event log. Use descriptive names that reflect what the agent is doing: `"classify"`, `"extract_fields"`, `"worker_query"`, `"synthesize"`. Using distinct names per step lets you see which part of your pipeline is being throttled.

**`run_id`** — a string that groups all calls from a single invocation cycle. Generate it once per `run()` call and pass it to every `invoke_with_telemetry` call in that cycle. Format is up to you — `f"my-{uuid.uuid4().hex[:6]}"` is sufficient.

---

## Handling `RateLimitExceeded`

The harness raises `RateLimitExceeded` when the server returns a 429 and retries are exhausted. Always catch it and return a graceful fallback rather than letting it propagate — otherwise a single throttled call can crash the entire cycle:

```python
try:
    result = await self.harness.invoke_with_telemetry(...)
except RateLimitExceeded:
    result = "[unavailable: throttled]"
```

The harness has already emitted a telemetry event for the failed call before raising, so the dashboard will show the 429 regardless of whether you catch the exception.

---

## `estimated_input_tokens`

This value is used by the paced token bucket to pre-deduct tokens before a request is made — it is the only way the bucket can pace a request before it goes out (since the actual token count isn't known until after the response returns).

- Over-estimate rather than under-estimate. If your prompt is large, use a higher value.
- A rough rule: `len(prompt_text) // 4` gives a reasonable character-to-token approximation for English text.
- The harness reports *actual* tokens (from the response `usage` field) in telemetry, regardless of what you estimated.

---

## Checklist for a new agent

- [ ] Class accepts `ModelHarness` in `__init__`
- [ ] All LLM calls go through `harness.invoke_with_telemetry()` or `harness.stream_with_telemetry()`
- [ ] All nodes catch `RateLimitExceeded` and return graceful fallback values
- [ ] Each call has a descriptive `node_name` and a shared `run_id` per cycle
- [ ] Agent is imported and wired in `runner.py` under a new `AGENT_TYPE` value
- [ ] Start with `AGENT_TYPE=your_type` in the environment
