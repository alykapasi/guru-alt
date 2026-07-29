"""Leaderboard ordering + pairwise diff math."""

import pytest

from tests.eval.sweep.report import leaderboard, pairwise_diff
from tests.eval.sweep.tracking import RunRecord


def _run(name: str, quality: float, cost: float) -> RunRecord:
    return RunRecord(name=name, metrics={"rubric_pass_rate": quality, "cost_usd": cost})


def test_leaderboard_ranks_by_quality_then_cost() -> None:
    runs = [_run("mid", 0.9, 0.2), _run("top", 1.0, 0.5), _run("cheap-tie", 0.9, 0.1)]
    assert [r.name for r in leaderboard(runs)] == ["top", "cheap-tie", "mid"]


def test_leaderboard_missing_quality_sorts_last() -> None:
    runs = [RunRecord(name="no-metric"), _run("good", 0.8, 0.3)]
    assert next(r.name for r in leaderboard(runs)) == "good"


def test_pairwise_diff_is_b_minus_a() -> None:
    diff = pairwise_diff(_run("a", 0.8, 0.5), _run("b", 0.95, 0.1))
    assert diff["rubric_pass_rate"] == pytest.approx(0.15)
    assert diff["cost_usd"] == pytest.approx(-0.4)
