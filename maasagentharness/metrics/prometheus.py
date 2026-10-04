"""GET /metrics — Prometheus text exposition format, rendered from MetricsStore.

Latency quantiles are exposed as a Prometheus *summary* with pre-computed
quantiles (the histogram's own estimates), not as a bucketed histogram —
the quantiles are what the plan asked for (§4.6), and a summary is the
honest type for them.
"""
from typing import Iterable, List, Tuple

from maasagentharness.metrics.aggregate import QUANTILES
from maasagentharness.metrics.store import MetricsStore


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(pairs: Iterable[Tuple[str, str]]) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in pairs)
    return f"{{{inner}}}" if inner else ""


def _fmt(value) -> str:
    if value is None:
        return "NaN"
    if isinstance(value, float):
        return repr(round(value, 4))
    return str(value)


def render(store: MetricsStore) -> str:
    lines: List[str] = []

    def family(name: str, kind: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")

    family("agent_harness_requests_total", "counter", "Model calls, by worker and HTTP status.")
    for worker, agg in sorted(store.by_worker.items()):
        for status, count in sorted(agg.status_counts.items()):
            lines.append(f"agent_harness_requests_total{_labels([('worker', worker), ('status', str(status))])} {count}")

    family("agent_harness_tokens_total", "counter", "Tokens consumed, by worker and direction.")
    for worker, agg in sorted(store.by_worker.items()):
        lines.append(f"agent_harness_tokens_total{_labels([('worker', worker), ('direction', 'in')])} {agg.tokens_in}")
        lines.append(f"agent_harness_tokens_total{_labels([('worker', worker), ('direction', 'out')])} {agg.tokens_out}")

    family("agent_harness_request_duration_ms", "summary", "End-to-end model call latency in milliseconds.")
    for worker, agg in sorted(store.by_worker.items()):
        for q in QUANTILES:
            value = agg.total_ms.quantile(q)
            lines.append(f"agent_harness_request_duration_ms{_labels([('worker', worker), ('quantile', str(q))])} {_fmt(value)}")
        lines.append(f"agent_harness_request_duration_ms_count{_labels([('worker', worker)])} {agg.total_ms.count}")
        lines.append(f"agent_harness_request_duration_ms_sum{_labels([('worker', worker)])} {_fmt(round(agg.total_ms.total, 2))}")

    family("agent_harness_ttft_ms", "summary", "Time to first streamed token in milliseconds (streaming calls only).")
    for worker, agg in sorted(store.by_worker.items()):
        for q in QUANTILES:
            value = agg.ttft_ms.quantile(q)
            lines.append(f"agent_harness_ttft_ms{_labels([('worker', worker), ('quantile', str(q))])} {_fmt(value)}")
        lines.append(f"agent_harness_ttft_ms_count{_labels([('worker', worker)])} {agg.ttft_ms.count}")

    family("agent_harness_dropped_iterations_total", "counter", "Open-model iterations dropped because max_vus was saturated.")
    for worker, total in sorted(store.dropped_by_worker.items()):
        lines.append(f"agent_harness_dropped_iterations_total{_labels([('worker', worker)])} {total}")

    family("agent_harness_clock_skew_ms", "gauge", "Largest observed |ingest - agent| clock skew in milliseconds.")
    for worker, skew in sorted(store.clock_skew_ms.items()):
        lines.append(f"agent_harness_clock_skew_ms{_labels([('worker', worker)])} {skew}")

    return "\n".join(lines) + "\n"
