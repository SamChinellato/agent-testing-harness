import asyncio
import time


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

    async def acquire(self, estimated_tokens: int = 500) -> None:
        """Acquire tokens before making a request. Sleeps if bucket lacks capacity."""
        if estimated_tokens <= 0:
            return

        while True:
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

            # Wait outside the lock so other tasks/threads aren't blocked
            await asyncio.sleep(min(wait_seconds, 2.0))
