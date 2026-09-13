"""Was the requested difficulty ever delivered? (S12, via O01.)

Every generated item stores the difficulty that was asked for and nothing has checked that the
model delivered it. O01 ruled out the textbook method — many learners on the same item — so
what is measured here is the *generator*: the map from requested to realised difficulty, which
is recoverable across items that share a prompt even when they share no learner.

The truth is constructed in each of these, because the instrument has to be pinned against
arithmetic before it is pointed at learners.
"""

import math

from app.learning.tracer import Estimate
from tests.eval.datasets.calibration import Attempt, replay
from tests.eval.datasets.synth import synthetic_dataset
from tests.eval.reliability import difficulty
from tests.eval.reliability.report import render_difficulty


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --- S12: was the requested difficulty ever delivered? -----------------------


def test_realised_difficulty_recovers_the_value_that_generated_the_answers() -> None:
    """The instrument against arithmetic it cannot argue with: scores generated at a known
    difficulty must solve back to that difficulty."""
    truth = 0.8
    attempts = [
        Attempt(prior_ability=a, difficulty=0.0, score=_sigmoid(a - truth))
        for a in (-2.0, -1.0, 0.0, 1.0, 2.0)
    ]

    solved, saturated = difficulty.solve_difficulty(attempts)

    assert not saturated
    assert solved is not None and abs(solved - truth) < 1e-6


def test_a_generator_running_easy_reads_as_easier_than_it_asked_for() -> None:
    """The finding this exists to make. Two runs identical but for the generator: one delivers
    what it was asked for, the other delivers items half a logit easier. The drift between them
    is the generator's bias, and it is recoverable without any item being answered twice —
    which is the whole point, because O01 ruled out the method that needs that.
    """
    abilities = [-1.5, -0.5, 0.5, 1.5] * 6
    honest = [Attempt(prior_ability=a, difficulty=1.0, score=_sigmoid(a - 1.0)) for a in abilities]
    easy = [Attempt(prior_ability=a, difficulty=1.0, score=_sigmoid(a - 0.5)) for a in abilities]

    honest_d, _ = difficulty.solve_difficulty(honest)
    easy_d, _ = difficulty.solve_difficulty(easy)

    assert honest_d is not None and easy_d is not None
    assert abs(honest_d - 1.0) < 1e-6, "an honest generator reads at what it asked for"
    assert abs(easy_d - 0.5) < 1e-6, "a generator running easy reads half a logit lower"
    assert easy_d < honest_d


def test_a_band_everyone_passed_is_saturated_rather_than_given_a_number() -> None:
    """Every answer right says "easier than the search can express", not a difficulty. Clamping
    silently would publish the search bound as a measurement."""
    attempts = [Attempt(prior_ability=a, difficulty=0.0, score=1.0) for a in (-1.0, 0.0, 1.0)]

    solved, saturated = difficulty.solve_difficulty(attempts)

    assert saturated
    assert solved == -difficulty.BOUND


def test_a_band_nobody_passed_is_saturated_at_the_other_bound() -> None:
    """The mirror of the case above, and a separate branch of the search. Every answer wrong
    says "harder than the search can express"; reporting the bound as a solved difficulty would
    publish the edge of the instrument as a property of the items."""
    attempts = [Attempt(prior_ability=a, difficulty=0.0, score=0.0) for a in (-1.0, 0.0, 1.0)]

    solved, saturated = difficulty.solve_difficulty(attempts)

    assert saturated
    assert solved == difficulty.BOUND


def test_a_saturated_band_is_left_out_of_the_mean_drift() -> None:
    """A bound is not a measurement, so averaging it in would let the search's own edge move a
    number that is supposed to describe the generator."""
    attempts = [
        *[Attempt(prior_ability=a, difficulty=-2.0, score=1.0) for a in (-1.0, 0.0, 1.0)],
        *[
            Attempt(prior_ability=a, difficulty=2.0, score=_sigmoid(a - 2.0))
            for a in (-1.0, 0.0, 1.0, 2.0)
        ],
    ]

    g = difficulty.assess(attempts, bands=2)

    assert g is not None
    assert any(b.saturated for b in g.bands)
    assert g.mean_absolute_drift is not None
    assert g.mean_absolute_drift < 0.01, "only the solvable band contributed"


def test_a_band_with_too_few_attempts_reports_no_number_at_all() -> None:
    attempts = [
        Attempt(prior_ability=0.0, difficulty=d, score=0.5) for d in (-2.0, -2.0, 2.0, 2.0, 2.0)
    ]

    g = difficulty.assess(attempts, bands=2, min_attempts=3)

    assert g is not None
    thin = next(b for b in g.bands if b.n == 2)
    assert thin.realised is None and thin.drift is None


