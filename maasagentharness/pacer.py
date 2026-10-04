import asyncio
import time
from typing import Optional


class TokenBucket:
    """Client-side token bucket rate limiter.

    Refills smoothly per second based on target tokens per minute.
    """
    def __init__(self, target_tpm: float):
        self.capacity = float(target_tpm)
        self.tokens = float(target_tpm)
        self.refill_rate = float(target_tpm) / 60.0  # tokens per second
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    def update_target(self, target_tpm: float):
        self.capacity = float(target_tpm)
        self.refill_rate = float(target_tpm) / 60.0

    async def acquire(self, estimated_tokens: int = 500, stop_event: Optional[asyncio.Event] = None) -> None:
        """Acquire tokens before making a request. Sleeps if bucket lacks capacity.

        If `stop_event` is given and becomes set while waiting, this returns
        immediately WITHOUT having acquired the tokens — the caller (ModelHarness)
        is responsible for checking `stop_event` right after this returns and
        aborting the call rather than treating it as a normal acquire. This keeps
        the pacer itself free of any notion of "stopped", matching the harness's
        job of owning cancellation.
        """
        if estimated_tokens <= 0:
            return

        while True:
            if stop_event is not None and stop_event.is_set():
                return

            async with self._lock:
                now = time.monotonic()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)

                if self.tokens >= estimated_tokens:
                    self.tokens -= estimated_tokens
                    return

                # Calculate wait time needed for enough tokens to accumulate
                needed = estimated_tokens - self.tokens
                wait_seconds = needed / self.refill_rate if self.refill_rate > 0 else 1.0

            # Wait outside the lock so other tasks/threads aren't blocked.
            # Race the wait against stop_event so a Stop click doesn't sit here
            # for up to 2s — it returns (unstopped path) the moment either the
            # timeout elapses or the event fires.
            timeout = min(wait_seconds, 2.0)
            if stop_event is not None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                # Loop back around: if the event fired, the top-of-loop check
                # above returns immediately on the next iteration.
            else:
                await asyncio.sleep(timeout)
