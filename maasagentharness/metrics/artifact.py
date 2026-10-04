"""Run artifacts: one directory per scenario run, written by the worker that
ran it, so the record of a run survives the dashboard going down and doesn't
depend on it being up.

    runs/<run_name>/<scenario_name>/<utc-timestamp>/
        scenario.json    resolved scenario config, written once at start
        requests.jsonl   one TelemetryEvent per line, appended as they happen
        summary.json     aggregates, rewritten atomically every flush

Deviation from the plan (§4.5): raw records are JSON Lines, not Parquet.
JSONL appends safely line-by-line, so a crashed run still leaves every
record it got to; Parquet would need pyarrow and a finalised file. Convert
to Parquet offline if you want columnar analysis.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from maasagentharness.metrics.aggregate import Aggregate


def _git_sha() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
        if out.returncode != 0:
            return None
        return out.stdout.strip() or None
    except Exception:
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)  # atomic on POSIX — readers never see a half-written summary


class RunArtifact:
    def __init__(self, directory: Path, meta: Dict[str, Any]):
        self.directory = directory
        self.meta = meta
        self.aggregate = Aggregate()
        self._requests_file = (directory / "requests.jsonl").open("a", encoding="utf-8")

    @classmethod
    def create(
        cls,
        root: Path,
        run_name: str,
        scenario_name: str,
        scenario_config: Dict[str, Any],
        worker_id: str,
    ) -> "RunArtifact":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = Path(root) / run_name / scenario_name / stamp
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(directory / "scenario.json", scenario_config)
        meta = {
            "run": run_name,
            "scenario": scenario_name,
            "worker_id": worker_id,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_sha": _git_sha(),
        }
        return cls(directory, meta)

    def record(self, event_dict: Dict[str, Any]) -> None:
        """Append one event. Flushed per line so a crash loses at most the
        line being written, never the whole run."""
        self._requests_file.write(json.dumps(event_dict, default=str) + "\n")
        self._requests_file.flush()
        self.aggregate.add(_Attr(event_dict))

    def write_summary(self, executor_stats: Optional[Dict[str, Any]], finished: bool) -> None:
        summary = {
            **self.meta,
            "finished": finished,
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "executor": executor_stats,
            "requests": self.aggregate.to_dict(),
        }
        _atomic_write_json(self.directory / "summary.json", summary)

    def close(self) -> None:
        if not self._requests_file.closed:
            self._requests_file.close()


class _Attr:
    """Adapts a dict to the attribute access Aggregate.add expects."""

    def __init__(self, d: Dict[str, Any]):
        self.__dict__.update(d)
