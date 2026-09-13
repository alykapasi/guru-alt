"""``uv run poe reliability-report`` — S59's first slice, rendered.

The calibration half needs no model and no network: it replays a mined dataset through the
production estimator and scores what it claimed against what happened. The grading half needs a
live grader, so it is opt-in (``--grade``) and paid, on the same footing as ``poe eval``.

Every number here is reported without a verdict. There is no pass mark, deliberately: what
calibration error or what kappa is good enough to build a product on is a decision nobody has
made, and picking one before the first curve has been looked at is exactly the guess S59 exists
to stop (S18). The report says what is true and leaves the threshold to a person.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from tests.eval.datasets.calibration import replay
from tests.eval.datasets.models import CalibrationDataset
from tests.eval.reliability import agreement, metrics

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "datasets" / "tracer-calibration.json"


def render_calibration(r: metrics.Reliability | None, *, source: str) -> str:
    if r is None:
        return f"Estimator calibration\n  no scorable points in {source}\n"
    lines = [
        "Estimator calibration",
        f"  source            {source}",
        f"  points            {r.n}",
        f"  base rate         {r.base_rate:.3f}"
        + (f"   (outcomes binarised at {r.binarised_at})" if r.binarised_at is not None else ""),
        "",
        f"  Brier             {r.brier:.4f}   lower is better",
        f"  calibration (ECE) {r.ece:.4f}   lower is better — are the numbers honest?",
        f"  resolution        {r.resolution:.4f}   HIGHER is better — do they say anything?",
        f"  uncertainty       {r.uncertainty:.4f}   what predicting the base rate would score",
        f"  skill             {r.skill:+.4f}   share of that error removed",
        "",
        f"  verdict           {'beats the base rate' if r.informative else 'NO BETTER THAN THE BASE RATE'}",
        "",
        "  predicted      n   said    happened    gap",
    ]
    for b in r.buckets:
        lines.append(
            f"  {b.lo:.1f}-{b.hi:.1f}  {b.n:>5}  {b.mean_predicted:.3f}     "
            f"{b.mean_actual:.3f}   {b.gap:+.3f}"
        )
    if not r.informative:
        lines += [
            "",
            "  A forecaster that ignores the learner and always predicts the base rate scores",
            "  zero calibration error too. Low ECE with skill at or below zero means honest",
            "  numbers that carry no information — read the resolution line, not the ECE.",
        ]
    return "\n".join(lines) + "\n"


def render_agreement(a: agreement.Agreement | None, *, against: str) -> str:
    if a is None:
        return f"Grading agreement\n  nothing to compare against {against}\n"
    corr = "undefined" if a.correlation is None else f"{a.correlation:+.3f}"
    kappa = "undefined" if a.kappa is None else f"{a.kappa:+.3f}"
    lines = [
        "Grading agreement",
        f"  against           {against}",
        f"  pairs             {a.n}",
        f"  mean difference   {a.mean_absolute_difference:.3f}",
        f"  worst difference  {a.max_absolute_difference:.3f}",
        f"  correlation       {corr}",
        "",
        f"  raw agreement     {a.raw_band_agreement:.3f}   inflated by the base rate",
        f"  kappa             {kappa}   chance-corrected — read this one",
        f"  interpretation    {a.interpretation}",
    ]
    if a.kappa is None:
        lines += [
            "",
            "  Kappa is undefined because one grader used a single band for everything. Raw",
            "  agreement of 1.000 there means no judgement was exercised, not that two judges",
            "  concurred.",
        ]
    return "\n".join(lines) + "\n"


def calibration_from(
    path: Path, *, binarise_at: float | None
) -> tuple[metrics.Reliability | None, str]:
    if not path.exists():
        return None, f"{path} (missing — run `uv run poe build-calibration-dataset`)"
    dataset = CalibrationDataset.from_file(path)
    walked = replay(dataset)
    note = f"{path.name}, {len(dataset.sequences)} sequences"
    if walked.n_recorded_predictions == 0:
        note += " — scoring today's estimator on old data, not the predictions production made"
    return metrics.assess(walked.pairs, binarise_at=binarise_at), note


async def agreement_against_labels() -> tuple[agreement.Agreement | None, str]:
    """Score the golden grading cases with the live grader and compare to the human labels.

    A paid call per case. Imported lazily so the offline half of this report never constructs a
    provider client.
    """
    from app.core.config import get_settings
    from app.learning.rubric_grading import Rubric, RubricGradingError, grade_open
    from app.llm.registry import build_llm_client
    from tests.eval.harness import load_rubric_cases

    client = build_llm_client(get_settings())
    pairs: list[tuple[float, float]] = []
    for case in load_rubric_cases():
        # Same construction as `harness.score_rubric`, so the two cannot grade the same case
        # differently and call the difference a finding.
        rubric = Rubric(criteria=case.criteria) if case.criteria else None
        try:
            result, _usage = await grade_open(
                client, stem=case.stem, response=case.response, rubric=rubric
            )
        except RubricGradingError:
            continue  # a weak model may emit unparseable output; it is not a disagreement
        pairs.append((result.score, case.expected_score))
    return agreement.compare(pairs), f"{len(pairs)} human-labelled golden cases"


def main() -> None:
    ap = argparse.ArgumentParser(description="Reliability before validity (S59).")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument(
        "--binarise-at",
        type=float,
        default=None,
        help="score correctness rather than partial credit, at this threshold",
    )
    ap.add_argument(
        "--grade",
        action="store_true",
        help="also score the golden cases with the live grader (paid)",
    )
    args = ap.parse_args()

    cal, source = calibration_from(args.dataset, binarise_at=args.binarise_at)
    print()
    print(render_calibration(cal, source=source))

    if args.grade:
        agr, against = asyncio.run(agreement_against_labels())
        print(render_agreement(agr, against=against))
    else:
        print("Grading agreement\n  skipped — pass --grade to score the golden cases (paid)\n")


if __name__ == "__main__":
    main()
