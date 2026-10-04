import asyncio
import json
import os
import random
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union
import httpx
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage

from maasagentharness.config import settings
from maasagentharness.control.budgets import Budget
from maasagentharness.pacer import TokenBucket
from maasagentharness.telemetry import TelemetryEvent, telemetry


# LangChain's BaseMessage.type is its own taxonomy, not an OpenAI role. Mapping
# it 1:1 (as the previous implementation did) sends `{"role": "ai", ...}` for
# every assistant turn in history — not a valid OpenAI role, and a gateway will
# reject it with 400 as soon as any multi-turn conversation has an assistant
# reply in it. See docs/phase-0-hotfixes.md #0.5.
ROLE_MAP: Dict[str, str] = {
    "system": "system",
    "human": "user",
    "ai": "assistant",
    "tool": "tool",
}


def messages_to_openai_payload(messages: List[BaseMessage]) -> List[Dict[str, str]]:
    """Convert LangChain messages to OpenAI chat-completion message dicts.

    Raises KeyError (loudly, not silently) if a message type has no mapping —
    better to fail the call than to forward an invalid role to the gateway.
    """
    return [{"role": ROLE_MAP[m.type], "content": m.content} for m in messages]


class RateLimitExceeded(Exception):
    """Raised when 429 is received and retries are exhausted."""
    def __init__(self, message: str, headers: Dict[str, str]):
        super().__init__(message)
        self.headers = headers


class WorkerStopped(Exception):
    """Raised when a Stop command (via `stop_event`) interrupts a call or wait.

    This is the mechanism behind "struggling to stop them": every model call,
    every pacer wait, and every retry/backoff sleep checks `stop_event` and
    raises this instead of completing, so a Stop click takes effect within
    milliseconds rather than at the end of the current cycle or backoff window.
    """
    pass


class WorkerInterrupted(Exception):
    """Raised when an Interrupt command (via `interrupt_event`) cancels the
    current in-flight call. Unlike WorkerStopped, this is single-shot and
    does NOT halt the worker — the next cycle proceeds normally. See
    docs/phase-1-5-implementation-plan.md's Phase 1 "Interrupt" note: the
    control plane and worker are different processes talking over HTTP, so
    this can't be a real asyncio Task.cancel() from the API side — instead
    the worker polls for an incrementing `interrupt_token` and raises this
    itself, bounded by the poll interval (~1s) rather than instant.

    Known limitation: if no call is in-flight when an interrupt is
    requested, the event stays set and aborts the *next* call issued
    shortly after instead of silently no-opping. Acceptable for Phase 1;
    revisit if this proves surprising in practice.
    """
    pass


