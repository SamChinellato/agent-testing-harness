import asyncio
import os
import sys
from maasagentharness.config import settings
from maasagentharness.pacer import TokenBucket
from maasagentharness.harness import ModelHarness
from maasagentharness.agents.research import ResearchAgent
from maasagentharness.agents.triage import TriageAgent
from maasagentharness.agents.assistant import AssistantAgent


async def test_components():
    print("Testing maasagentharness components locally...")
    
    # 1. Test TokenBucket
    bucket = TokenBucket(target_tpm=1800)
    print(f"TokenBucket initialized: capacity={bucket.capacity}, rate={bucket.refill_rate:.2f}/s")
    await bucket.acquire(100)
    print("TokenBucket acquire completed successfully.")

    # 2. Test Harness init
    harness = ModelHarness(user_id="user-a", team_id="team-alpha", agent_name="triage", mode="paced")
    print("ModelHarness initialized successfully.")

    # 3. Test Graph compilations
    triage = TriageAgent(harness=harness)
    print("TriageAgent graph compiled successfully.")

    research = ResearchAgent(harness=harness)
    print("ResearchAgent graph compiled successfully.")

    assistant = AssistantAgent(harness=harness)
    print("AssistantAgent initialized successfully.")

    print("All component tests passed!")


if __name__ == "__main__":
    asyncio.run(test_components())
