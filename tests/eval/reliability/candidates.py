"""Alternative estimators to score the shipped one against (S56).

Replay fidelity landed with S56 and answered "did what we shipped predict well?". It could not
answer the question that decides anything: **would something else have predicted better?** A
calibration number on its own has no scale — 0.87 skill is good or bad only relative to what
another estimator gets on the same sequences.

These live in the eval package rather than in ``app.learning.tracer`` on purpose. Nothing ships
them, and a candidate in the production module is a candidate somebody can accidentally
configure. Promoting one is a separate decision that should follow a reading, not precede it.

``app.learning.tracer.estimator_from`` deliberately refuses a name it does not ship, so that a
*fidelity* replay cannot silently substitute today's estimator for yesterday's. That guard is
not in the way here: a candidate comparison passes the estimator object directly and recomputes
every prediction, which is exactly the distinction ``replay(dataset, estimator=...)`` draws.
"""

from __future__ import annotations

# ``_sigmoid`` is imported rather than reimplemented, private though it is. A candidate has to
# use the *identical* logistic to the estimator it is being compared against, or a difference in
# numerical handling at the tails shows up in the table as a modelling result. Copying it here
# would be a second definition free to drift from the first.
from app.learning.tracer import DEFAULT_UNCERTAINTY, Estimate, _sigmoid


class ConstantEstimator:
    """Predicts the same number for every learner and every item, and never learns.

    The null model, and the anchor for reading every other row. A forecaster that says 0.62
    about a mastered component and an unseen one is worthless, and this is that forecaster
    made explicit — scored through the same replay and the same metrics as the real ones.

    It is in the table to be *beaten*, and to check the harness: constructed at a dataset's own
    base rate, its skill score has to come out at zero, because the skill score is defined as
    the improvement over exactly this. A comparison that gives it anything else is measuring
    something other than what it claims.
    """

    name = "constant"

    def __init__(self, *, p: float = 0.5) -> None:
        self._p = p

    @property
    def config(self) -> dict[str, float]:
        return {"p": self._p}

    def expected(self, prior: Estimate, *, difficulty: float) -> float:
        return self._p

    def update(
        self, prior: Estimate, *, score: float, difficulty: float, weight: float = 1.0
    ) -> Estimate:
        return prior  # learns nothing, by construction

    def decay(self, prior: Estimate, *, elapsed_days: float) -> Estimate:
        return prior


class EloEstimator:
    """Classic Elo: one fixed step size, no uncertainty, no time term.

    The honest floor for "does the Bayesian machinery earn its keep?". Glicko's update weights
    each observation by the information it carries and shrinks uncertainty as evidence arrives;
    Elo moves by ``k * (score - expected)`` whether it is the first answer or the hundredth.
    If the two score alike on real sequences, the uncertainty machinery is costing complexity
    for nothing — which is a finding, not a failure.

    Uncertainty is held at the default rather than modelled. It is not part of Elo, and the
    ``Estimate`` it must return has nowhere to say "not applicable"; every consumer of this
    estimator's output in the eval path reads ``ability`` only.
    """

    name = "elo"

    def __init__(self, *, k: float = 0.4) -> None:
        self._k = k

    @property
    def config(self) -> dict[str, float]:
        return {"k": self._k}

    def expected(self, prior: Estimate, *, difficulty: float) -> float:
        return _sigmoid(prior.ability - difficulty)

    def update(
        self, prior: Estimate, *, score: float, difficulty: float, weight: float = 1.0
    ) -> Estimate:
        e = self.expected(prior, difficulty=difficulty)
        return Estimate(
            ability=prior.ability + self._k * weight * (score - e),
            uncertainty=DEFAULT_UNCERTAINTY,
        )

    def decay(self, prior: Estimate, *, elapsed_days: float) -> Estimate:
        return prior  # Elo has no time term; forgetting is invisible to it
