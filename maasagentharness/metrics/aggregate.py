"""Per-worker (or per-run) aggregate over TelemetryEvents.

The same class backs two things: the dashboard's live MetricsStore (for
/metrics) and the on-disk run artifact (summary.json), so the two can never
disagree about what a p95 means.
"""
from collections import Counter
from typing import Any, Dict, Optional

from maasagentharness.metrics.histogram import LogHistogram

QUANTILES = (0.5, 0.9, 0.95, 0.99)


class Aggregate:
    def __init__(self) -> None:
        self.requests = 0
        self.status_counts: Counter = Counter()
        self.tokens_in = 0
        self.tokens_out = 0
        self.total_ms = LogHistogram()
        self.ttft_ms = LogHistogram()
        # ITL is reported per request as a mean; we aggregate those means.
        self._itl_sum = 0.0
        self._itl_n = 0

    def add(self, ev: Any) -> None:
        """`ev` is any object with: status, prompt_tokens, completion_tokens,
        latency_ms, and optionally ttft_ms / itl_ms_mean.
        """
        self.requests += 1
        self.status_counts[int(ev.status)] += 1
        if ev.status == 200:
            self.tokens_in += int(ev.prompt_tokens)
            self.tokens_out += int(ev.completion_tokens)
        self.total_ms.add(float(ev.latency_ms))
        ttft = getattr(ev, "ttft_ms", None)
        if ttft is not None:
            self.ttft_ms.add(float(ttft))
        itl = getattr(ev, "itl_ms_mean", None)
        if itl is not None:
            self._itl_sum += float(itl)
            self._itl_n += 1

    @property
    def throttled(self) -> int:
        return self.status_counts.get(429, 0)

    @property
    def itl_mean(self) -> Optional[float]:
        return self._itl_sum / self._itl_n if self._itl_n else None

    def to_dict(self) -> Dict[str, Any]:
        def q_dict(h: LogHistogram) -> Dict[str, Optional[float]]:
            return {f"p{int(q * 100)}": _round(h.quantile(q)) for q in QUANTILES}

        return {
            "requests": self.requests,
            "status_counts": {str(k): v for k, v in sorted(self.status_counts.items())},
            "throttled": self.throttled,
            "throttle_rate": (self.throttled / self.requests) if self.requests else 0.0,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_ms": q_dict(self.total_ms),
            "ttft_ms": q_dict(self.ttft_ms),
            "itl_ms_mean": _round(self.itl_mean),
        }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)
