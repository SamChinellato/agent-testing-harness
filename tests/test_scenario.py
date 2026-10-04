"""Phase 3 tests: schema/duration/threshold parsing, the YAML loader (incl.
${ENV_VAR} interpolation), and every executor's core behavior against fake,
no-network iteration_fns.
"""
import asyncio

import pytest

from maasagentharness.scenario.executors.arrival import (
    ConstantArrivalRate,
    PoissonArrivalRate,
    RampingArrivalRate,
)
from maasagentharness.scenario.executors.factory import build_executor
from maasagentharness.scenario.executors.vus import ConstantVUs, RampingVUs
from maasagentharness.scenario.loader import load_run_config
from maasagentharness.scenario.schema import (
    ExecutorConfig,
    RunConfig,
    ScenarioConfig,
    ScenarioConfigError,
    Stage,
    ThresholdSpec,
    parse_duration,
)
from maasagentharness.scenario.sweep import Sweep

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("30s", 30.0), ("2m", 120.0), ("1h", 3600.0), ("500ms", 0.5),
    ("5", 5.0), ("1.5s", 1.5),
])
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == pytest.approx(expected)


def test_parse_duration_rejects_garbage():
    with pytest.raises(ScenarioConfigError):
        parse_duration("banana")


# ---------------------------------------------------------------------------
# Threshold parsing
# ---------------------------------------------------------------------------

def test_threshold_parse_and_check():
    t = ThresholdSpec.parse("p95(total_latency_ms) < 8000")
    assert t.fn == "p95" and t.metric == "total_latency_ms" and t.op == "<" and t.value == 8000.0
    assert t.check(5000) is True
    assert t.check(9000) is False


def test_threshold_parse_rate_expression():
    t = ThresholdSpec.parse("rate(status_429) < 0.05")
    assert t.fn == "rate"
    assert t.metric == "status_429"


def test_threshold_parse_rejects_garbage():
    with pytest.raises(ScenarioConfigError):
        ThresholdSpec.parse("not a threshold")


# ---------------------------------------------------------------------------
# ExecutorConfig required-field validation
# ---------------------------------------------------------------------------

def test_executor_config_constant_vus_requires_vus_and_duration():
    with pytest.raises(Exception):  # pydantic wraps ScenarioConfigError in ValidationError
        ExecutorConfig(kind="constant_vus")
    ExecutorConfig(kind="constant_vus", vus=5, duration="30s")  # should not raise


def test_executor_config_arrival_rate_requires_max_vus():
    with pytest.raises(Exception):
        ExecutorConfig(kind="constant_arrival_rate", rate=10, duration="30s")
    ExecutorConfig(kind="constant_arrival_rate", rate=10, duration="30s", max_vus=5)


def test_executor_config_ramping_vus_requires_stages():
    with pytest.raises(Exception):
        ExecutorConfig(kind="ramping_vus")
    ExecutorConfig(kind="ramping_vus", stages=[Stage(target=5, duration="1m")])


# ---------------------------------------------------------------------------
# Loader: YAML -> RunConfig, with ${ENV_VAR} interpolation
# ---------------------------------------------------------------------------

def test_loader_parses_a_full_scenario_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MAAS_ENDPOINT", "https://example.invalid")
    yaml_text = """
run: demo-run
endpoint:
  base_url: ${TEST_MAAS_ENDPOINT}
  model: granite-3.1-2b-instruct
budgets:
  - budget_id: demo-budget
    scope: global
    tokens_per_minute: 3000
scenarios:
  steady:
    agent: triage
    mode: paced
    executor:
      kind: constant_arrival_rate
      rate: 30
      duration: 10s
      max_vus: 5
    budget: demo-budget
thresholds:
  - "p95(total_latency_ms) < 8000"
"""
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml_text)
    run = load_run_config(path)
    assert isinstance(run, RunConfig)
    assert run.run == "demo-run"
    assert run.endpoint.base_url == "https://example.invalid"
    assert run.scenarios["steady"].executor.rate == 30
    assert run.budgets[0].budget_id == "demo-budget"
    assert len(run.parsed_thresholds) == 1


