"""Pluggable experiment tracking (D7): a Protocol seam, an MLflow impl, an in-memory fake."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field


class RunRecord(BaseModel):
    """A logged run read back for reporting: its name, params, and metrics."""

    name: str
    params: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)


class Tracker(Protocol):
    """The tracking seam. One cell = one run."""

    def start_run(self, name: str) -> None: ...
    def log_params(self, params: dict[str, str]) -> None: ...
    def log_metrics(self, metrics: dict[str, float]) -> None: ...
    def log_artifact(self, path: str | Path) -> None: ...
    def end_run(self) -> None: ...
    def list_runs(self) -> list[RunRecord]: ...


class FakeTracker:
    """In-memory tracker for tests: records everything, serves it back via ``list_runs``."""

    def __init__(self) -> None:
        self.runs: list[RunRecord] = []
        self.artifacts: dict[str, list[str]] = {}
        self._current: RunRecord | None = None

    def start_run(self, name: str) -> None:
        self._current = RunRecord(name=name)
        self.runs.append(self._current)
        self.artifacts[name] = []

    def log_params(self, params: dict[str, str]) -> None:
        assert self._current is not None, "log_params outside a run"
        self._current.params.update(params)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        assert self._current is not None, "log_metrics outside a run"
        self._current.metrics.update(metrics)

    def log_artifact(self, path: str | Path) -> None:
        assert self._current is not None, "log_artifact outside a run"
        self.artifacts[self._current.name].append(str(path))

    def end_run(self) -> None:
        self._current = None

    def list_runs(self) -> list[RunRecord]:
        return list(self.runs)


def _present(value: object) -> bool:
    """True unless the value is None or a NaN float (MLflow fills absent cells with NaN)."""
    return value is not None and not (isinstance(value, float) and math.isnan(value))


class MLflowTracker:
    """Logs to a local file-backed MLflow store (no server). Tracking subset only (D7, §6)."""

    def __init__(self, experiment: str, tracking_uri: str | None = None) -> None:
        # MLflow >=3.x gates the filesystem tracking backend behind this opt-in (it's in
        # maintenance mode upstream); we rely on it deliberately for local, no-server sweeps.
        os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

        import mlflow  # lazy: keeps this module importable without mlflow installed

        self._mlflow = mlflow
        mlflow.set_tracking_uri(tracking_uri or f"file:{Path('mlruns').resolve()}")
        mlflow.set_experiment(experiment)
        self._experiment = experiment

    def start_run(self, name: str) -> None:
        self._mlflow.start_run(run_name=name)

    def log_params(self, params: dict[str, str]) -> None:
        self._mlflow.log_params(params)

    def log_metrics(self, metrics: dict[str, float]) -> None:
        self._mlflow.log_metrics(metrics)

    def log_artifact(self, path: str | Path) -> None:
        self._mlflow.log_artifact(str(path))

    def end_run(self) -> None:
        self._mlflow.end_run()

    def list_runs(self) -> list[RunRecord]:
        frame = self._mlflow.search_runs(experiment_names=[self._experiment])
        records: list[RunRecord] = []
        for _, row in frame.iterrows():  # ty: ignore[unresolved-attribute]
            params = {
                str(key)[len("params.") :]: str(val)
                for key, val in row.items()
                if str(key).startswith("params.") and _present(val)
            }
            metrics = {
                str(key)[len("metrics.") :]: float(val)
                for key, val in row.items()
                if str(key).startswith("metrics.") and _present(val)
            }
            name = row.get("tags.mlflow.runName") or row.get("run_id", "")
            records.append(RunRecord(name=str(name), params=params, metrics=metrics))
        return records
