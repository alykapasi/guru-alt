"""Do two graders say the same thing? (S59, first slice — the half that is not self-judged.)

The rubric grader produces the partial credit every mastery estimate is built from. Its scores
have been checked against a small set of human labels, which measures *accuracy against those
ten cases*; it has never been checked against a second grader, which measures whether the score
is a property of the answer or a property of one prompt on one model on one day.

S59's standing requirement is that the production grader must not be the sole judge of its own
teaching. Concordance is the available form of that: there is no ground truth for "how right is
this paragraph", only the degree to which independent judges land in the same place.

**Raw agreement is the misleading number here, and it is the one that looks best.** If most
answers are correct, two graders who both say "correct" almost always will agree ~90% of the
time while sharing no judgement whatsoever — a grader that returns 1.0 unconditionally would
score just as well. Cohen's kappa subtracts the agreement two *independent* graders would reach
by chance given how often each uses each band, so it is the number to read and the reason raw
agreement is reported beside it rather than instead of it.

Nothing here calls a model. These are functions over paired scores, so the metrics are
deterministic and testable offline; obtaining a second grader's scores is a paid, manual step
(``tests/eval/reliability/report.py``), on the same footing as the other paid eval tasks.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

# Score bands for the chance-corrected view. Partial credit is continuous, and kappa needs
# categories — these are the bands the feedback language already distinguishes (wrong, mostly
# wrong, half, mostly right, right), so the categories match a distinction the product makes
# rather than one invented for the statistic.
DEFAULT_BANDS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)

Pair = tuple[float, float]  # (grader_a, grader_b)


class Agreement(BaseModel):
    """How far two graders' scores coincide, before and after correcting for chance."""

    n: int
    mean_absolute_difference: float
    max_absolute_difference: float
    correlation: float | None
    raw_band_agreement: float
    kappa: float | None
    bands: list[float]

    @property
    def interpretation(self) -> str:
        """Landis & Koch's conventional labels, stated as convention rather than as a threshold.

        Deliberately not a pass mark. What kappa a grader must reach before its scores can carry
        a mastery estimate is a decision nobody has made (S18, S59), and inventing one here would
        be the guess those entries exist to stop.
        """
        if self.kappa is None:
            return "undefined (one grader used a single band)"
        k = self.kappa
        if k < 0.0:
            return "worse than chance"
        if k < 0.20:
            return "slight"
        if k < 0.40:
            return "fair"
        if k < 0.60:
            return "moderate"
        if k < 0.80:
            return "substantial"
        return "almost perfect"


def band_of(score: float, bands: Sequence[float] = DEFAULT_BANDS) -> int:
    """Which band a score falls in. Lower edges are exclusive, so 0.2 sits above the 0.2 cut."""
    return sum(1 for edge in bands if score > edge)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _pearson(pairs: Sequence[Pair]) -> float | None:
    """``None`` when either grader is constant — correlation is undefined, not zero.

    The distinction matters: a grader that gives every answer 1.0 has no correlation to report,
    and calling that 0.0 would read as "no relationship found" when the truth is "no question
    could be asked".
    """
    if len(pairs) < 2:
        return None
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    mx, my = _mean(xs), _mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0.0 or syy == 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    return sxy / (sxx * syy) ** 0.5


def _kappa(pairs: Sequence[Pair], bands: Sequence[float]) -> tuple[float, float | None]:
    """Raw band agreement, and Cohen's kappa over the same bands.

    Kappa is ``None`` when chance agreement is total — which happens when both graders used one
    band for everything. There is nothing left for a judge to disagree about, so the correction
    divides by zero; that is an absence of evidence, not perfect agreement, and returning 1.0
    would be the most flattering possible lie.
    """
    n = len(pairs)
    a_bands = [band_of(a, bands) for a, _ in pairs]
    b_bands = [band_of(b, bands) for _, b in pairs]
    observed = sum(1 for x, y in zip(a_bands, b_bands, strict=True) if x == y) / n

    categories = set(a_bands) | set(b_bands)
    expected = sum((a_bands.count(c) / n) * (b_bands.count(c) / n) for c in categories)
    if expected >= 1.0:
        return observed, None
    return observed, (observed - expected) / (1.0 - expected)


def compare(pairs: Sequence[Pair], *, bands: Sequence[float] = DEFAULT_BANDS) -> Agreement | None:
    """Score two graders against each other. ``None`` when there is nothing to compare."""
    if not pairs:
        return None
    diffs = [abs(a - b) for a, b in pairs]
    raw, kappa = _kappa(pairs, bands)
    return Agreement(
        n=len(pairs),
        mean_absolute_difference=_mean(diffs),
        max_absolute_difference=max(diffs),
        correlation=_pearson(pairs),
        raw_band_agreement=raw,
        kappa=kappa,
        bands=list(bands),
    )
