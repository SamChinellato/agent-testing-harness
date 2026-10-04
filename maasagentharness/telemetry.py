import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx
from pydantic import BaseModel, Field


class TelemetryEvent(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now().isoformat())
    # Assigned by the dashboard on ingest (0 until then). Monotonically
    # increasing, so the UI can key log rows by `seq` instead of array index —
    # see docs/phase-0-hotfixes.md #0.9. Agents never need to set this.
    seq: int = 0
    user: str
    team: str
    agent: str
    mode: str
    run_id: str
    node: str
    status: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    ratelimit_headers: Dict[str, str] = Field(default_factory=dict)
    attempt: int = 1
    # Phase 2 — Budget.enforcement == "shadow": how long a real, enforcing
    # pacer WOULD have delayed this call, measured without actually
    # delaying it. 0 whenever shadow accounting isn't active for this call.
    shadow_queue_wait_ms: int = 0


class TelemetryEmitter:
    def __init__(self, dashboard_url: Optional[str] = None):
        self.dashboard_url = dashboard_url or os.getenv("DASHBOARD_URL", "http://localhost:8000")
        self._http_client: Optional[httpx.AsyncClient] = None
        self._listeners: list = []

    def add_listener(self, callback) -> None:
        """Register a coroutine callback(event) called after every emit."""
        self._listeners.append(callback)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=2.0)
        return self._http_client

    async def emit(self, event: TelemetryEvent) -> None:
        # 1. Print structured JSON to stdout
        event_dict = event.model_dump()
        print(f"[TELEMETRY] {json.dumps(event_dict)}", flush=True)

        # 2. Forward to dashboard if reachable
        if self.dashboard_url:
            try:
                client = await self._get_client()
                await client.post(f"{self.dashboard_url.rstrip('/')}/events", json=event_dict)
            except Exception:
                # Telemetry failure should never crash the agent execution
                pass

        for cb in self._listeners:
            try:
                await cb(event)
            except Exception:
                pass

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()


# Global telemetry emitter singleton
telemetry = TelemetryEmitter()
