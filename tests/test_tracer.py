"""Property tests for the mastery estimator (pure math, no DB).

We assert the qualitative guarantees the tracer must keep (TECHNICAL_DESIGN §7.3, §10):
mastery rises with correct answers, uncertainty shrinks with evidence and regrows with
time, partial credit lands between, and surprising results move ability more.
"""

import math

import pytest
from pydantic import ValidationError

from app.learning.tracer import (
    DEFAULT_UNCERTAINTY,
    MIN_UNCERTAINTY,
    Estimate,
    GlickoEstimator,
    MasteryEstimator,
    aggregate,
)


@pytest.fixture
def est() -> GlickoEstimator:
    return GlickoEstimator()


def test_default_estimate_is_average_and_unsure() -> None:
    e = Estimate()
    assert e.ability == 0.0
    assert e.uncertainty == DEFAULT_UNCERTAINTY


def test_estimate_is_immutable() -> None:
    e = Estimate()
    with pytest.raises(ValidationError):
        e.ability = 1.0  # type: ignore[misc]


def test_glicko_satisfies_protocol(est: GlickoEstimator) -> None:
    assert isinstance(est, MasteryEstimator)


def test_correct_answer_raises_ability(est: GlickoEstimator) -> None:
    after = est.update(Estimate(), score=1.0, difficulty=0.0)
    assert after.ability > 0.0


def test_wrong_answer_lowers_ability(est: GlickoEstimator) -> None:
    after = est.update(Estimate(), score=0.0, difficulty=0.0)
    assert after.ability < 0.0


def test_expected_score_leaves_ability_unchanged(est: GlickoEstimator) -> None:
    # At ability == difficulty, E = 0.5; answering exactly 0.5 is no news.
    after = est.update(Estimate(), score=0.5, difficulty=0.0)
    assert after.ability == pytest.approx(0.0)


def test_partial_credit_lands_between(est: GlickoEstimator) -> None:
    low = est.update(Estimate(), score=0.3, difficulty=0.0).ability
    mid = est.update(Estimate(), score=0.6, difficulty=0.0).ability
    high = est.update(Estimate(), score=0.9, difficulty=0.0).ability
    assert low < mid < high


def test_uncertainty_shrinks_with_evidence(est: GlickoEstimator) -> None:
    e = Estimate()
    prev = e.uncertainty
    for _ in range(10):
        e = est.update(e, score=1.0, difficulty=0.0)
        assert e.uncertainty < prev
        prev = e.uncertainty
    assert e.uncertainty >= MIN_UNCERTAINTY


def test_uncertainty_never_drops_below_floor(est: GlickoEstimator) -> None:
    # Already at the floor, the most-informative item (E=0.5) cannot push lower.
    e = Estimate(uncertainty=MIN_UNCERTAINTY)
    for _ in range(20):
        e = est.update(e, score=0.5, difficulty=e.ability)
        assert e.uncertainty >= MIN_UNCERTAINTY
    assert e.uncertainty == pytest.approx(MIN_UNCERTAINTY, abs=1e-6)


def test_surprising_result_moves_ability_more(est: GlickoEstimator) -> None:
    # A correct answer on a hard item (low expected) is more informative than on an
    # easy one, so it should raise ability further.
    prior = Estimate()
    easy = est.update(prior, score=1.0, difficulty=-2.0)  # E high, small surprise
    hard = est.update(prior, score=1.0, difficulty=2.0)  # E low, big surprise
    assert hard.ability > easy.ability


def test_expected_is_monotonic_and_centred(est: GlickoEstimator) -> None:
    assert est.expected(Estimate(), difficulty=0.0) == pytest.approx(0.5)
    weak = est.expected(Estimate(ability=-2.0), difficulty=0.0)
    strong = est.expected(Estimate(ability=2.0), difficulty=0.0)
    assert 0.0 < weak < 0.5 < strong < 1.0


def test_score_is_clamped(est: GlickoEstimator) -> None:
    over = est.update(Estimate(), score=5.0, difficulty=0.0)
    one = est.update(Estimate(), score=1.0, difficulty=0.0)
    assert over.ability == pytest.approx(one.ability)


def test_decay_grows_uncertainty_and_caps(est: GlickoEstimator) -> None:
    confident = est.update(Estimate(), score=1.0, difficulty=0.0)
    later = est.decay(confident, elapsed_days=30.0)
    assert later.uncertainty > confident.uncertainty
    assert later.ability == confident.ability  # decay forgets *certainty*, not the estimate
    way_later = est.decay(confident, elapsed_days=10_000.0)
    assert way_later.uncertainty == pytest.approx(DEFAULT_UNCERTAINTY)


def test_decay_zero_elapsed_is_noop(est: GlickoEstimator) -> None:
    e = est.update(Estimate(), score=1.0, difficulty=0.0)
    assert est.decay(e, elapsed_days=0.0).uncertainty == pytest.approx(e.uncertainty)


def test_partial_weight_moves_ability_less(est: GlickoEstimator) -> None:
    # Apportioned credit (weight < 1) is weaker evidence: smaller move, less sharpening.
    full = est.update(Estimate(), score=1.0, difficulty=0.0, weight=1.0)
    half = est.update(Estimate(), score=1.0, difficulty=0.0, weight=0.5)
    assert 0.0 < half.ability < full.ability
    assert half.uncertainty > full.uncertainty


def test_aggregate_empty_is_unknown() -> None:
    e = aggregate([])
    assert e.ability == 0.0
    assert e.uncertainty == DEFAULT_UNCERTAINTY


def test_aggregate_of_identical_is_identity() -> None:
    child = Estimate(ability=1.5, uncertainty=0.3)
    assert aggregate([child, child, child]).ability == pytest.approx(1.5)
    assert aggregate([child, child, child]).uncertainty == pytest.approx(0.3)


def test_aggregate_untested_children_stay_unknown() -> None:
    e = aggregate([Estimate(), Estimate()])
    assert e.ability == pytest.approx(0.0)
    assert e.uncertainty == pytest.approx(DEFAULT_UNCERTAINTY)


def test_aggregate_known_child_dominates_ability_untested_widens() -> None:
    known = Estimate(ability=2.0, uncertainty=0.1)
    untested = Estimate()  # ability 0, uncertainty 1.0
    e = aggregate([known, untested])
    assert e.ability > 1.5  # precision-weighted toward what we actually measured
    assert known.uncertainty < e.uncertainty < DEFAULT_UNCERTAINTY  # but still wide


def test_aggregate_weights_shift_the_mean() -> None:
    a = Estimate(ability=2.0, uncertainty=0.3)
    b = Estimate(ability=-2.0, uncertainty=0.3)
    light_b = aggregate([a, b], weights=[3.0, 1.0]).ability
    heavy_b = aggregate([a, b], weights=[1.0, 3.0]).ability
    assert heavy_b < light_b  # weighting b more pulls the mean toward b


def test_repeated_correct_answers_converge(est: GlickoEstimator) -> None:
    # Ability should climb but with diminishing steps as it outgrows the item.
    e = Estimate()
    steps = []
    for _ in range(20):
        nxt = est.update(e, score=1.0, difficulty=0.0)
        steps.append(nxt.ability - e.ability)
        e = nxt
    assert all(s > 0 for s in steps)
    assert steps[-1] < steps[0]
    assert math.isfinite(e.ability)
