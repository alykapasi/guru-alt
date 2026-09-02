"""Prequential tracer-calibration scorer (Phase 9b)."""

import pytest

from tests.eval.datasets.calibration import score_tracer_calibration
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step
from tests.eval.datasets.synth import synthetic_dataset


def test_prequential_predicts_before_updating() -> None:
    # First step is predicted from the cold-start prior (ability 0): expected(0, d=0) = 0.5.
    seq = ObservationSequence(learner_id="L", kc_id="K", steps=[Step(score=1.0, difficulty=0.0)])
    report = score_tracer_calibration(CalibrationDataset(sequences=[seq]))
    assert report.n_points == 1
    # predicted 0.5 vs actual 1.0 -> MAE 0.5 (proves it predicts BEFORE updating, no peeking)
    assert report.mae == pytest.approx(0.5)


def test_well_calibrated_synthetic_data_scores_within_tolerance() -> None:
    # The CI gate: a correct estimator recovers the synthetic learners' abilities. The 0.2
    # aggregate bound has margin over the estimator's cold-start convergence while staying far
    # below the ~0.5 noise floor the discrimination test relies on. (If this fails just over
    # 0.2, that's cold-start convergence, not a bug: raise n_items for more convergence per
    # learner until it passes with margin — never weaken the discrimination test to compensate.)
    report = score_tracer_calibration(synthetic_dataset(n_learners=8, n_items=25, seed=3))
    assert report.mae is not None and report.mae <= 0.2
    assert report.reliability  # populated
    assert report.sequences_passed >= 1


def test_scorer_discriminates_calibrated_from_noise() -> None:
    good = score_tracer_calibration(synthetic_dataset(n_learners=8, n_items=20, seed=3))
    # Unpredictable noise at fixed difficulty: alternating 0/1 keeps the estimate near 0.5.
    noisy = CalibrationDataset(
        sequences=[
            ObservationSequence(
                learner_id="L",
                kc_id="K",
                steps=[Step(score=float(i % 2), difficulty=0.0) for i in range(20)],
            )
        ]
    )
    noisy_report = score_tracer_calibration(noisy)
    assert good.mae is not None and noisy_report.mae is not None
    assert good.mae < noisy_report.mae  # calibrated data scores strictly better


def test_empty_dataset_yields_no_points_and_no_crash() -> None:
    report = score_tracer_calibration(CalibrationDataset(sequences=[]))
    assert report.n_points == 0
    assert report.mae is None and report.rmse is None
    assert report.reliability == []
    assert report.sequences_total == 0


def test_reliability_buckets_partition_all_points() -> None:
    report = score_tracer_calibration(synthetic_dataset(n_learners=6, n_items=15, seed=9))
    assert report.reliability
    assert all(b.n > 0 for b in report.reliability)
    assert all(0.0 <= b.mean_actual <= 1.0 for b in report.reliability)
    assert report.n_points == sum(b.n for b in report.reliability)  # every point in one bucket
