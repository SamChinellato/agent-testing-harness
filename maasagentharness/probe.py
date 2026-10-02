import asyncio
import json
import time
from typing import List
from langchain_core.messages import HumanMessage
from maasagentharness.config import settings
from maasagentharness.harness import ModelHarness


async def main():
    print(f"[PROBE] Starting probe against {settings.maas_host}{settings.model_path} with model {settings.model_name}...")
    harness = ModelHarness(agent_name="probe", mode="runaway")
    
    # Send rapid requests for 90 seconds to test rate limit behavior and headers
    start_time = time.monotonic()
    request_count = 0
    throttled_count = 0

    while time.monotonic() - start_time < 90:
        request_count += 1
        try:
            print(f"[PROBE] Sending request #{request_count}...", flush=True)
            res = await harness.invoke_with_telemetry(
                messages=[HumanMessage(content="Acknowledge this probe request with 'OK'.")],
                node_name="probe_req",
                run_id=f"probe-{request_count}",
                estimated_input_tokens=150,
            )
            print(f"[PROBE] #{request_count} response received: {res[:40]}...")
        except Exception as e:
            throttled_count += 1
            print(f"[PROBE] #{request_count} encountered error / 429: {e}")
        
        await asyncio.sleep(0.1)

    print(f"\n[PROBE SUMMARY] Completed 90s probe: {request_count} sent, {throttled_count} throttled.")


if __name__ == "__main__":
    asyncio.run(main())
