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


def _default_quality_metric(runs: list[RunRecord]) -> str:
    """Pick the quality metric to rank on from the runs' logged `suites` param.

    All cells in a sweep share the same suites (from the config), so the first suite of the first
    run carrying a `suites` param decides the column — e.g. a kc_tagging sweep ranks on
    `kc_tagging_pass_rate`. Falls back to `rubric_pass_rate` when no suites param is present.
    """
    for run in runs:
        suites = run.params.get("suites", "")
        first = suites.split(",")[0].strip() if suites else ""
        if first:
            return f"{first}_pass_rate"
    return "rubric_pass_rate"


def _format_leaderboard(runs: list[RunRecord], *, quality_metric: str = "rubric_pass_rate") -> str:
    ranked = leaderboard(runs, quality_metric=quality_metric)
    lines = [f"{'run':<28}{quality_metric:>22}{'cost_usd':>12}", "-" * 62]
    for run in ranked:
        quality = run.metrics.get(quality_metric, float("nan"))
        cost = run.metrics.get("cost_usd", float("nan"))
        lines.append(f"{run.name:<28}{quality:>22.3f}{cost:>12.4f}")
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
    parser.add_argument(
        "--quality-metric",
        default=None,
        help="metric to rank by (default: derived from the run's suites)",
    )
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
        quality_metric = args.quality_metric or _default_quality_metric(runs)
        print(_format_leaderboard(runs, quality_metric=quality_metric))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
