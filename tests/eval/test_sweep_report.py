"""Leaderboard ordering + pairwise diff math."""

import pytest

from tests.eval.sweep.report import (
    _default_quality_metric,
    _format_diff,
    _format_leaderboard,
    leaderboard,
    pairwise_diff,
)
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


def test_format_leaderboard_orders_best_first() -> None:
    out = _format_leaderboard([_run("mid", 0.9, 0.2), _run("top", 1.0, 0.5)])
    body = [line for line in out.splitlines() if line.startswith(("top", "mid"))]
    assert body[0].startswith("top") and body[1].startswith("mid")


def test_format_diff_shows_signed_deltas() -> None:
    out = _format_diff(_run("a", 0.8, 0.5), _run("b", 0.95, 0.1))
    assert "b" in out and "a" in out
    assert "+0.1500" in out  # rubric_pass_rate delta
    assert "-0.4000" in out  # cost_usd delta


def test_format_leaderboard_uses_given_quality_metric() -> None:
    runs = [
        RunRecord(
            name="a",
            params={"suites": "kc_tagging"},
            metrics={"kc_tagging_pass_rate": 0.7, "cost_usd": 0.2},
        ),
        RunRecord(
            name="b",
            params={"suites": "kc_tagging"},
            metrics={"kc_tagging_pass_rate": 0.9, "cost_usd": 0.5},
        ),
    ]
    out = _format_leaderboard(runs, quality_metric="kc_tagging_pass_rate")
    body = [line for line in out.splitlines() if line.startswith(("a", "b"))]
    assert body[0].startswith("b") and body[1].startswith("a")  # 0.9 ranks above 0.7
    assert "kc_tagging_pass_rate" in out.splitlines()[0]  # header names the metric


def test_default_quality_metric_derives_from_suites() -> None:
    runs = [RunRecord(name="a", params={"suites": "kc_tagging"}, metrics={})]
    assert _default_quality_metric(runs) == "kc_tagging_pass_rate"
    assert _default_quality_metric([RunRecord(name="x")]) == "rubric_pass_rate"  # fallback
