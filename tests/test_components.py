"""Component smoke tests — construction and wiring, not network calls.

Phase 0 fix #0.14: previously `async def test_components()` was only ever
invoked from `__main__`; under plain pytest it was collected and silently
never awaited, so this file asserted nothing in CI. Converted to real
pytest `async def test_*` functions with assertions pytest-asyncio can run.
"""
from maasagentharness.pacer import TokenBucket
from maasagentharness.harness import ModelHarness
from maasagentharness.agents.research import ResearchAgent
from maasagentharness.agents.triage import TriageAgent
from maasagentharness.agents.assistant import AssistantAgent


async def test_token_bucket_acquire():
    bucket = TokenBucket(target_tpm=1800)
    assert bucket.capacity == 1800
    assert bucket.refill_rate == 30.0
    await bucket.acquire(100)
    assert bucket.tokens == 1700


def test_harness_init():
    harness = ModelHarness(user_id="user-a", team_id="team-alpha", agent_name="triage", mode="paced")
    assert harness.pacer is not None
    assert harness.mode == "paced"


def test_agent_graphs_compile():
    harness = ModelHarness(user_id="user-a", team_id="team-alpha", agent_name="triage", mode="paced")
    assert TriageAgent(harness=harness).graph is not None
    assert ResearchAgent(harness=harness).graph is not None
    assert AssistantAgent(harness=harness) is not None
