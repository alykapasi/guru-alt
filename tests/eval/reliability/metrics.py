"""Is the estimator's number true? (S59, first slice — reliability before validity.)

The tracer publishes a prediction before every attempt. Everything downstream is expressed in
those numbers: "retention improved" is a claim about estimates, "the detour helped" is a
comparison of them. If the estimator is miscalibrated, those readings are not noisy — they are
uninterpretable, and they fail silently. So this is measured before anything tries to establish
that the product teaches.

**Calibration alone is not the answer, and reporting it alone would be misleading.** A forecaster
that ignores its input and always predicts the base rate is *perfectly calibrated*: over many
attempts it says 0.62 and 62% of them succeed. It is also useless, because it says the same thing
about a component the learner has mastered and one they have never seen. Calibration asks whether
the numbers are honest; **resolution** asks whether they are informative. A report carrying only
the first invites exactly the wrong conclusion, which is why every function here comes back in
one object with both, and why the skill score — the comparison against that do-nothing forecaster
— is the headline rather than a footnote.

**On the word Brier.** Strictly it scores binary outcomes. Ours are partial credit: an open answer
can be 0.667 right, and the estimator is updated with that. Mean squared error against a
continuous target is still a proper scoring rule and the Murphy decomposition still holds, so the
continuous reading is the honest default. But "how well do we predict *correctness*" is a
different question from "how well do we predict *partial credit*", and a single number cannot
answer both — so ``binarise_at`` gives the second reading explicitly rather than letting one
silently stand in for the other.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

# Ten buckets over [0, 1]. Enough to see a curve bend, few enough that a first cohort's data
# leaves most of them populated — an empty bucket contributes nothing and quietly narrows what
# the error is averaged over.
DEFAULT_BUCKETS = 10

Pair = tuple[float, float]  # (predicted, actual)


class Bucket(BaseModel):
    """One band of the reliability curve: what we said, against what happened."""

    lo: float
    hi: float
    n: int
    mean_predicted: float
    mean_actual: float

    @property
    def gap(self) -> float:
        """Signed miscalibration. Positive means the estimator was over-confident here."""
        return self.mean_predicted - self.mean_actual


class Reliability(BaseModel):
    """What a run of predictions was worth, and why.

    ``brier`` decomposes exactly as ``reliability - resolution + uncertainty`` (Murphy), and the
    three parts are what make the single number readable:

    * ``reliability`` — squared miscalibration, n-weighted across buckets. **Lower is better**;
      zero means that when the estimator says 0.7, 0.7 is what happens.
    * ``resolution`` — how far the bucket outcomes move away from the base rate. **Higher is
      better**; zero means the predictions carry no information about the outcome, however
      honest they are.
    * ``uncertainty`` — the variance of the outcomes themselves. Not a property of the estimator
      at all: it is the score the base-rate forecaster gets, and the bar to clear.
    """

    n: int
    base_rate: float
    brier: float
    ece: float
    reliability: float
    resolution: float
    uncertainty: float
    skill: float
    buckets: list[Bucket]
    binarised_at: float | None = None

    @property
    def informative(self) -> bool:
        """Did the estimator beat predicting the base rate every time?

        The question a first calibration read exists to answer. A low ``ece`` with ``skill`` at
        or below zero is the failure this whole module is shaped to make visible: honest numbers
        that say nothing.
        """
        return self.skill > 0.0


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def bucketise(pairs: Sequence[Pair], *, buckets: int = DEFAULT_BUCKETS) -> list[Bucket]:
    """Group predictions into equal-width bands. Empty bands are omitted, not zero-filled.

    A band nobody predicted into is an absence of evidence; reporting it as a zero-error band
    would improve every average it appears in.
    """
    out: list[Bucket] = []
    for i in range(buckets):
        lo, hi = i / buckets, (i + 1) / buckets
        last = i == buckets - 1
        inside = [(p, a) for p, a in pairs if lo <= p < hi or (last and p == hi)]
        if not inside:
            continue
        out.append(
            Bucket(
                lo=lo,
                hi=hi,
                n=len(inside),
                mean_predicted=_mean([p for p, _ in inside]),
                mean_actual=_mean([a for _, a in inside]),
            )
        )
    return out


def assess(
    pairs: Sequence[Pair],
    *,
    buckets: int = DEFAULT_BUCKETS,
    binarise_at: float | None = None,
) -> Reliability | None:
    """Score a run of (predicted, actual) pairs. ``None`` when there is nothing to score.

    ``binarise_at`` answers the other question: outcomes at or above the threshold become 1 and
    the rest 0, so the report is about predicting *correctness* rather than partial credit. It
    changes the base rate and therefore every number here, which is why it is recorded on the
    result rather than left to the caller's memory.
    """
    if not pairs:
        return None
    if binarise_at is not None:
        pairs = [(p, 1.0 if a >= binarise_at else 0.0) for p, a in pairs]

    n = len(pairs)
    actuals = [a for _, a in pairs]
    base = _mean(actuals)
    brier = _mean([(p - a) ** 2 for p, a in pairs])

    bs = bucketise(pairs, buckets=buckets)
    # Murphy's decomposition, n-weighted over the same bands the curve is drawn from — so the
    # table and the scalars can never tell different stories.
    reliability = sum(b.n * (b.mean_predicted - b.mean_actual) ** 2 for b in bs) / n
    resolution = sum(b.n * (b.mean_actual - base) ** 2 for b in bs) / n
    uncertainty = _mean([(a - base) ** 2 for a in actuals])
    ece = sum(b.n * abs(b.gap) for b in bs) / n

    # Brier skill score: how much of the base-rate forecaster's error we removed. Zero means we
    # matched a forecaster that ignores the learner entirely; negative means we did worse than
    # one. `uncertainty` is that forecaster's score, so a degenerate run where every outcome is
    # identical has nothing to improve on and no skill is defined — reported as 0.0 rather than
    # dividing by zero, because "no information available" is not "no skill shown".
    skill = 0.0 if uncertainty == 0.0 else 1.0 - brier / uncertainty

    return Reliability(
        n=n,
        base_rate=base,
        brier=brier,
        ece=ece,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        skill=skill,
        buckets=bs,
        binarised_at=binarise_at,
    )