def test_a_map_that_does_not_even_order_its_requests_is_called_out() -> None:
    """The weakest claim the difficulty label has to support. A generator can be wrong about
    magnitude and still rank its requests correctly, which is enough for item selection to
    work; one that inverts them is producing a label carrying no information at all.
    """
    abilities = [-1.0, 0.0, 1.0, 2.0]
    attempts = [
        # asked for easy, delivered hard
        *[Attempt(prior_ability=a, difficulty=-1.0, score=_sigmoid(a - 1.5)) for a in abilities],
        # asked for hard, delivered easy
        *[Attempt(prior_ability=a, difficulty=1.0, score=_sigmoid(a + 1.5)) for a in abilities],
    ]

    g = difficulty.assess(attempts, bands=2)

    assert g is not None
    assert g.usable
    assert not g.monotonic
    assert "NOT MONOTONIC" in render_difficulty(g)


def test_drift_is_signed_because_the_corrections_are_opposite() -> None:
    """A generator running easy inflates every ability built on it; one running hard deflates
    them. An unsigned drift says a band is wrong without saying which way to correct."""
    abilities = [-1.0, 0.0, 1.0, 2.0] * 3
    easy = [Attempt(prior_ability=a, difficulty=1.0, score=_sigmoid(a - 0.2)) for a in abilities]

    g = difficulty.assess(easy, bands=1)

    assert g is not None
    band = g.bands[0]
    assert band.drift is not None and band.drift < 0.0, "delivered easier than requested"


class _ForgettingEstimator:
    """A minimal estimator whose decay pulls ability back toward the population mean.

    Exists only for the test below, and deliberately implements the protocol rather than
    subclassing a candidate: it is a fixture for the replay's contract, not an alternative
    anybody is proposing, and inheriting from one would tie this test to that one's behaviour.

    Glicko's decay grows uncertainty and leaves ability alone, so today "the ability before
    decay" and "the ability after decay" are the same number and nothing can tell which the
    replay recorded. A forgetting model that moves ability is entirely plausible as a future
    estimator; the contract should be pinned before one arrives rather than after it produces
    a quietly wrong difficulty map.
    """

    name = "forgetting"

    @property
    def config(self) -> dict[str, float]:
        return {}

    def expected(self, prior: Estimate, *, difficulty: float) -> float:
        return _sigmoid(prior.ability - difficulty)

    def update(
        self, prior: Estimate, *, score: float, difficulty: float, weight: float = 1.0
    ) -> Estimate:
        return prior

    def decay(self, prior: Estimate, *, elapsed_days: float) -> Estimate:
        return Estimate(ability=prior.ability * 0.5, uncertainty=prior.uncertainty)


def test_the_recorded_ability_is_the_one_predicted_from_not_the_one_before_decay() -> None:
    """Difficulty is solved against the ability the estimator actually held at prediction time.
    Recording the pre-decay ability would attribute to the item whatever forgetting had just
    taken off the learner."""
    from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Seed, Step

    seq = ObservationSequence(
        learner_id="L",
        kc_id="K",
        steps=[Step(score=1.0, difficulty=0.0, elapsed_days=10.0)],
    )
    dataset = CalibrationDataset(sequences=[seq])

    walked = replay(dataset, estimator=_ForgettingEstimator())

    # The learner starts at DEFAULT_ABILITY 0.0, so halve it and nothing moves. Seed above it
    # so pre- and post-decay are different numbers and the assertion can tell them apart.
    assert walked.attempts[0].prior_ability == 0.0

    seeded = dataset.model_copy(
        update={
            "sequences": [
                # A Seed instance, not a dict: model_copy does not validate, so a dict here
                # reaches the replay as a dict and fails on attribute access.
                seq.model_copy(update={"seed": Seed(ability=2.0, uncertainty=0.8, source="t")})
            ]
        }
    )
    walked = replay(seeded, estimator=_ForgettingEstimator())

    assert walked.attempts[0].prior_ability == 1.0, "post-decay (2.0 halved), not the 2.0 before"


def test_the_attempts_come_from_the_same_walk_as_the_predictions() -> None:
    """The input side and the output side of one replay, not two walks that could disagree —
    the drift S56 was written to remove."""
    dataset = synthetic_dataset(n_learners=6, n_items=7, seed=3)

    walked = replay(dataset)

    assert len(walked.attempts) == len(walked.pairs) == 42
    assert difficulty.assess(walked.attempts) is not None
