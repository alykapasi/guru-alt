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
from tests.eval.reliability import agreement, comparison, difficulty, knobs, metrics

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


def render_comparison(c: comparison.Comparison | None) -> str:
    """The shipped estimator against the alternatives, best first (S56)."""
    if c is None:
        return "Estimator comparison\n  nothing to compare — no scorable points\n"
    lines = [
        "Estimator comparison",
        f"  steps             {c.n_steps}",
        "",
        "  estimator        skill      ECE   resolution   config",
    ]
    for row in c.rows:
        conf = ", ".join(f"{k}={v:g}" for k, v in sorted(row.config.items()))
        mark = " *" if row.as_shipped else "  "
        lines.append(
            f" {mark}{row.name:<14} {row.skill:+.4f}   {row.reliability.ece:.4f}   "
            f"{row.reliability.resolution:.4f}       {conf}"
        )
    lines += ["", "  * = what shipped, scored on the predictions production actually made."]
    margin = c.margin
    if margin is None:
        lines.append("  No candidate to compare the shipped estimator against.")
    elif c.shipped_is_best:
        lines.append(f"  Shipped leads the best candidate by {margin:.4f} skill.")
    else:
        lines += [
            f"  A CANDIDATE BEAT PRODUCTION by {-margin:.4f} skill.",
            "  That is the only result here that licences changing the estimator — and it",
            "  licences investigating it, not shipping it: one dataset, one replay, and the",
            "  candidate never had to make its predictions before seeing the data collected",
            "  under a different one.",
        ]
    return "\n".join(lines) + "\n"


def render_difficulty(g: difficulty.GeneratorCalibration | None) -> str:
    """Requested difficulty against what the answers say it was worth (S12)."""
    if g is None:
        return "Generator difficulty\n  nothing to score\n"
    lines = [
        "Generator difficulty",
        f"  attempts          {g.n}  ({g.n_scored} in bands large enough to solve)",
        "",
        "  requested        n   asked    delivered    drift",
    ]
    for b in g.bands:
        if b.realised is None:
            got, drift = "     —", "  too few"
        elif b.saturated:
            got, drift = f"{b.realised:+.2f}*", " saturated"
        else:
            got, drift = f"{b.realised:+.2f} ", f"{b.drift:+.3f}"
        lines.append(
            f"  {b.lo:+.1f}-{b.hi:+.1f} {b.n:>5}  {b.mean_requested:+.2f}      {got}   {drift}"
        )
    lines.append("")
    if g.mean_absolute_drift is not None:
        lines.append(f"  mean |drift|      {g.mean_absolute_drift:.3f}  logits, n-weighted")
    if not g.usable:
        lines += [
            "",
            "  Too few populated bands to describe the map; this is one band, not a curve.",
        ]
    elif not g.monotonic:
        lines += [
            "",
            "  NOT MONOTONIC — asking for a harder item did not produce a harder one. The",
            "  stored difficulty is not merely miscalibrated in magnitude; it is not ordering",
            "  the requests, which is the weakest thing the label has to do to be worth having.",
        ]
    lines += [
        "",
        "  * saturated: every answer in the band went one way, so the data gives a direction",
        "    and no magnitude.",
        "  Abilities come from an estimator that assumed these difficulties were right, so a",
        "  drift here is the residual it could not absorb — evidence of miscalibration when",
        "  present, weaker evidence of calibration when absent. Expect drift to fall as",
        "  requested difficulty rises even from a perfect generator: the estimator absorbs",
        "  part of each item's difficulty into the learner's ability, which shrinks both ends",
        "  toward the middle. Read the drift against another run, not against zero.",
    ]
    return "\n".join(lines) + "\n"


def render_knobs() -> str:
    """Every uncalibrated number the loop runs on (S18)."""
    lines = [
        "Uncalibrated constants",
        f"  inventoried       {len(knobs.KNOBS)}",
    ]
    try:
        drift = knobs.drifted()
    except Exception as exc:  # settings unloadable; the list is still worth printing
        return "\n".join([*lines, f"  could not read live values: {exc}"]) + "\n"
    lines.append(f"  matching the code {len(knobs.KNOBS) - len(drift)}")
    if drift:
        lines.append("")
        for knob, now in drift:
            lines.append(f"  DRIFTED  {knob.id}: inventoried {knob.value:g}, code has {now}")
        lines += [
            "",
            "  A drifted entry means an uncalibrated number was changed without the inventory",
            "  being told. Re-guessing a knob nobody has measured is a decision, not an edit.",
        ]
    lines += [
        "",
        "  These are the numbers a reading would have to settle. None is set by evidence; each",
        "  entry names the reading that would set it. The count is the denominator for S18.",
    ]
    return "\n".join(lines) + "\n"


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
