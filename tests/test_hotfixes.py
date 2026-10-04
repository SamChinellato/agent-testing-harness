"""Regression tests for the Phase 0 hotfixes in docs/phase-0-hotfixes.md.

These are intentionally narrow — each test targets exactly one fix and is
named after it, so a future regression points straight at which doc section
broke.
"""
import asyncio
import inspect
import time

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from maasagentharness import probe
from maasagentharness.harness import (
    ModelHarness,
    RateLimitExceeded,
    WorkerStopped,
    messages_to_openai_payload,
)
from maasagentharness.pacer import TokenBucket


# ---------------------------------------------------------------------------
# #0.5 — role mapping
# ---------------------------------------------------------------------------

def test_role_mapping_produces_valid_openai_roles():
    messages = [
        SystemMessage(content="s"),
        HumanMessage(content="h"),
        AIMessage(content="a"),
    ]
    payload = messages_to_openai_payload(messages)
    assert [m["role"] for m in payload] == ["system", "user", "assistant"]
    # Specifically: AIMessage.type == "ai", not "assistant" — the previous
    # implementation forwarded `.type` almost verbatim and sent role="ai",
    # which is not a valid OpenAI role.
    assert payload[2]["role"] != "ai"


# ---------------------------------------------------------------------------
# #0.3/#0.4 — interruptible stop
# ---------------------------------------------------------------------------

async def test_invoke_raises_immediately_when_already_stopped():
    """No network call should happen at all — stop is checked up front."""
    stop_event = asyncio.Event()
    stop_event.set()
    harness = ModelHarness(agent_name="test", mode="naive", stop_event=stop_event)

    start = time.monotonic()
    with pytest.raises(WorkerStopped):
        await harness.invoke_with_telemetry(
            messages=[HumanMessage(content="hi")],
            node_name="test",
            run_id="r1",
        )
    elapsed = time.monotonic() - start
    assert elapsed < 0.5  # instant — never touched the network


async def test_interruptible_sleep_aborts_before_full_duration():
    stop_event = asyncio.Event()
    harness = ModelHarness(agent_name="test", mode="naive", stop_event=stop_event)

    async def fire_stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    start = time.monotonic()
    fire_task = asyncio.create_task(fire_stop_soon())
    with pytest.raises(WorkerStopped):
        await harness._interruptible_sleep(10.0)  # would take 10s uninterrupted
    elapsed = time.monotonic() - start
    await fire_task
    assert elapsed < 1.0  # interrupted almost immediately, not after 10s


async def test_pacer_acquire_returns_promptly_when_stopped_without_raising():
    """The pacer itself never raises — it returns early and leaves the
    'should I actually proceed?' decision to the harness."""
    bucket = TokenBucket(target_tpm=1)  # effectively never has enough tokens
    stop_event = asyncio.Event()

    async def fire_stop_soon():
        await asyncio.sleep(0.05)
        stop_event.set()

    start = time.monotonic()
    fire_task = asyncio.create_task(fire_stop_soon())
    await bucket.acquire(100_000, stop_event=stop_event)  # must not hang or raise
    elapsed = time.monotonic() - start
    await fire_task
    assert elapsed < 1.0


# ---------------------------------------------------------------------------
# #0.6 — stream_with_telemetry re-raises instead of swallowing
# ---------------------------------------------------------------------------

class _FakeThrottledStreamResponse:
    status_code = 429
    headers = httpx.Headers({"retry-after": "1"})

    async def aiter_lines(self):
        return
        yield  # pragma: no cover — never reached; 429 raises before this

    def raise_for_status(self):
        raise AssertionError("should not be reached — 429 raises first")


class _FakeStreamCtx:
    async def __aenter__(self):
        return _FakeThrottledStreamResponse()

    async def __aexit__(self, *exc_info):
        return False


async def test_stream_with_telemetry_reraises_rate_limit(monkeypatch):
    def fake_stream(self, method, url, headers=None, json=None):
        return _FakeStreamCtx()

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    monkeypatch.setattr("maasagentharness.harness.settings.maas_endpoint", "https://example.invalid")

    harness = ModelHarness(agent_name="test", mode="naive")
    with pytest.raises(RateLimitExceeded):
        async for _ in harness.stream_with_telemetry(
            messages=[HumanMessage(content="hi")],
            node_name="test",
            run_id="r1",
        ):
            pass


# ---------------------------------------------------------------------------
# #0.7 — probe entry point is callable synchronously
# ---------------------------------------------------------------------------

def test_probe_main_is_sync_entry_point():
    assert not inspect.iscoroutinefunction(probe.main)
