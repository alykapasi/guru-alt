"""Did the generator deliver the difficulty it was asked for? (S12, via O01.)

Every generated item stores the difficulty that was *requested*. Nothing has ever checked that
the model delivered it, so the number the tracer predicts from is an instruction, not a
measurement — and a systematically easy generator makes every ability estimate drift upward
while every calibration curve still looks fine, because the estimator and the item agree with
each other and both are wrong about the learner.

**Why this shape, and not classical item calibration.** The textbook method separates "this item
is hard" from "this learner is weak" by having many learners answer the same item. O01 ruled
that out: the subject domain is deliberately unrestricted and the first cohort is five to twenty
people, so two learners will never meet the same question, and that evidence will not accumulate
at any cohort size this audience supports. What survives is calibrating **the generator rather
than the item** — every item comes from one prompt asking for difficulty *D*, so the map from
requested to realised difficulty is measurable across thousands of items that share a generator
even when they share no learner.

**Realised difficulty** is the *d* at which the model's total expected score equals the total
observed score, given the ability the estimator held at each attempt. Under ``E = sigmoid(θ - d)``
that sum is strictly decreasing in *d*, so the root is unique and bisection finds it exactly.

**The circularity is real and is not hidden.** The abilities come from an estimator that assumed
the stored difficulties were right, so this measures the *residual* the estimator could not
absorb, not a ground truth. Removing that assumption means estimating abilities and difficulties
jointly, which is a much heavier instrument and a decision for after the first reading. A drift
this method reports is therefore evidence the generator is miscalibrated; a drift of zero is
weaker evidence that it is not.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence

from pydantic import BaseModel

from tests.eval.datasets.calibration import Attempt

DEFAULT_BANDS = 5
"""Bands across the requested-difficulty range. Few, because each needs enough attempts to
solve for a difficulty at all, and a first cohort has little to spread across them."""

BOUND = 6.0
"""Where the search stops. ``sigmoid(6) ≈ 0.9975``: past here a band is saturated — every
answer right or every answer wrong — and the data cannot say how far past."""

MIN_ATTEMPTS = 3
"""Below this a band's realised difficulty is noise wearing a number's clothes."""


class DifficultyBand(BaseModel):
    """One band of requested difficulty, against what the answers say it actually was."""

    lo: float
    hi: float
    n: int
    mean_requested: float
    realised: float | None
    # The root sat at the search bound: every answer in the band went one way, so the data
    # gives a direction and no magnitude. Reported rather than silently clamped, because a
    # clamped bound looks like a measurement.
    saturated: bool = False

    @property
    def drift(self) -> float | None:
        """Realised minus requested. **Positive means harder than asked for.**

        Signed, because the corrections are opposite: a generator running easy inflates every
        ability estimate built on it, one running hard deflates them.
        """
        return None if self.realised is None else self.realised - self.mean_requested


class GeneratorCalibration(BaseModel):
    """What the generator's difficulty requests were actually worth."""

    n: int
    n_scored: int
    bands: list[DifficultyBand]
    mean_absolute_drift: float | None

    @property
    def monotonic(self) -> bool:
        """Does realised difficulty rise with requested difficulty?

        The weakest claim worth making and the first to fail. A generator can be badly
        calibrated in magnitude and still *order* its requests correctly, which is enough for
        item selection to work; one that does not even order them is producing a difficulty
        label that carries no information at all.
        """
        got = [b.realised for b in self.bands if b.realised is not None and not b.saturated]
        return all(earlier <= later for earlier, later in itertools.pairwise(got))

    @property
    def usable(self) -> bool:
        """Enough populated bands to say anything about the map, rather than about one band."""
        return len([b for b in self.bands if b.realised is not None]) >= 2


def _expected_total(attempts: Sequence[Attempt], d: float) -> float:
    return sum(_sigmoid(a.prior_ability - d) for a in attempts)


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def solve_difficulty(attempts: Sequence[Attempt]) -> tuple[float | None, bool]:
    """The ``d`` where expected total score meets observed total. Returns ``(d, saturated)``.

    ``sum(sigmoid(θ_i - d))`` is strictly decreasing in ``d``, so bisection converges on the
    single root. When the observed total lies outside what any ``d`` in range can produce, the
    band is saturated: the answers say "harder than the search can express", not a number.
    """
    if not attempts:
        return None, False
    observed = sum(a.score for a in attempts)
    if _expected_total(attempts, BOUND) > observed:
        return BOUND, True  # even the hardest d over-predicts: harder still than we can say
    if _expected_total(attempts, -BOUND) < observed:
        return -BOUND, True
    lo, hi = -BOUND, BOUND
    for _ in range(60):  # 2*BOUND / 2^60 — far below anything the data supports
        mid = (lo + hi) / 2.0
        if _expected_total(attempts, mid) > observed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0, False


def assess(
    attempts: Sequence[Attempt], *, bands: int = DEFAULT_BANDS, min_attempts: int = MIN_ATTEMPTS
) -> GeneratorCalibration | None:
    """Group attempts by requested difficulty and solve each band's realised difficulty.

    Bands are cut across the *observed* range of requested difficulties rather than a fixed
    scale, because a generator only ever asked for what it asked for and a band outside that
    range would report an absence as a result. ``None`` when there is nothing to score.
    """
    attempts = list(attempts)
    if not attempts:
        return None

    requested = [a.difficulty for a in attempts]
    low, high = min(requested), max(requested)
    width = (high - low) / bands if high > low else 0.0

    out: list[DifficultyBand] = []
    scored = 0
    for i in range(bands if width > 0.0 else 1):
        lo = low + i * width if width > 0.0 else low
        hi = low + (i + 1) * width if width > 0.0 else high
        last = i == (bands - 1 if width > 0.0 else 0)
        inside = [a for a in attempts if lo <= a.difficulty < hi or (last and a.difficulty == hi)]
        if not inside:
            continue
        realised, saturated = (
            solve_difficulty(inside) if len(inside) >= min_attempts else (None, False)
        )
        if realised is not None:
            scored += len(inside)
        out.append(
            DifficultyBand(
                lo=lo,
                hi=hi,
                n=len(inside),
                mean_requested=sum(a.difficulty for a in inside) / len(inside),
                realised=realised,
                saturated=saturated,
            )
        )

    drifts = [
        (b.n, abs(b.drift)) for b in out if b.drift is not None and not b.saturated
    ]  # a saturated band has a bound, not a drift
    mean_drift = sum(n * d for n, d in drifts) / sum(n for n, _ in drifts) if drifts else None
    return GeneratorCalibration(
        n=len(attempts), n_scored=scored, bands=out, mean_absolute_drift=mean_drift
    )
