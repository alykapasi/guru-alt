"""Does anything predict better than what shipped? (S56.)

Replay fidelity landed with S56 and answered "did what we shipped predict well?". It could not
answer the question that decides anything: would something else have predicted better? A
calibration number alone has no scale.

No reading here — that needs learners. These pin the instrument against data whose truth is
constructed, and against the one arithmetic identity it must satisfy: the null model scores
exactly zero skill, because skill is defined as the improvement over it.
"""

from tests.eval.datasets.synth import synthetic_dataset
from tests.eval.reliability import comparison
from tests.eval.reliability.candidates import ConstantEstimator, EloEstimator
from tests.eval.reliability.report import render_comparison

# --- S56: does anything predict better than what shipped? --------------------


def test_the_null_estimator_scores_exactly_zero_skill() -> None:
    """The harness checking itself.

    Skill is *defined* as the error removed relative to a forecaster that always predicts the
    base rate. Run that forecaster through the same replay and the same metrics and it must
    come out at zero. Anything else means the replay and the metrics disagree about what they
    are measuring, and every other row in the table would be measuring that disagreement too.
    """
    dataset = synthetic_dataset(n_learners=20, n_items=12, seed=5)

    c = comparison.compare(dataset)

    assert c is not None
    null = next(r for r in c.rows if r.name == "constant")
    assert abs(null.skill) < 1e-9


def test_the_null_estimator_learns_nothing_by_construction() -> None:
    """Its predictions ignore ability, so an update that moved ability would not change any
    number in the table — which is exactly why the property needs stating directly rather than
    being left to a scoring test that cannot see it."""
    from app.learning.tracer import Estimate

    null = ConstantEstimator(p=0.4)
    prior = Estimate(ability=0.9, uncertainty=0.3)

    after = null.update(prior, score=1.0, difficulty=-3.0)

    assert after == prior
    assert null.expected(after, difficulty=99.0) == 0.4, "and nothing moves the prediction"


def test_a_responsive_estimator_ranks_above_a_sluggish_one() -> None:
    """Elo with a large step converges within a dozen items; with a small one it does not.

    This is the ordering the table exists to produce — if it could not separate two estimators
    that differ only in how fast they learn, it could not separate any.
    """
    dataset = synthetic_dataset(n_learners=25, n_items=20, seed=11)

    # Handed to the comparison worst-first on purpose: passing them in the order the table
    # should produce lets an unsorted table pass by coincidence.
    c = comparison.compare(dataset, candidates=[EloEstimator(k=0.02), EloEstimator(k=0.4)])

    assert c is not None
    # .get, not [...]: the shipped Glicko row is in this table too and has no k at all.
    quick = next(r for r in c.rows if r.config.get("k") == 0.4)
    slow = next(r for r in c.rows if r.config.get("k") == 0.02)
    assert quick.skill > slow.skill
    assert c.rows.index(quick) < c.rows.index(slow), "rows come back best first"


def test_exactly_one_row_is_what_shipped() -> None:
    dataset = synthetic_dataset(n_learners=8, n_items=8, seed=2)

    c = comparison.compare(dataset)

    assert c is not None
    assert len([r for r in c.rows if r.as_shipped]) == 1
    assert c.shipped is not None and c.shipped.name == "glicko"


def test_the_lead_is_measured_against_the_best_candidate_not_against_itself() -> None:
    """Regression. Reporting the best row's skill minus the shipped row's compares production
    with itself whenever production wins, and prints a lead of exactly zero over a field it in
    fact beat comfortably. The number has to be the distance to the nearest *other* estimator.
    """
    dataset = synthetic_dataset(n_learners=20, n_items=15, seed=9)

    c = comparison.compare(dataset, candidates=[ConstantEstimator(p=0.5)])

    assert c is not None and c.shipped_is_best
    shipped = c.shipped
    assert shipped is not None
    assert c.margin is not None and c.margin > 0.1, "a real lead, not 0.0000"
    assert abs(c.margin - (shipped.skill - c.rows[-1].skill)) < 1e-12


def test_a_candidate_that_wins_is_reported_as_a_finding_not_a_footnote() -> None:
    """Production losing is the only result here that licences changing the estimator, so it
    has to be legible as such — and hedged, because one replay on one dataset is not a mandate.
    """
    dataset = synthetic_dataset(n_learners=15, n_items=15, seed=4)
    c = comparison.compare(dataset, candidates=[EloEstimator(k=0.4)])
    assert c is not None
    # Force the loss rather than hunt for a seed that produces one: the rendering is what is
    # under test, and a seed chosen to make production lose would be testing the fixture.
    beaten = c.model_copy(update={"rows": sorted(c.rows, key=lambda r: r.as_shipped)})

    out = render_comparison(beaten)

    assert "A CANDIDATE BEAT PRODUCTION" in out
    assert "licences investigating it, not shipping it" in out


def test_nothing_to_compare_is_none_rather_than_an_empty_table() -> None:
    from tests.eval.datasets.models import CalibrationDataset

    assert comparison.compare(CalibrationDataset(sequences=[])) is None
