"""Turns a validated `ExecutorConfig` into a constructed `Executor`.
`sweep` is handled separately by the caller (runner.py) since its result
type (`SweepResult`) differs from `ExecutorStats` — see sweep.py.
"""
from maasagentharness.scenario.executors.arrival import (
    ConstantArrivalRate,
    PoissonArrivalRate,
    RampingArrivalRate,
)
from maasagentharness.scenario.executors.base import Executor
from maasagentharness.scenario.executors.vus import ConstantVUs, RampingVUs
from maasagentharness.scenario.schema import ExecutorConfig, ScenarioConfigError, parse_duration


def build_executor(cfg: ExecutorConfig) -> Executor:
    if cfg.kind == "constant_vus":
        return ConstantVUs(vus=cfg.vus, duration_s=parse_duration(cfg.duration))
    if cfg.kind == "ramping_vus":
        return RampingVUs(stages=cfg.stages)
    if cfg.kind == "constant_arrival_rate":
        return ConstantArrivalRate(rate=cfg.rate, duration_s=parse_duration(cfg.duration), max_vus=cfg.max_vus)
    if cfg.kind == "ramping_arrival_rate":
        return RampingArrivalRate(stages=cfg.stages, max_vus=cfg.max_vus)
    if cfg.kind == "poisson_arrival_rate":
        return PoissonArrivalRate(rate=cfg.rate, duration_s=parse_duration(cfg.duration), max_vus=cfg.max_vus)
    raise ScenarioConfigError(
        f"'{cfg.kind}' is not built by build_executor() — sweep is constructed directly by the caller."
    )
