"""Process lifecycle for managed workers.

This is what makes Start/Kill real operations instead of hopeful dict
writes (redesign-spec.md finding #4/#6): for a `managed=true` worker, the
dashboard process is the parent of the worker process, so it can actually
start one on demand and actually send it a signal, rather than writing a
desired value and hoping some already-running process notices.

Attached (managed=false) workers — the current `uv run agent` workflow —
are unaffected: nothing here runs for them, and `kill()`/`spawn()` refuse to
touch a worker record this process didn't start.
"""
import asyncio
import os
import signal
import sys
from typing import Dict, List, Optional

from maasagentharness.control.store import WorkerStore

KILL_TIMEOUT = 10.0


class ManagedWorkerError(Exception):
    pass


class Supervisor:
    def __init__(self, store: WorkerStore):
        self.store = store
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._reaper_task: Optional[asyncio.Task] = None

    async def spawn(
        self,
        worker_id: str,
        env_overrides: Dict[str, str],
        module: str = "maasagentharness.runner",
        args: Optional[List[str]] = None,
    ) -> int:
        """Spawn `python -m <module> <args>` as a child process with the
        given environment overrides (USER_ID, DASHBOARD_URL, etc).
        Returns the PID. Raises ManagedWorkerError if already running.

        `module`/`args` default to the plain `uv run agent` worker
        (`maasagentharness.runner`, no args). Phase 3's `POST /api/runs`
        passes `module="maasagentharness.scenario.cli"` with
        `args=["run", scenario_path, "--scenario", name]` instead, so one
        scenario name becomes one independently manageable/killable
        worker process, same as any other managed worker.
        """
        if worker_id in self._processes and self._processes[worker_id].returncode is None:
            raise ManagedWorkerError(f"worker '{worker_id}' is already running (managed)")

        env = {**os.environ, **env_overrides}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", module, *(args or []),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._processes[worker_id] = proc
        await self.store.set_pid(worker_id, proc.pid)
        return proc.pid

    async def kill(self, worker_id: str) -> None:
        """SIGTERM, wait up to KILL_TIMEOUT, then SIGKILL. Raises
        ManagedWorkerError if this supervisor never spawned the process —
        callers should translate that into a 409, not attempt a kill on a
        PID this process doesn't own.
        """
        proc = self._processes.get(worker_id)
        if proc is None:
            raise ManagedWorkerError(f"worker '{worker_id}' is not managed by this supervisor")

        if proc.returncode is not None:
            return  # already exited

        proc.send_signal(signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

        await self.store.set_pid(worker_id, None)

    def is_managed_by_this_process(self, worker_id: str) -> bool:
        return worker_id in self._processes

    async def start_reaper(self, interval: float = 2.0) -> None:
        """Background loop: notice managed processes that died on their own
        (crash, OOM, etc — not via our own kill()) and mark them `lost`
        rather than leaving a stale 'running' record that nothing will ever
        correct, since the heartbeat that would normally update it has
        stopped arriving along with the process.
        """
        self._reaper_task = asyncio.current_task()
        while True:
            for worker_id, proc in list(self._processes.items()):
                if proc.returncode is not None:
                    # Process exited. If we didn't do it via kill() (which
                    # already cleans up pid), record it as lost.
                    record = await self.store.get(worker_id)
                    if record is not None and record.pid is not None:
                        await self.store.mark_lost(
                            worker_id, f"managed process exited unexpectedly (code {proc.returncode})"
                        )
                    del self._processes[worker_id]
            await asyncio.sleep(interval)

    def stop_reaper(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
