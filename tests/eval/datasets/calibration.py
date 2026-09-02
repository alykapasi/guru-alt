"""Prequential tracer-calibration scorer (Phase 9b).

Replays each sequence through the production estimator, predicting each step's expected score
from prior steps only, then updating on the actual — the gold-standard one-step-ahead eval of an
online learner model. Reuses app.learning.tracer so the eval can't drift from what ships.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.learning.tracer import Estimate, GlickoEstimator, MasteryEstimator
from tests.eval.datasets.models import CalibrationDataset

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


def score_tracer_calibration(
    dataset: CalibrationDataset,
    *,
    estimator: MasteryEstimator = _DEFAULT_ESTIMATOR,
    tolerance: float = 0.15,
) -> CalibrationReport:
    """Prequential (predict-then-update) replay; MAE/RMSE + a reliability table over all
    steps.
    """
    pairs: list[tuple[float, float]] = []  # (predicted, actual) across all sequences
    sequences_passed = 0
    for seq in dataset.sequences:
        estimate = Estimate()
        seq_abs_err = 0.0
        for step in seq.steps:
            predicted = estimator.expected(estimate, difficulty=step.difficulty)
            pairs.append((predicted, step.score))
            seq_abs_err += abs(predicted - step.score)
            estimate = estimator.update(estimate, score=step.score, difficulty=step.difficulty)
        if seq.steps and seq_abs_err / len(seq.steps) <= tolerance:
            sequences_passed += 1
    return CalibrationReport(
        mae=_mae(pairs),
        rmse=_rmse(pairs),
        n_points=len(pairs),
        reliability=_reliability(pairs),
        sequences_passed=sequences_passed,
        sequences_total=len(dataset.sequences),
    )


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
