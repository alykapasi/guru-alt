"""The estimator's numbers have to be both honest and informative (S59).

The failure these guard against is not a wrong number — it is a *flattering* one. A forecaster
that ignores the learner and always predicts the base rate scores perfectly on every calibration
measure there is, and a report that leads with calibration would call it excellent.
"""

from tests.eval.reliability import metrics


def test_a_perfect_forecaster_scores_zero_error_and_full_skill() -> None:
    pairs = [(1.0, 1.0), (0.0, 0.0), (1.0, 1.0), (0.0, 0.0)]

    r = metrics.assess(pairs)

    assert r is not None
    assert r.brier == 0.0
    assert r.ece == 0.0
    assert r.skill == 1.0
    assert r.informative


def test_the_base_rate_forecaster_is_perfectly_calibrated_and_worthless() -> None:
    """The headline test, and the reason this module reports skill beside calibration.

    This forecaster says 0.5 about every attempt and is right that half of them succeed. Its
    calibration error is zero. It knows nothing about any learner: it would say 0.5 about a
    component that has been mastered and one never seen. A report leading with `ece` would
    describe it as a flawless estimator.
    """
    pairs = [(0.5, 1.0), (0.5, 0.0)] * 25

    r = metrics.assess(pairs)

    assert r is not None
    assert r.ece == 0.0, "it is genuinely calibrated"
    assert r.reliability == 0.0
    assert r.resolution == 0.0, "and carries no information at all"
    assert r.skill == 0.0
    assert not r.informative


def test_a_forecaster_that_is_worse_than_knowing_nothing_scores_negative_skill() -> None:
    """Backwards predictions: confident and wrong, every time."""
    pairs = [(0.9, 0.0), (0.1, 1.0)] * 10

    r = metrics.assess(pairs)

    assert r is not None
    assert r.skill < 0.0
    assert not r.informative


def test_over_confidence_shows_as_a_positive_gap_in_the_bucket_it_happened_in() -> None:
    """The curve has to localise the miscalibration, not just total it — 'over-confident above
    0.8' and 'over-confident everywhere' need different fixes."""
    pairs = [(0.9, 0.0)] * 5 + [(0.9, 1.0)] * 5  # says 0.9, delivers 0.5

    r = metrics.assess(pairs)

    assert r is not None
    top = [b for b in r.buckets if b.lo >= 0.8]
    assert len(top) == 1
    assert top[0].gap == 0.4
    assert top[0].n == 10


def test_the_murphy_decomposition_adds_back_up_to_the_brier_score() -> None:
    """brier == reliability - resolution + uncertainty.

    Exact here because every forecast sits at a bucket centre, so the bucket mean *is* each
    forecast in it. With forecasts spread inside a bucket the identity holds only up to the
    within-bucket spread of the forecasts, which is a property of range-binning rather than of
    this implementation — hence the deliberately discrete inputs.
    """
    pairs = [(0.05, 1.0), (0.05, 0.0), (0.95, 1.0), (0.95, 1.0), (0.55, 0.0), (0.55, 1.0)]

    r = metrics.assess(pairs)

    assert r is not None
    assert abs(r.brier - (r.reliability - r.resolution + r.uncertainty)) < 1e-12


def test_uncertainty_is_the_score_the_base_rate_forecaster_would_get() -> None:
    """Which is what makes it the bar, rather than another property of our estimator."""
    pairs = [(0.3, 1.0), (0.7, 0.0), (0.2, 1.0), (0.9, 0.0)]

    r = metrics.assess(pairs)
    flat = metrics.assess([(r.base_rate, a) for _, a in pairs]) if r else None

    assert r is not None and flat is not None
    assert abs(r.uncertainty - flat.brier) < 1e-12


def test_a_band_nobody_predicted_into_is_absent_rather_than_a_zero_error_band() -> None:
    """Reporting an empty band as zero error would improve every average it entered."""
    pairs = [(0.05, 0.0), (0.95, 1.0)]

    r = metrics.assess(pairs)

    assert r is not None
    assert len(r.buckets) == 2
    assert sum(b.n for b in r.buckets) == 2


def test_binarising_answers_a_different_question_and_says_so() -> None:
    """Predicting partial credit and predicting correctness are not the same claim, so the
    threshold that turned one into the other is recorded on the result."""
    pairs = [(0.8, 0.7), (0.8, 0.9)]

    partial = metrics.assess(pairs)
    binary = metrics.assess(pairs, binarise_at=0.8)

    assert partial is not None and binary is not None
    assert partial.binarised_at is None
    assert binary.binarised_at == 0.8
    assert partial.base_rate == 0.8
    assert binary.base_rate == 0.5, "0.7 became a miss, 0.9 a hit"
    assert partial.brier != binary.brier


def test_nothing_to_score_is_none_rather_than_a_flattering_zero() -> None:
    assert metrics.assess([]) is None


def test_a_run_with_one_outcome_reports_no_skill_rather_than_dividing_by_zero() -> None:
    """Everyone got it right. There is no base-rate error to improve on, so no skill can be
    demonstrated — which is not the same as demonstrating none."""
    r = metrics.assess([(0.9, 1.0), (0.8, 1.0)])

    assert r is not None
    assert r.uncertainty == 0.0
    assert r.skill == 0.0
    assert not r.informative


def test_calibration_error_is_weighted_by_how_many_attempts_a_band_holds() -> None:
    """A band holding one attempt must not count as much as one holding a hundred.

    Unweighted, a single stray prediction in an otherwise-unused band would dominate the number
    and make a well-calibrated estimator look broken — or, reversed, let a badly calibrated band
    carrying most of the traffic hide behind nine clean ones.
    """
    pairs = [(0.05, 0.05)] * 100 + [(0.95, 0.45)]  # 100 perfect, 1 badly wrong

    r = metrics.assess(pairs)

    assert r is not None
    assert abs(r.ece - (0.5 / 101)) < 1e-9, "n-weighted"
    assert r.ece < 0.01, "not the 0.25 an unweighted mean of the two bands would give"


def test_under_confidence_reads_as_a_negative_gap() -> None:
    """The sign is the diagnosis. Over- and under-confidence need opposite corrections, so a
    gap reported unsigned would say a band is wrong without saying which way."""
    pairs = [(0.15, 1.0)] * 10  # said 0.15, everyone got it right

    r = metrics.assess(pairs)

    assert r is not None
    assert len(r.buckets) == 1
    assert r.buckets[0].gap < 0.0
    assert abs(r.buckets[0].gap + 0.85) < 1e-12


def test_an_outcome_exactly_on_the_binarising_threshold_counts_as_a_hit() -> None:
    """The boundary case, stated rather than left to whichever comparison was typed. A score
    that exactly meets the bar passed it."""
    on_the_line = metrics.assess([(0.5, 0.8), (0.5, 0.0)], binarise_at=0.8)

    assert on_the_line is not None
    assert on_the_line.base_rate == 0.5, "0.8 became a hit, not a miss"