class ModelHarness:
    """Shared harness for LLM interaction, token-bucket pacing, retry policy, and telemetry."""

    def __init__(
        self,
        user_id: Optional[str] = None,
        team_id: Optional[str] = None,
        agent_name: str = "general",
        mode: str = "naive",
        pacer: Optional[TokenBucket] = None,
        stop_event: Optional[asyncio.Event] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        budget: Optional[Budget] = None,
    ):
        self.user_id = user_id or settings.user_id
        self.team_id = team_id or settings.team_id
        self.agent_name = agent_name
        self.mode = mode  # naive, paced, runaway
        # Phase 2: `budget` is the resolved Budget (control/budgets.py) for
        # this worker — None when the caller hasn't wired the budget system
        # in (e.g. probe.py, or a test constructing ModelHarness directly),
        # in which case pacing falls back to the pre-Phase-2 behavior:
        # sized from `settings.limit_tpm`, active only in "paced" mode.
        self.budget = budget
        self.pacer = pacer
        if self.pacer is None:
            if budget is not None and budget.tokens_per_minute is not None:
                # Sized regardless of `mode` — a "shadow" budget needs a
                # pacer active even for naive/runaway agents to measure
                # their overage; see `_pacing_action`.
                self.pacer = TokenBucket(target_tpm=budget.effective_tokens_per_minute)
            elif budget is None and self.mode == "paced":
                target_tpm = settings.limit_tpm * settings.paced_target_ratio
                self.pacer = TokenBucket(target_tpm=target_tpm)
        # Checked before every pacer wait, before every model call, and during
        # every retry/backoff sleep. None means "never stops" (e.g. the probe).
        self.stop_event = stop_event
        # Checked at the same points as stop_event, EXCEPT retry/backoff
        # sleeps — see WorkerInterrupted's docstring. Single-shot: consuming
        # it clears it, so only the call in flight when it fired is aborted.
        self.interrupt_event = interrupt_event

    def update_budget(self, budget: Optional[Budget]) -> None:
        """Called by the worker's poll loop on every tick with the freshly
        resolved Budget. Creates the pacer on the fly if one didn't exist
        yet (e.g. this worker started unbudgeted and an operator just
        assigned one) and keeps its target in sync either way — same
        "live re-target without resetting accumulated tokens" behavior
        Phase 0/1 already had for the flat `limit_tpm` bridge, now driven
        by the real Budget object.
        """
        self.budget = budget
        if budget is not None and budget.tokens_per_minute is not None:
            if self.pacer is None:
                self.pacer = TokenBucket(target_tpm=budget.effective_tokens_per_minute)
            else:
                self.pacer.update_target(budget.effective_tokens_per_minute)

    def _pacing_action(self) -> str:
        """Decides, per call, whether to block on the pacer, run it in
        shadow (measure-only) mode, or skip it entirely.

        `client_bucket` enforcement only actually blocks in "paced" mode —
        naive/runaway's whole point is ignoring the limit, and a Budget
        shouldn't silently override that scenario. `shadow` enforcement
        runs regardless of mode: it's specifically useful for measuring
        how far a naive/runaway agent overshoots a budget it's deliberately
        not obeying. See docs/phase-1-5-implementation-plan.md §Phase 2's
        integration note for the full reasoning.
        """
        if self.pacer is None:
            return "none"
        if self.budget is None:
            return "block" if self.mode == "paced" else "none"
        if self.budget.enforcement == "off":
            return "none"
        if self.budget.enforcement == "shadow":
            return "shadow"
        return "block" if self.mode == "paced" else "none"

    def _should_stop(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _raise_if_stopped(self) -> None:
        if self._should_stop():
            raise WorkerStopped(f"{self.agent_name}/{self.user_id} stopped")

    def _raise_if_interrupted(self) -> None:
        if self.interrupt_event is not None and self.interrupt_event.is_set():
            self.interrupt_event.clear()  # single-shot — don't abort the NEXT call too
            raise WorkerInterrupted(f"{self.agent_name}/{self.user_id} interrupted")

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep for `seconds`, but return immediately if `stop_event` fires —
        and then raise WorkerStopped so the caller's retry loop doesn't
        continue as if nothing happened. This is what makes exponential
        backoff (up to ~14s across 3 attempts) interruptible instead of a
        guaranteed wait before a Stop command takes effect.
        """
        if self.stop_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        # stop_event fired before the timeout
        self._raise_if_stopped()

    def _extract_ratelimit_headers(self, headers: httpx.Headers) -> Dict[str, str]:
        extracted = {}
        for key, val in headers.items():
            k_lower = key.lower()
            if "ratelimit" in k_lower or "retry-after" in k_lower or "limit" in k_lower:
                extracted[key] = val
        return extracted

    async def invoke_with_telemetry(
        self,
        messages: List[BaseMessage],
        node_name: str,
        run_id: str,
        estimated_input_tokens: int = 500,
        max_attempts: Optional[int] = None,
    ) -> Any:
        """Execute non-streamed LLM call with retry handling, pacing, and telemetry."""
        if max_attempts is None:
            max_attempts = 1 if self.mode == "runaway" else 3

        self._raise_if_stopped()
        self._raise_if_interrupted()

        # 1. Pacing check — interruptible: a Stop click during a pacing wait
        # returns here without having consumed bucket capacity.
        shadow_wait_ms = 0
        pacing_action = self._pacing_action()
        if pacing_action == "block":
            await self.pacer.acquire(estimated_input_tokens, stop_event=self.stop_event)
            self._raise_if_stopped()
        elif pacing_action == "shadow":
            shadow_wait_s = await self.pacer.acquire(estimated_input_tokens, shadow=True)
            shadow_wait_ms = int(shadow_wait_s * 1000)

        attempt = 0
        while attempt < max_attempts:
            self._raise_if_stopped()
            self._raise_if_interrupted()
            attempt += 1
            start_time = time.monotonic()
            status = 200
            prompt_tokens = 0
            completion_tokens = 0
            rl_headers = {}
            error_obj = None
            response = None

            try:
                base_url = settings.get_base_url()

                # We use httpx AsyncClient to capture exact raw response headers and status codes
                async with httpx.AsyncClient(timeout=60.0) as client:
                    payload = {
                        "model": settings.model_name,
                        "messages": messages_to_openai_payload(messages),
                        "stream": False,
                    }
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {settings.maas_api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    status = resp.status_code
                    rl_headers = self._extract_ratelimit_headers(resp.headers)

                    if resp.status_code == 429:
                        raise RateLimitExceeded("429 Too Many Requests", rl_headers)
                    resp.raise_for_status()

                    data = resp.json()
                    usage = data.get("usage", {})
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    response = data["choices"][0]["message"]["content"]

            except RateLimitExceeded as e:
                status = 429
                error_obj = e
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                rl_headers = self._extract_ratelimit_headers(e.response.headers)
                error_obj = e
            except Exception as e:
                status = 500
                error_obj = e

            latency_ms = int((time.monotonic() - start_time) * 1000)

            # Emit telemetry
            event = TelemetryEvent(
                user=self.user_id,
                team=self.team_id,
                agent=self.agent_name,
                mode=self.mode,
                run_id=run_id,
                node=node_name,
                status=status,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                ratelimit_headers=rl_headers,
                attempt=attempt,
                shadow_queue_wait_ms=shadow_wait_ms,
            )
            await telemetry.emit(event)

            if status == 200:
                return response

            if status == 429:
                if attempt >= max_attempts:
                    raise RateLimitExceeded(f"Rate limited after {attempt} attempts", rl_headers)

                # Retry strategy based on mode
                if self.mode == "naive":
                    # Naive: immediate retry
                    await self._interruptible_sleep(0.1)
                elif self.mode == "paced":
                    # Exponential backoff with jitter or Retry-After header
                    retry_after = rl_headers.get("retry-after") or rl_headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        backoff = float(retry_after)
                    else:
                        backoff = (2 ** attempt) + random.uniform(0.1, 1.0)
                    await self._interruptible_sleep(backoff)
                else:
                    # Runaway: no sleep
                    raise RateLimitExceeded("Runaway throttled", rl_headers)
            else:
                if error_obj:
                    raise error_obj
                raise RuntimeError(f"Request failed with status {status}")

    async def stream_with_telemetry(
        self,
        messages: List[BaseMessage],
        node_name: str,
        run_id: str,
        estimated_input_tokens: int = 500,
    ) -> AsyncIterator[str]:
        """Execute streamed LLM call with token accounting and telemetry."""
        self._raise_if_stopped()
        self._raise_if_interrupted()

        shadow_wait_ms = 0
        pacing_action = self._pacing_action()
        if pacing_action == "block":
            await self.pacer.acquire(estimated_input_tokens, stop_event=self.stop_event)
            self._raise_if_stopped()
        elif pacing_action == "shadow":
            shadow_wait_s = await self.pacer.acquire(estimated_input_tokens, shadow=True)
            shadow_wait_ms = int(shadow_wait_s * 1000)

        base_url = settings.get_base_url()

        start_time = time.monotonic()
        status = 200
        prompt_tokens = 0
        completion_tokens = 0
        rl_headers = {}
        full_content = []
        error_obj: Optional[BaseException] = None

        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "model": settings.model_name,
                "messages": messages_to_openai_payload(messages),
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            try:
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.maas_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as resp:
                    status = resp.status_code
                    rl_headers = self._extract_ratelimit_headers(resp.headers)
                    if resp.status_code == 429:
                        raise RateLimitExceeded("429 Too Many Requests", rl_headers)
                    resp.raise_for_status()

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            if "usage" in chunk and chunk["usage"]:
                                prompt_tokens = chunk["usage"].get("prompt_tokens", prompt_tokens)
                                completion_tokens = chunk["usage"].get("completion_tokens", completion_tokens)
                            if "choices" in chunk and chunk["choices"]:
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    full_content.append(content)
                                    yield content
                        except json.JSONDecodeError:
                            continue

            except RateLimitExceeded as e:
                status = 429
                error_obj = e
            except Exception as e:
                status = 500
                error_obj = e

            latency_ms = int((time.monotonic() - start_time) * 1000)

            # If usage wasn't returned in stream, estimate completion tokens roughly from content
            if completion_tokens == 0 and full_content:
                completion_tokens = max(1, len("".join(full_content)) // 4)
            if prompt_tokens == 0:
                prompt_tokens = estimated_input_tokens

            event = TelemetryEvent(
                user=self.user_id,
                team=self.team_id,
                agent=self.agent_name,
                mode=self.mode,
                run_id=run_id,
                node=node_name,
                status=status,
                prompt_tokens=prompt_tokens if status == 200 else 0,
                completion_tokens=completion_tokens if status == 200 else 0,
                latency_ms=latency_ms,
                ratelimit_headers=rl_headers,
                attempt=1,
                shadow_queue_wait_ms=shadow_wait_ms,
            )
            await telemetry.emit(event)

            # Phase 0 fix #0.6: previously this swallowed every exception and
            # just ended the generator, so callers (e.g. AssistantAgent's
            # `except RateLimitExceeded`) never saw a throttled/failed stream —
            # it looked identical to a successful empty response. Re-raise so
            # the caller can react.
            if error_obj is not None:
                raise error_obj
