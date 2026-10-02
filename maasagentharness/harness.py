import asyncio
import os
import random
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union
import httpx
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage

from maasagentharness.config import settings
from maasagentharness.pacer import TokenBucket
from maasagentharness.telemetry import TelemetryEvent, telemetry


class RateLimitExceeded(Exception):
    """Raised when 429 is received and retries are exhausted."""
    def __init__(self, message: str, headers: Dict[str, str]):
        super().__init__(message)
        self.headers = headers


class ModelHarness:
    """Shared harness for LLM interaction, token-bucket pacing, retry policy, and telemetry."""

    def __init__(
        self,
        user_id: Optional[str] = None,
        team_id: Optional[str] = None,
        agent_name: str = "general",
        mode: str = "naive",
        pacer: Optional[TokenBucket] = None,
    ):
        self.user_id = user_id or settings.user_id
        self.team_id = team_id or settings.team_id
        self.agent_name = agent_name
        self.mode = mode  # naive, paced, runaway
        self.pacer = pacer
        if self.pacer is None and self.mode == "paced":
            target_tpm = settings.limit_tpm * settings.paced_target_ratio
            self.pacer = TokenBucket(target_tpm=target_tpm)

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

        # 1. Pacing check
        if self.mode == "paced" and self.pacer is not None:
            await self.pacer.acquire(estimated_input_tokens)

        attempt = 0
        while attempt < max_attempts:
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
                        "messages": [
                            {"role": m.type if m.type != "human" else "user", "content": m.content}
                            for m in messages
                        ],
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
                    await asyncio.sleep(0.1)
                elif self.mode == "paced":
                    # Exponential backoff with jitter or Retry-After header
                    retry_after = rl_headers.get("retry-after") or rl_headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        backoff = float(retry_after)
                    else:
                        backoff = (2 ** attempt) + random.uniform(0.1, 1.0)
                    await asyncio.sleep(backoff)
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
        if self.mode == "paced" and self.pacer is not None:
            await self.pacer.acquire(estimated_input_tokens)

        base_url = settings.get_base_url()

        start_time = time.monotonic()
        status = 200
        prompt_tokens = 0
        completion_tokens = 0
        rl_headers = {}
        full_content = []

        async with httpx.AsyncClient(timeout=60.0) as client:
            payload = {
                "model": settings.model_name,
                "messages": [
                    {"role": m.type if m.type != "human" else "user", "content": m.content}
                    for m in messages
                ],
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

                    import json
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

            except RateLimitExceeded:
                status = 429
            except Exception:
                status = 500

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
            )
            await telemetry.emit(event)