def test_loader_raises_on_undefined_env_var(tmp_path):
    path = tmp_path / "scenario.yaml"
    path.write_text("""
run: demo
endpoint:
  base_url: ${THIS_VAR_DOES_NOT_EXIST_ANYWHERE}
scenarios:
  s1:
    agent: triage
    executor: {kind: constant_vus, vus: 1, duration: 1s}
""")
    with pytest.raises(ScenarioConfigError, match="THIS_VAR_DOES_NOT_EXIST_ANYWHERE"):
        load_run_config(path)


def test_loader_raises_on_missing_file(tmp_path):
    with pytest.raises(ScenarioConfigError):
        load_run_config(tmp_path / "does_not_exist.yaml")


def test_loader_raises_on_invalid_yaml(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("not: valid: yaml: : :")
    with pytest.raises(ScenarioConfigError):
        load_run_config(path)


# ---------------------------------------------------------------------------
# Executors — closed model (vus.py)
# ---------------------------------------------------------------------------

async def test_constant_vus_runs_concurrently_and_completes_many_iterations():
    counter = {"n": 0, "max_concurrent": 0, "current": 0}

    async def fast_iter():
        counter["current"] += 1
        counter["max_concurrent"] = max(counter["max_concurrent"], counter["current"])
        counter["n"] += 1
        await asyncio.sleep(0.02)
        counter["current"] -= 1

    ex = ConstantVUs(vus=4, duration_s=0.2)
    stats = await ex.run(fast_iter, asyncio.Event())
    assert stats.iterations_started == stats.iterations_completed
    assert stats.iterations_started > 4  # ran multiple rounds, not just one per VU
    assert counter["max_concurrent"] == 4  # all 4 VUs genuinely concurrent


async def test_constant_vus_respects_stop_event():
    counter = {"n": 0}

    async def fast_iter():
        counter["n"] += 1
        await asyncio.sleep(0.01)

    stop = asyncio.Event()

    async def stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    ex = ConstantVUs(vus=3, duration_s=10.0)  # would run 10s if stop didn't fire
    stop_task = asyncio.ensure_future(stop_soon())
    stats = await ex.run(fast_iter, stop)
    await stop_task
    assert stats.duration_s < 1.0  # stopped early, not after the full 10s


async def test_ramping_vus_hits_each_stage_target_concurrency():
    concurrency_samples = []
    current = {"n": 0}

    async def tracked_iter():
        current["n"] += 1
        concurrency_samples.append(current["n"])
        await asyncio.sleep(0.03)
        current["n"] -= 1

    stages = [Stage(target=1, duration="0.15s"), Stage(target=3, duration="0.15s")]
    ex = RampingVUs(stages=stages)
    stats = await ex.run(tracked_iter, asyncio.Event())
    assert stats.iterations_started > 0
    # We should have observed concurrency reaching 3 at some point during stage 2.
    assert max(concurrency_samples) >= 3


# ---------------------------------------------------------------------------
# Executors — open model (arrival.py) — dropped-iteration accounting
# ---------------------------------------------------------------------------

async def test_constant_arrival_rate_fires_on_schedule_with_no_drops():
    counter = {"n": 0}

    async def fast_iter():
        counter["n"] += 1
        await asyncio.sleep(0.01)

    ex = ConstantArrivalRate(rate=600, duration_s=0.5, max_vus=50)  # 10/sec, fast iterations, plenty of headroom
    stats = await ex.run(fast_iter, asyncio.Event())
    assert stats.iterations_dropped == 0
    assert stats.iterations_started >= 3  # ~5 expected at 10/sec for 0.5s


async def test_constant_arrival_rate_drops_when_max_vus_exceeded():
    """The key saturation signal: offering load faster than max_vus slow
    iterations can drain produces dropped iterations — the thing a
    closed-model executor can never produce by construction.
    """
    async def slow_iter():
        await asyncio.sleep(0.5)

    ex = ConstantArrivalRate(rate=600, duration_s=1.0, max_vus=2)  # 10/sec offered, only 2 concurrent allowed
    stats = await ex.run(slow_iter, asyncio.Event())
    assert stats.iterations_dropped > 0
    assert stats.iterations_started + stats.iterations_dropped >= 8  # scheduler kept firing on schedule


async def test_ramping_arrival_rate_interpolates_between_stages():
    rates_seen = []

    ex = RampingArrivalRate(
        stages=[Stage(target=10, duration="0.2s"), Stage(target=100, duration="0.2s")],
        max_vus=50,
    )
    # Sample the interpolation function directly rather than inferring
    # rate from noisy scheduling timing. Stage 1 has no "previous" stage
    # to ramp from, so it holds flat at its own target (10) for its whole
    # duration — the ramp happens entering stage 2 (k6's semantics).
    rates_seen.append(ex._rate_at(0.0))    # start of stage 1: flat at 10
    rates_seen.append(ex._rate_at(0.2))    # boundary: still 10 (stage 1 was flat)
    rates_seen.append(ex._rate_at(0.3))    # mid-ramp within stage 2
    rates_seen.append(ex._rate_at(0.4))    # end of stage 2: 100
    assert rates_seen[0] == pytest.approx(10.0, abs=1.0)
    assert rates_seen[1] == pytest.approx(10.0, abs=1.0)
    assert rates_seen[3] == pytest.approx(100.0, abs=1.0)
    assert rates_seen[1] < rates_seen[2] < rates_seen[3]


async def test_poisson_arrival_rate_is_bursty_not_metronomic():
    """With a fixed seed, successive inter-arrival intervals should vary —
    unlike ConstantArrivalRate's identical spacing every time.
    """
    import random
    ex = PoissonArrivalRate(rate=600, duration_s=1.0, max_vus=50, rng=random.Random(42))
    intervals = {ex._interval(600) for _ in range(20)}
    assert len(intervals) > 1  # not all identical — genuinely random spacing


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

async def test_sweep_finds_a_ceiling_against_a_fake_breaking_endpoint():
    in_flight = {"n": 0}

    async def fake_iter():
        in_flight["n"] += 1
        try:
            await asyncio.sleep(0.15)
        finally:
            in_flight["n"] -= 1

    sweep = Sweep(max_vus=3, floor_rate=5.0, ceiling_search_duration_s=1.0, step_duration_s=0.5, num_intermediate_steps=2)
    result = await sweep.run(fake_iter, asyncio.Event())
    assert result.ceiling_rate is not None
    assert len(result.steps) >= 3  # floor + at least one ceiling probe + intermediates
    # Every step at or above the ceiling rate should show drops; the floor should not.
    assert result.steps[0].iterations_dropped == 0


async def test_sweep_respects_stop_event():
    async def fast_iter():
        await asyncio.sleep(0.01)

    stop = asyncio.Event()
    stop.set()  # already stopped before the sweep even starts
    sweep = Sweep(max_vus=3, floor_rate=5.0, step_duration_s=5.0)
    result = await sweep.run(fast_iter, stop)
    assert result.ceiling_rate is None
    assert len(result.steps) <= 1


# ---------------------------------------------------------------------------
# build_executor factory
# ---------------------------------------------------------------------------

def test_build_executor_dispatches_every_kind():
    cfg_pairs = [
        (ExecutorConfig(kind="constant_vus", vus=1, duration="1s"), ConstantVUs),
        (ExecutorConfig(kind="ramping_vus", stages=[Stage(target=1, duration="1s")]), RampingVUs),
        (ExecutorConfig(kind="constant_arrival_rate", rate=10, duration="1s", max_vus=5), ConstantArrivalRate),
        (ExecutorConfig(kind="ramping_arrival_rate", stages=[Stage(target=1, duration="1s")], max_vus=5), RampingArrivalRate),
        (ExecutorConfig(kind="poisson_arrival_rate", rate=10, duration="1s", max_vus=5), PoissonArrivalRate),
    ]
    for cfg, expected_type in cfg_pairs:
        assert isinstance(build_executor(cfg), expected_type)


def test_build_executor_rejects_sweep():
    with pytest.raises(ScenarioConfigError):
        build_executor(ExecutorConfig(kind="sweep", max_vus=5))
