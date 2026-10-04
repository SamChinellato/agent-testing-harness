"""Bounded-memory streaming percentile estimator.

A log-bucketed histogram (HDR-style): values fall into buckets whose
boundaries grow by a fixed ratio `gamma`, so the relative error of any
reported quantile is bounded by (gamma-1)/(gamma+1) regardless of how many
samples are recorded. With gamma=1.02 that's about 1%.

Memory is bounded by the number of distinct buckets touched, which for
latencies spanning 1ms to 1000s is a few hundred — not the number of
samples. That's the property the plan asked for (§4.4) without pulling in
a t-digest dependency.
"""
import math
from typing import Dict, Optional


class LogHistogram:
    def __init__(self, gamma: float = 1.02):
        if gamma <= 1.0:
            raise ValueError("gamma must be > 1")
        self.gamma = gamma
        self._log_gamma = math.log(gamma)
        self.buckets: Dict[int, int] = {}
        self.zero_count = 0  # values <= 0 (e.g. a 0ms latency from a failed call)
        self.count = 0
        self.total = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        if value <= 0:
            self.zero_count += 1
            return
        idx = math.ceil(math.log(value) / self._log_gamma)
        self.buckets[idx] = self.buckets.get(idx, 0) + 1

    @property
    def mean(self) -> Optional[float]:
        return self.total / self.count if self.count else None

    def quantile(self, q: float) -> Optional[float]:
        """q in [0, 1]. Returns None if empty."""
        if not 0.0 <= q <= 1.0:
            raise ValueError("q must be in [0, 1]")
        if self.count == 0:
            return None
        target = max(1, math.ceil(q * self.count))
        seen = self.zero_count
        if seen >= target:
            return 0.0
        last_value = 0.0
        for idx in sorted(self.buckets):
            seen += self.buckets[idx]
            # Representative value for bucket (gamma^(idx-1), gamma^idx]:
            # the point minimising worst-case relative error inside it.
            last_value = 2 * self.gamma ** idx / (self.gamma + 1)
            if seen >= target:
                return last_value
        return last_value
