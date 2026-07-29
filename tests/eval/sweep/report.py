"""Leaderboard + pairwise ablation diff over logged runs (pure functions)."""

from __future__ import annotations

from tests.eval.sweep.tracking import RunRecord


def leaderboard(
    runs: list[RunRecord], *, quality_metric: str = "rubric_pass_rate"
) -> list[RunRecord]:
    """Runs ranked by quality (desc), ties broken by cost (asc). Missing quality sorts last."""

    def key(run: RunRecord) -> tuple[float, float]:
        quality = run.metrics.get(quality_metric, float("-inf"))
        cost = run.metrics.get("cost_usd", float("inf"))
        return (-quality, cost)

    return sorted(runs, key=key)


def pairwise_diff(a: RunRecord, b: RunRecord) -> dict[str, float]:
    """Per-metric delta ``b - a`` over metrics present in either run (missing = 0.0)."""
    keys = set(a.metrics) | set(b.metrics)
    return {k: b.metrics.get(k, 0.0) - a.metrics.get(k, 0.0) for k in sorted(keys)}
