"""Prequential tracer-calibration scorer (Phase 9b, corrected by S56).

Replays each sequence through the production estimator, predicting each step's expected score
from prior steps only, then updating on the actual — the gold-standard one-step-ahead eval of an
online learner model. Reuses app.learning.tracer so the eval can't drift from what ships.

Two distinct questions run through the same code, and conflating them was the defect S56 names:

* *How well did what we shipped predict?* — the default. Steps are replayed down the production
  path (placement prior, then decay, then a weight- and assistance-scaled update) and scored
  against the prediction production actually made at the time.
* *How well would a candidate estimator predict?* — pass ``estimator``. Predictions are then
  recomputed by that estimator, because the recorded ones are not its.

The old scorer did neither: it started every learner at the population prior, never decayed,
and gave every KC of a multi-KC item the full weight of the item.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.learning.tracer import GlickoEstimator, MasteryEstimator, estimator_from
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence
from tests.eval.datasets.replay import initial_estimate

_N_BUCKETS = 5
_DEFAULT_ESTIMATOR: MasteryEstimator = GlickoEstimator()


class ReliabilityBucket(BaseModel):
    lo: float
    hi: float
    n: int
    mean_predicted: float
    mean_actual: float


class CalibrationReport(BaseModel):
    mae: float | None  # None when the dataset has no scorable points
    rmse: float | None
    n_points: int
    reliability: list[ReliabilityBucket]
    sequences_passed: int
    sequences_total: int
    # How many scored points used the prediction production recorded, rather than one
    # recomputed now. A run reporting 0 here is measuring today's estimator on old data —
    # a legitimate thing to do, but not the same claim, so the number is on the report.
    n_recorded_predictions: int = 0


class Replay(BaseModel):
    """The raw output of one prequential pass, before anybody scores it.

    Separated from :func:`score_tracer_calibration` so that scoring the same replay a second way
    cannot mean replaying it a second way. The reliability report (S59) reads these pairs; if it
    re-implemented the walk, the two would drift the first time the production update changed and
    the disagreement would look like a finding.
    """

    by_sequence: list[list[tuple[float, float]]]
    n_recorded_predictions: int

    @property
    def pairs(self) -> list[tuple[float, float]]:
        """Every (predicted, actual) point, sequence boundaries dropped."""
        return [pair for seq in self.by_sequence for pair in seq]


def replay(dataset: CalibrationDataset, *, estimator: MasteryEstimator | None = None) -> Replay:
    """Walk every sequence predict-then-update, collecting what was claimed against what happened.

    ``estimator`` scores a *candidate*: predictions are recomputed with it and the recorded ones
    ignored. Left unset, each sequence is replayed under the estimator that produced it.
    """
    by_sequence: list[list[tuple[float, float]]] = []
    recorded = 0
    for seq in dataset.sequences:
        est = estimator or _estimator_for(seq)
        estimate = initial_estimate(seq)
        seq_pairs: list[tuple[float, float]] = []
        for step in seq.steps:
            decayed = est.decay(estimate, elapsed_days=step.elapsed_days or 0.0)
            if estimator is None and step.predicted is not None:
                predicted = step.predicted
                recorded += 1
            else:
                predicted = est.expected(decayed, difficulty=step.difficulty)
            seq_pairs.append((predicted, step.score))
            estimate = est.update(
                decayed,
                score=step.score,
                difficulty=step.difficulty,
                weight=(step.weight if step.weight is not None else 1.0)
                * (step.credit if step.credit is not None else 1.0),
            )
        by_sequence.append(seq_pairs)
    return Replay(by_sequence=by_sequence, n_recorded_predictions=recorded)


def score_tracer_calibration(
    dataset: CalibrationDataset,
    *,
    estimator: MasteryEstimator | None = None,
    tolerance: float = 0.15,
) -> CalibrationReport:
    """Prequential (predict-then-update) replay; MAE/RMSE + a reliability table over all steps."""
    walked = replay(dataset, estimator=estimator)
    sequences_passed = sum(
        1
        for seq in walked.by_sequence
        if seq and sum(abs(p - a) for p, a in seq) / len(seq) <= tolerance
    )
    pairs = walked.pairs
    return CalibrationReport(
        mae=_mae(pairs),
        rmse=_rmse(pairs),
        n_points=len(pairs),
        reliability=_reliability(pairs),
        sequences_passed=sequences_passed,
        sequences_total=len(dataset.sequences),
        n_recorded_predictions=walked.n_recorded_predictions,
    )


def _estimator_for(seq: ObservationSequence) -> MasteryEstimator:
    """The estimator a sequence was recorded under; the current default for older rows."""
    if seq.estimator is None or seq.estimator_config is None:
        return _DEFAULT_ESTIMATOR
    return estimator_from(seq.estimator, seq.estimator_config)


def _mae(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def _rmse(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return (sum((p - a) ** 2 for p, a in pairs) / len(pairs)) ** 0.5


def _reliability(pairs: list[tuple[float, float]]) -> list[ReliabilityBucket]:
    buckets: list[ReliabilityBucket] = []
    for i in range(_N_BUCKETS):
        lo = i / _N_BUCKETS
        hi = (i + 1) / _N_BUCKETS
        # buckets are [lo, hi); the last one also captures predicted == 1.0
        in_bucket = [(p, a) for p, a in pairs if lo <= p < hi or (i == _N_BUCKETS - 1 and p == hi)]
        if not in_bucket:
            continue
        n = len(in_bucket)
        buckets.append(
            ReliabilityBucket(
                lo=lo,
                hi=hi,
                n=n,
                mean_predicted=sum(p for p, _ in in_bucket) / n,
                mean_actual=sum(a for _, a in in_bucket) / n,
            )
        )
    return buckets
