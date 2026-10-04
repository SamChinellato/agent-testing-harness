"""Worker-side HTTP client for the Phase 1 control API.

Kept separate from AgentWorker so the HTTP transport can be tested (or
swapped — e.g. for an in-process ASGI transport in tests) independently of
the worker's run loop and stop/interrupt logic.
"""
from typing import Optional

import httpx

from maasagentharness.control.models import DesiredStateResponse


class ControlClient:
    def __init__(
        self,
        dashboard_url: str,
        worker_id: str,
        team_id: str,
        display_name: str = "",
        task_description: str = "",
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base = dashboard_url.rstrip("/")
        self.worker_id = worker_id
        self.team_id = team_id
        self.display_name = display_name
        self.task_description = task_description
        # None in production (real sockets). Tests pass an
        # httpx.ASGITransport bound to the dashboard's FastAPI app, so a
        # full AgentWorker can be driven against it with no real network
        # calls and no port to bind.
        self.transport = transport

    async def register(self, agent: str, mode: str, managed: bool = False) -> None:
        async with httpx.AsyncClient(timeout=3.0, transport=self.transport) as client:
            try:
                await client.post(
                    f"{self.base}/api/workers",
                    json={
                        "worker_id": self.worker_id,
                        "team": self.team_id,
                        "display_name": self.display_name,
                        "task_description": self.task_description,
                        "managed": managed,
                        "agent": agent,
                        "mode": mode,
                    },
                )
                print(f"[WORKER] Registered '{self.display_name}' ({self.worker_id}) with dashboard (v2 API).", flush=True)
            except Exception as e:
                print(f"[WORKER] Could not register with dashboard: {e}", flush=True)

    async def get_desired(self) -> DesiredStateResponse:
        async with httpx.AsyncClient(timeout=1.5, transport=self.transport) as client:
            try:
                resp = await client.get(f"{self.base}/api/workers/{self.worker_id}/desired")
                if resp.status_code == 200:
                    return DesiredStateResponse(**resp.json())
            except Exception:
                pass  # dashboard unreachable — treat as unregistered/stopped, never as "running"
        return DesiredStateResponse(unregistered=True)

    async def heartbeat(
        self,
        applied_generation: int,
        phase: str,
        current_node: Optional[str],
        iteration: int,
        last_error: Optional[str],
        applied_interrupt_token: int = 0,
    ) -> Optional[float]:
        async with httpx.AsyncClient(timeout=1.5, transport=self.transport) as client:
            try:
                resp = await client.post(
                    f"{self.base}/api/workers/{self.worker_id}/heartbeat",
                    json={
                        "applied_generation": applied_generation,
                        "phase": phase,
                        "current_node": current_node,
                        "iteration": iteration,
                        "last_error": last_error,
                        "applied_interrupt_token": applied_interrupt_token,
                    },
                )
                if resp.status_code == 200:
                    return resp.json().get("lease_expires_at")
            except Exception:
                pass
        return None
