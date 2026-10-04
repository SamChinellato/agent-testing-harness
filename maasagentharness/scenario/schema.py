"""Phase 3: the declarative scenario/run schema.

This is the fix for redesign-spec.md §1's actual missing capability: the
harness could only ever run one request in flight at a time, paced by a
hardcoded per-mode `sleep()`. There was no way to say "60 requests/min",
"ramp 1->50 over two minutes", or "sweep to find the ceiling". This module
is the typed, validated shape of a scenario file; `loader.py` turns YAML
into these models; `executors/` turns these models into actual load.
"""
import re
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

AgentLiteral = Literal["triage", "research", "assistant"]
ModeLiteral = Literal["naive", "paced", "runaway"]
ExecutorKind = Literal[
    "constant_vus", "ramping_vus",
    "constant_arrival_rate", "ramping_arrival_rate", "poisson_arrival_rate",
    "sweep",
]
ScopeLiteral = Literal["global", "team", "worker"]
EnforcementLiteral = Literal["off", "client_bucket", "shadow"]

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)?$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class ScenarioConfigError(ValueError):
    """Raised for anything wrong with a scenario file — a bad duration
    string, a missing field an executor kind requires, an unresolvable
    ${ENV_VAR}, or an unparseable threshold expression. Always a clear,
    specific message; never a bare pydantic ValidationError dump.
    """
    pass


def parse_duration(value: str) -> float:
    """"30s" -> 30.0, "2m" -> 120.0, "1h" -> 3600.0, "500ms" -> 0.5.
    A bare number (no unit) is interpreted as seconds.
    """
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(value.strip())
    if not match:
        raise ScenarioConfigError(
            f"Invalid duration '{value}' — expected e.g. '30s', '2m', '1h', '500ms', or a bare number of seconds."
        )
    amount = float(match.group("value"))
    unit = match.group("unit") or "s"
    return amount * _UNIT_SECONDS[unit]


class Stage(BaseModel):
    """One step of a ramping executor's schedule: hold/ramp toward `target`
    (VUs for ramping_vus, iterations/min for ramping_arrival_rate) over
    `duration`.
    """
    model_config = ConfigDict(extra="forbid")

    target: int
    duration: str


class ExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ExecutorKind
    rate: Optional[float] = None       # iterations/min — *_arrival_rate, sweep's floor
    vus: Optional[int] = None          # constant_vus
    stages: Optional[List[Stage]] = None  # ramping_*
    max_vus: Optional[int] = None      # concurrency cap — required for every open-model executor
    duration: Optional[str] = None     # constant_vus / constant_arrival_rate / poisson_arrival_rate
    # sweep-only knobs; all have sensible defaults in executors/sweep.py.
    step_duration: Optional[str] = None
    ceiling_search_duration: Optional[str] = None
    num_intermediate_steps: Optional[int] = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> "ExecutorConfig":
        def require(*names: str) -> None:
            missing = [n for n in names if getattr(self, n) is None]
            if missing:
                raise ScenarioConfigError(
                    f"executor kind '{self.kind}' requires {', '.join(missing)}"
                )

        if self.kind == "constant_vus":
            require("vus", "duration")
        elif self.kind == "ramping_vus":
            require("stages")
        elif self.kind == "constant_arrival_rate":
            require("rate", "duration", "max_vus")
        elif self.kind in ("ramping_arrival_rate",):
            require("stages", "max_vus")
        elif self.kind == "poisson_arrival_rate":
            require("rate", "duration", "max_vus")
        elif self.kind == "sweep":
            require("max_vus")
        return self


class ScenarioConfig(BaseModel):
    """One named load-generating scenario within a run.

    `mode` is an addition to the plan's original schema (which only had
    agent/executor/budget/graceful_stop/start_time) — see
    docs/phase-1-5-implementation-plan.md §3.7 for why it was missing and
    needed adding: naive/paced/runaway controls retry/backoff strategy and
    whether client_bucket enforcement actually blocks, independent of the
    executor's load shape.
    """
    model_config = ConfigDict(extra="forbid")

    agent: AgentLiteral
    mode: ModeLiteral = "naive"
    executor: ExecutorConfig
    budget: Optional[str] = None       # a budget_id — resolved directly, bypassing team/global fallback
    graceful_stop: str = "30s"
    start_time: str = "0s"


class BudgetConfig(BaseModel):
    """An inline Budget definition — upserted into the store before the
    run starts, so a scenario file is self-contained and reproducible
    without requiring the budget to already exist.
    """
    model_config = ConfigDict(extra="forbid")

    budget_id: str
    scope: ScopeLiteral = "global"
    scope_ref: Optional[str] = None
    tokens_per_minute: Optional[int] = None
    requests_per_minute: Optional[int] = None
    max_concurrency: Optional[int] = None
    headroom: float = 0.90
    enforcement: EnforcementLiteral = "client_bucket"


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: Optional[str] = None
    model: Optional[str] = None


_THRESHOLD_RE = re.compile(
    r"^(?P<fn>[a-zA-Z_][a-zA-Z0-9_]*)\((?P<metric>[a-zA-Z_][a-zA-Z0-9_]*)\)\s*"
    r"(?P<op>>=|<=|==|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)$"
)


class ThresholdSpec(BaseModel):
    """A parsed `"p95(total_latency_ms) < 8000"`-style expression.

    Evaluation against real percentiles needs Phase 4's histogram store;
    today only `breach_fn`-style checks against `ExecutorStats` (e.g.
    `rate(dropped) < 0.05`) are actually evaluable — see sweep.py. This
    model exists now so scenario files can declare thresholds today and
    have them mean something as soon as Phase 4 lands, without a schema
    migration.
    """
    raw: str
    fn: str
    metric: str
    op: str
    value: float

    @classmethod
    def parse(cls, raw: str) -> "ThresholdSpec":
        m = _THRESHOLD_RE.match(raw.strip())
        if not m:
            raise ScenarioConfigError(
                f"Invalid threshold '{raw}' — expected e.g. 'p95(total_latency_ms) < 8000' "
                f"or 'rate(status_429) < 0.05'."
            )
        return cls(
            raw=raw, fn=m.group("fn"), metric=m.group("metric"),
            op=m.group("op"), value=float(m.group("value")),
        )

    def check(self, observed: float) -> bool:
        """True if `observed` satisfies this threshold (i.e. no breach)."""
        ops = {
            "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
            "==": lambda a, b: a == b,
        }
        return ops[self.op](observed, self.value)


class RunConfig(BaseModel):
    """The top-level shape of a scenario YAML file."""
    model_config = ConfigDict(extra="forbid")

    run: str
    endpoint: EndpointConfig = Field(default_factory=EndpointConfig)
    budgets: List[BudgetConfig] = Field(default_factory=list)
    scenarios: Dict[str, ScenarioConfig]
    thresholds: List[str] = Field(default_factory=list)

    @property
    def parsed_thresholds(self) -> List[ThresholdSpec]:
        return [ThresholdSpec.parse(t) for t in self.thresholds]
