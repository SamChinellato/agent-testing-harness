"""YAML -> validated RunConfig, with `${ENV_VAR}` interpolation.

Deliberately strict: an undefined `${ENV_VAR}` is a load-time error, not a
literal `${ENV_VAR}` silently embedded into (say) a base_url. A scenario
file that references a secret or endpoint via env var should fail loudly
if that env var isn't set, not run against a broken URL.
"""
import os
import re
from pathlib import Path
from typing import Any, Set, Union

import yaml
from pydantic import ValidationError

from maasagentharness.scenario.schema import RunConfig, ScenarioConfigError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any, missing: Set[str]) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                missing.add(name)
                return m.group(0)
            return os.environ[name]
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, missing) for v in value]
    return value


def load_run_config(path: Union[str, Path]) -> RunConfig:
    path = Path(path)
    if not path.exists():
        raise ScenarioConfigError(f"Scenario file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ScenarioConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ScenarioConfigError(f"{path}: top-level YAML must be a mapping, got {type(raw).__name__}")

    missing: Set[str] = set()
    interpolated = _interpolate(raw, missing)
    if missing:
        raise ScenarioConfigError(
            f"{path}: undefined environment variable(s) referenced: {', '.join(sorted(missing))}"
        )

    try:
        return RunConfig(**interpolated)
    except ValidationError as e:
        raise ScenarioConfigError(f"{path}: {e}") from e
