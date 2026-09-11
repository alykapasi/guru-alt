"""Continuous mastery estimation — the swappable core of the knowledge tracer.

A learner's mastery of one knowledge component (KC) is a continuous ability ``θ``
paired with an ``uncertainty`` (a Glicko rating deviation). Each graded interaction is
treated as a "match" between the learner and an item of some difficulty; the logistic
expectation ``E = sigmoid(θ - d)`` drives a precision-weighted Bayesian update.

This module is **pure** — no database, no LLM, no clock. It is the math behind the
``MasteryEstimator`` interface (Elo/Glicko now, DKT later, see TECHNICAL_DESIGN §7.2-7.3);
the service layer (Phase 3 slice 2) loads/stores ``Estimate``s in ``LearnerKCState`` and
rolls them up the knowledge graph.

Scale note: we work in **logits** (natural units), not the doc's illustrative base-10/400
Elo scale. ``ability`` is centred at 0 = population average and ``uncertainty`` starts at
1.0 — matching the ``LearnerKCState`` defaults — and ``E = sigmoid(θ - d)`` is the Rasch/1PL form.
"""

import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_ABILITY = 0.0
"""θ for an unseen KC: the population average."""

DEFAULT_UNCERTAINTY = 1.0
"""Prior rating deviation: wide == "we don't know yet". Also the cap as memory fades."""

MIN_UNCERTAINTY = 0.05
"""We never claim perfect certainty — keeps the estimator responsive to new evidence."""


class Estimate(BaseModel):
    """A continuous mastery estimate on the logit scale: ability ``θ`` + its uncertainty.

    Immutable value object — updates return a new ``Estimate`` rather than mutating.
    """

    model_config = ConfigDict(frozen=True)

    ability: float = DEFAULT_ABILITY
    uncertainty: float = Field(default=DEFAULT_UNCERTAINTY, gt=0.0)


@runtime_checkable
class MasteryEstimator(Protocol):
    """Pure mastery math behind a swappable seam (Glicko/Elo now, DKT later).

    Implementations carry no I/O: they map a prior ``Estimate`` plus one observation to a
    posterior. The service layer owns persistence, multi-KC apportioning, and roll-up.
    """

    name: str

    @property
    def config(self) -> dict[str, float]:
        """The parameters this instance was built with, as plain JSON-able values.

        Recorded alongside every observation so a replay can rebuild the estimator *as it was
        configured then*. Without it a replay silently uses today's constants against
        yesterday's events and reports the difference as a modelling result (S56).
        """
        ...

    def expected(self, prior: Estimate, *, difficulty: float) -> float:
        """P(correct) for an item of ``difficulty`` given ``prior`` — in [0, 1]."""
        ...

    def update(
        self, prior: Estimate, *, score: float, difficulty: float, weight: float = 1.0
    ) -> Estimate:
        """Fold one graded interaction (``score`` ∈ [0, 1], partial credit) into the estimate.

        ``weight`` < 1 scales the evidence down — used to apportion a multi-KC item's credit.
        """
        ...

    def decay(self, prior: Estimate, *, elapsed_days: float) -> Estimate:
        """Grow uncertainty with elapsed time — stale knowledge is less certain."""
        ...


class GlickoEstimator:
    """Glicko-style Bayesian estimator on the logit scale.

    Expectation is logistic, ``E = sigmoid(θ - d)``. The update treats one interaction as a
    single observation of Fisher information ``E*(1 - E)`` and combines it with the prior
    precision ``1/RD²`` — so the posterior uncertainty **strictly shrinks** with evidence,
    and an unexpected result (``score`` far from ``E``) moves ability more than an expected
    one. ``decay`` regrows uncertainty as ``√(RD² + c²·t)`` (Glicko's time term).
    """

    name = "glicko"

    def __init__(
        self,
        *,
        volatility: float = 0.05,
        max_uncertainty: float = DEFAULT_UNCERTAINTY,
    ) -> None:
        self._c = volatility
        self._rd_max = max_uncertainty

    @property
    def config(self) -> dict[str, float]:
        return {"volatility": self._c, "max_uncertainty": self._rd_max}

    def expected(self, prior: Estimate, *, difficulty: float) -> float:
        return _sigmoid(prior.ability - difficulty)

    def update(
        self, prior: Estimate, *, score: float, difficulty: float, weight: float = 1.0
    ) -> Estimate:
        s = _clamp(score, 0.0, 1.0)
        e = self.expected(prior, difficulty=difficulty)
        info = weight * e * (1.0 - e)  # Fisher information this (apportioned) item carries
        new_var = 1.0 / (1.0 / prior.uncertainty**2 + info)
        ability = prior.ability + new_var * weight * (s - e)
        uncertainty = _clamp(math.sqrt(new_var), MIN_UNCERTAINTY, self._rd_max)
        return Estimate(ability=ability, uncertainty=uncertainty)

    def decay(self, prior: Estimate, *, elapsed_days: float) -> Estimate:
        grown = math.sqrt(prior.uncertainty**2 + self._c**2 * max(elapsed_days, 0.0))
        return Estimate(ability=prior.ability, uncertainty=min(grown, self._rd_max))


def estimator_from(name: str, config: dict[str, float]) -> MasteryEstimator:
    """Rebuild the estimator an event was recorded under.

    Raises on a name we no longer ship rather than substituting today's default: a replay that
    quietly changes estimator is worse than one that refuses, because its output still looks
    like a measurement of the learner.
    """
    if name != GlickoEstimator.name:
        raise ValueError(f"no estimator named {name!r} — cannot replay these events")
    return GlickoEstimator(
        volatility=float(config["volatility"]),
        max_uncertainty=float(config["max_uncertainty"]),
    )


def _sigmoid(x: float) -> float:
    """Numerically stable logistic."""
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def aggregate(estimates: Sequence[Estimate], weights: Sequence[float] | None = None) -> Estimate:
    """Roll child estimates up to a parent node (KC → topic → subject, §7.4).

    The point estimate is a **precision-weighted** mean — children we're certain about
    inform it more, so an untested child (wide uncertainty) barely moves the ability.
    The uncertainty is the **coverage-weighted RMS** of child uncertainties, so those same
    untested children *widen* the parent — exactly the dashboard's "Calculus 62% (wide)".

    ``weights`` (default equal) express coverage/importance — e.g. a subject weights its
    topics by KC count. An empty input or all-zero weights yields the unknown prior.
    """
    if not estimates:
        return Estimate()
    w = [1.0] * len(estimates) if weights is None else list(weights)
    total = sum(w)
    if total <= 0.0:
        return Estimate()
    variance = sum(wi * e.uncertainty**2 for wi, e in zip(w, estimates, strict=True)) / total
    precisions = [wi / e.uncertainty**2 for wi, e in zip(w, estimates, strict=True)]
    ability = sum(p * e.ability for p, e in zip(precisions, estimates, strict=True)) / sum(
        precisions
    )
    uncertainty = _clamp(math.sqrt(variance), MIN_UNCERTAINTY, DEFAULT_UNCERTAINTY)
    return Estimate(ability=ability, uncertainty=uncertainty)
