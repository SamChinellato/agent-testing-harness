"""Phase 4 tests: streaming percentiles, aggregates, Prometheus rendering,
run artifacts (incl. a simulated crash), server-side ingest timestamps,
the /metrics route, heartbeat-reported drops, TTFT/ITL against a fake
streaming endpoint with known delays, and the persistent HTTP client.
"""
import asyncio
import json
import random
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage

from maasagentharness.config import settings
from maasagentharness.control.store import WorkerStore
from maasagentharness.control.supervisor import Supervisor
from maasagentharness.dashboard import app as dashboard_app
from maasagentharness.harness import ModelHarness
from maasagentharness.metrics import Aggregate, LogHistogram, MetricsStore, RunArtifact, render_prometheus
from maasagentharness.metrics.store import default_store
from maasagentharness.telemetry import TelemetryEvent, telemetry


# ---------------------------------------------------------------------------
# LogHistogram — bounded-memory percentiles
# ---------------------------------------------------------------------------

def _exact_quantile(values, q):
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(-(-q * len(ordered) // 1)) - 1))
    return ordered[idx]


def test_histogram_quantiles_within_relative_error_of_exact():
    rng = random.Random(7)
    # Lognormal-ish latencies: a realistic long-tailed shape.
    values = [rng.lognormvariate(6.0, 0.8) for _ in range(20_000)]
    h = LogHistogram(gamma=1.02)
    for v in values:
        h.add(v)
    for q in (0.5, 0.9, 0.95, 0.99):
        exact = _exact_quantile(values, q)
        estimate = h.quantile(q)
        assert estimate == pytest.approx(exact, rel=0.02), f"q={q}"


def test_histogram_memory_is_bounded_by_buckets_not_samples():
    h = LogHistogram()
    for i in range(200_000):
        h.add(float((i % 5000) + 1))  # 1..5000 ms
    assert h.count == 200_000
    assert len(h.buckets) < 400  # a few hundred buckets, not 200k entries


def test_histogram_empty_and_zero_values():
    h = LogHistogram()
    assert h.quantile(0.5) is None
    h.add(0.0)
    h.add(0.0)
    assert h.quantile(0.5) == 0.0


def test_histogram_rejects_bad_quantile():
    with pytest.raises(ValueError):
        LogHistogram().quantile(1.5)


# ---------------------------------------------------------------------------
# Aggregate — TTFT None is distinct from zero
# ---------------------------------------------------------------------------

def _ev_dict(**kw):
    base = dict(status=200, prompt_tokens=10, completion_tokens=5, latency_ms=100, ttft_ms=None, itl_ms_mean=None)
    base.update(kw)
    return base


def _ev(**kw):
    return SimpleNamespace(**_ev_dict(**kw))


def test_aggregate_counts_and_tokens_only_for_success():
    agg = Aggregate()
    agg.add(_ev())
    agg.add(_ev(status=429, prompt_tokens=0, completion_tokens=0))
    d = agg.to_dict()
    assert d["requests"] == 2
    assert d["status_counts"] == {"200": 1, "429": 1}
    assert d["throttled"] == 1
    assert d["tokens_in"] == 10 and d["tokens_out"] == 5


def test_aggregate_ttft_not_applicable_is_not_zero():
    agg = Aggregate()
    agg.add(_ev(ttft_ms=None))  # non-streamed call
    assert agg.ttft_ms.count == 0
    assert agg.to_dict()["ttft_ms"]["p50"] is None


# ---------------------------------------------------------------------------
# Prometheus exposition
# ---------------------------------------------------------------------------

def test_prometheus_render_contains_expected_series():
    store = MetricsStore()
    store.ingest(SimpleNamespace(user="w1", **_ev_dict(latency_ms=250)), agent_ts_ms=None)
    store.set_dropped("w1", 3)
    text = render_prometheus(store)
    assert 'agent_harness_requests_total{worker="w1",status="200"} 1' in text
    assert 'agent_harness_tokens_total{worker="w1",direction="in"} 10' in text
    assert 'agent_harness_request_duration_ms{worker="w1",quantile="0.95"}' in text
    assert 'agent_harness_dropped_iterations_total{worker="w1"} 3' in text
    assert "# TYPE agent_harness_request_duration_ms summary" in text


def test_prometheus_label_values_are_escaped():
    store = MetricsStore()
    store.set_dropped('bad"worker\\x', 1)
    text = render_prometheus(store)
    assert 'worker="bad\\"worker\\\\x"' in text


# ---------------------------------------------------------------------------
# RunArtifact — crash tolerance
# ---------------------------------------------------------------------------

def test_artifact_writes_scenario_records_and_summary(tmp_path):
    art = RunArtifact.create(
        root=tmp_path, run_name="demo", scenario_name="steady",
        scenario_config={"agent": "triage"}, worker_id="demo-steady",
    )
    art.record(_ev_dict(user="demo-steady", node="classify", ts_ingest_ms=1, ts="x", seq=0, mode="naive",
                        agent="triage", run_id="r", team="t", attempt=1, ratelimit_headers={}, shadow_queue_wait_ms=0))
    art.write_summary(executor_stats={"iterations_started": 1}, finished=True)
    art.close()

    assert (art.directory / "scenario.json").exists()
    lines = (art.directory / "requests.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["node"] == "classify"
    summary = json.loads((art.directory / "summary.json").read_text())
    assert summary["finished"] is True
    assert summary["requests"]["requests"] == 1


def test_artifact_survives_a_simulated_crash(tmp_path):
    """Never closed, never finished: every flushed record must still parse,
    and the last periodic summary must still be valid JSON."""
    art = RunArtifact.create(
        root=tmp_path, run_name="crash", scenario_name="s",
        scenario_config={}, worker_id="crash-s",
    )
    for i in range(5):
        art.record(_ev_dict(user="crash-s", node=f"n{i}", ts_ingest_ms=1, ts="x", seq=i, mode="naive",
                            agent="triage", run_id="r", team="t", attempt=1, ratelimit_headers={},
                            shadow_queue_wait_ms=0))
    art.write_summary(executor_stats=None, finished=False)
    # ... process dies here, no close(), no finished summary ...

    for line in (art.directory / "requests.jsonl").read_text().splitlines():
        json.loads(line)  # every line is complete, valid JSON
    summary = json.loads((art.directory / "summary.json").read_text())
    assert summary["finished"] is False
    assert summary["requests"]["requests"] == 5  # summary reflects everything recorded before it was written


# ---------------------------------------------------------------------------
# Dashboard: ingest timestamps, /metrics route, heartbeat-reported drops
# ---------------------------------------------------------------------------

@pytest.fixture
async def dash_client(tmp_path):
    default_store.reset()
    dashboard_app.events_buffer.clear()
    s = WorkerStore(str(tmp_path / "dash.db"))
    await s.connect()
    dashboard_app.app.state.worker_store = s
    dashboard_app.app.state.supervisor = Supervisor(s)
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await s.close()
    default_store.reset()
    dashboard_app.events_buffer.clear()


def _event_payload(**overrides):
    base = {
        "ts": "2020-01-01T00:00:00",  # deliberately ancient — the agent's clock is wrong
        "user": "metrics-w1", "team": "t", "agent": "triage", "mode": "naive",
        "run_id": "r1", "node": "classify", "status": 200,
        "prompt_tokens": 100, "completion_tokens": 20, "latency_ms": 300, "attempt": 1,
    }
    base.update(overrides)
    return base


async def test_events_are_stamped_with_server_ingest_time(dash_client):
    before = int(time.time() * 1000)
    resp = await dash_client.post("/events", json=_event_payload())
    assert resp.status_code == 200
    ev = dashboard_app.events_buffer[-1]
    assert ev.ts_ingest_ms >= before
    # The agent's year-2020 timestamp must not drag the event out of the window.
    assert ev in dashboard_app.events_buffer
    assert default_store.clock_skew_ms["metrics-w1"] > 1_000_000_000  # skew measured, not trusted


async def test_metrics_route_serves_prometheus_text(dash_client):
    await dash_client.post("/events", json=_event_payload())
    await dash_client.post("/events", json=_event_payload(status=429, prompt_tokens=0, completion_tokens=0))
    resp = await dash_client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'agent_harness_requests_total{worker="metrics-w1",status="429"} 1' in resp.text
    assert 'agent_harness_tokens_total{worker="metrics-w1",direction="in"} 100' in resp.text


@pytest.fixture
async def worker_store(tmp_path):
    s = WorkerStore(str(tmp_path / "hb.db"))
    await s.connect()
    dashboard_app.app.state.worker_store = s
    dashboard_app.app.state.supervisor = Supervisor(s)
    yield s
    await s.close()


async def test_heartbeat_reports_dropped_iterations_into_metrics(worker_store):
    transport = httpx.ASGITransport(app=dashboard_app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        await c.post("/api/workers", json={"worker_id": "hb-w", "team": "t"})
        await c.post("/api/workers/hb-w/heartbeat", json={
            "applied_generation": 0, "phase": "running", "dropped_iterations_total": 7,
        })
        text = (await c.get("/metrics")).text
    assert 'agent_harness_dropped_iterations_total{worker="hb-w"} 7' in text


# ---------------------------------------------------------------------------
# Harness: TTFT / ITL against a fake streaming endpoint with known delays
# ---------------------------------------------------------------------------

class _TimedStreamResponse:
    status_code = 200
    headers = httpx.Headers({})

    def __init__(self, first_delay: float, gap: float, chunks: int):
        self._first = first_delay
        self._gap = gap
        self._chunks = chunks

    async def aiter_lines(self):
        await asyncio.sleep(self._first)
        for i in range(self._chunks):
            if i:
                await asyncio.sleep(self._gap)
            payload = {"choices": [{"delta": {"content": f"t{i} "}}]}
            yield f"data: {json.dumps(payload)}"
        yield 'data: {"usage": {"prompt_tokens": 12, "completion_tokens": 6}}'
        yield "data: [DONE]"

    def raise_for_status(self):
        pass


class _TimedStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


async def test_ttft_and_itl_measured_against_known_delays(monkeypatch):
    first_delay, gap = 0.12, 0.06
    resp = _TimedStreamResponse(first_delay=first_delay, gap=gap, chunks=5)

    def fake_stream(self, method, url, headers=None, json=None):
        return _TimedStreamCtx(resp)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)
    monkeypatch.setattr(settings, "maas_endpoint", "https://example.invalid")

    captured = []

    async def capture(event: TelemetryEvent):
        captured.append(event)

    telemetry.add_listener(capture)
    try:
        harness = ModelHarness(agent_name="t", mode="naive")
        out = []
        async for token in harness.stream_with_telemetry([HumanMessage(content="hi")], "chat", "r1"):
            out.append(token)
        await harness.aclose()
    finally:
        telemetry.remove_listener(capture)

    assert len(out) == 5
    event = captured[-1]
    assert event.ttft_ms == pytest.approx(first_delay * 1000, abs=40)
    assert event.itl_ms_mean == pytest.approx(gap * 1000, abs=25)


async def test_non_streamed_calls_report_ttft_as_none():
    """invoke_with_telemetry can't measure first-token time; its events must
    say None, not 0."""
    captured = []

    async def capture(event: TelemetryEvent):
        captured.append(event)

    telemetry.add_listener(capture)
    try:
        harness = ModelHarness(agent_name="t", mode="naive")
        # No endpoint configured -> the call fails fast with status 500, emitting an event.
        monkeypatch_endpoint = settings.maas_endpoint
        settings.maas_endpoint = ""
        try:
            with pytest.raises(Exception):
                await harness.invoke_with_telemetry([HumanMessage(content="hi")], "n", "r1")
        finally:
            settings.maas_endpoint = monkeypatch_endpoint
        await harness.aclose()
    finally:
        telemetry.remove_listener(capture)

    assert captured and captured[-1].ttft_ms is None and captured[-1].itl_ms_mean is None


async def test_harness_reuses_one_http_client_and_aclose_resets_it():
    harness = ModelHarness(agent_name="t", mode="naive")
    c1 = harness._client()
    c2 = harness._client()
    assert c1 is c2  # persistent — not a fresh client per call (finding #24)
    await harness.aclose()
    assert harness._http_client is None
    assert harness._client() is not c1
    await harness.aclose()
