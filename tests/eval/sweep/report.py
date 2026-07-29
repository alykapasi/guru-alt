"""Leaderboard + pairwise ablation diff over logged runs (pure functions)."""

from __future__ import annotations

import sys

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


def _format_leaderboard(runs: list[RunRecord]) -> str:
    ranked = leaderboard(runs)
    lines = [f"{'run':<28}{'quality':>9}{'cost_usd':>12}", "-" * 49]
    for run in ranked:
        quality = run.metrics.get("rubric_pass_rate", float("nan"))
        cost = run.metrics.get("cost_usd", float("nan"))
        lines.append(f"{run.name:<28}{quality:>9.3f}{cost:>12.4f}")
    return "\n".join(lines)


def _format_diff(a: RunRecord, b: RunRecord) -> str:
    lines = [f"diff: {b.name} - {a.name}", "-" * 34]
    lines += [f"{key:<24}{delta:>+10.4f}" for key, delta in pairwise_diff(a, b).items()]
    return "\n".join(lines)


def main() -> int:
    import argparse

    from tests.eval.sweep.tracking import MLflowTracker

    parser = argparse.ArgumentParser(prog="sweep-report")
    parser.add_argument("--experiment", help="experiment name to report on")
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    args = parser.parse_args()

    if not args.experiment:
        print(
            "usage: poe sweep-report --experiment <name> [--compare RUN_A RUN_B]",
            file=sys.stderr,
        )
        return 2

    runs = MLflowTracker(experiment=args.experiment).list_runs()
    if args.compare:
        by_name = {r.name: r for r in runs}
        a, b = by_name.get(args.compare[0]), by_name.get(args.compare[1])
        if a is None or b is None:
            print(f"run(s) not found: {args.compare}", file=sys.stderr)
            return 1
        print(_format_diff(a, b))
    else:
        print(_format_leaderboard(runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
