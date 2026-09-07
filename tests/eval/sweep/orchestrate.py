"""Drive a whole sweep: expand → run each cell → log params/metrics/artifacts per run."""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient
from tests.eval import harness
from tests.eval.sweep.config import Cell, SweepConfig, expand
from tests.eval.sweep.cost import CostSummary
from tests.eval.sweep.runner import run_cell
from tests.eval.sweep.tracking import Tracker


async def run_sweep(
    config: SweepConfig,
    base_client: LLMClient,
    tracker: Tracker,
    *,
    session: AsyncSession | None = None,
    artifact_dir: Path | None = None,
) -> None:
    """Run every cell in ``config``; one cell = one tracked run. A failed cell is logged with
    ``status=failed`` and does not abort the sweep (§10)."""
    for cell in expand(config):
        tracker.start_run(cell.id)
        try:
            tracker.log_params(_cell_params(cell))
            try:
                reports, cost = await run_cell(cell, base_client, session=session)
            except Exception as exc:  # provider error / timeout / auth / bad suite — keep going
                tracker.log_params({"status": "failed", "error": str(exc)[:500]})
                continue
            tracker.log_params({"status": "ok"})
            tracker.log_metrics(_cell_metrics(reports, cost))
            _log_artifacts(tracker, cell, reports, artifact_dir)
        finally:
            tracker.end_run()


def _cell_params(cell: Cell) -> dict[str, str]:
    params: dict[str, str] = {"suites": ",".join(cell.suites)}
    for role, spec in cell.role_overrides.items():
        params[f"{role.value}_model"] = f"{spec.provider}:{spec.model}"
    for name, value in cell.toggles.items():
        params[f"toggle_{name}"] = str(value)
    for key, value in cell.gen_config.items():
        params[f"gen_{key}"] = str(value)
    return params


def _cell_metrics(reports: list[harness.EvalReport], cost: CostSummary) -> dict[str, float]:
    metrics: dict[str, float] = {"total_tokens": float(cost.total_tokens)}
    # Omitted, not zeroed, when a model in the cell has no known price: the leaderboard ranks a
    # missing cost last, which is the right answer for a configuration whose cost is unestablished.
    if cost.cost_usd is not None:
        metrics["cost_usd"] = cost.cost_usd
    for report in reports:
        metrics[f"{report.suite}_pass_rate"] = report.pass_rate
        if report.mae is not None:
            metrics[f"{report.suite}_mae"] = report.mae
    return metrics


def _log_artifacts(
    tracker: Tracker,
    cell: Cell,
    reports: list[harness.EvalReport],
    artifact_dir: Path | None,
) -> None:
    out = artifact_dir or Path(tempfile.mkdtemp())
    out.mkdir(parents=True, exist_ok=True)
    for report in reports:
        path = out / f"{cell.id}-{report.suite}.json"
        path.write_text(report.model_dump_json(indent=2))
        tracker.log_artifact(path)
