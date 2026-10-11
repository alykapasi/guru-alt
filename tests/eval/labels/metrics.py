"""How far a model's decisions agree with the founder's labels (S59).

Categorical throughout: every area's decision and label is one value from a closed vocabulary,
grading included (as a band). Accuracy is reported beside Cohen's kappa for the reason
``tests/eval/reliability/agreement.py`` gives: raw agreement flatters a model that says the
common thing, and kappa subtracts what chance alone would agree on.

No pass mark. Below :data:`MIN_READABLE` examples the report prints "too few to read" rather
than a figure — a kappa over eight labels is noise with a decimal point.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

MIN_READABLE = 20


@dataclass(frozen=True)
class Categorical:
    n: int
    accuracy: float
    kappa: float | None
    """``None`` when chance agreement is total (both sides used one value): no evidence, not 1.0."""
    confusion: dict[tuple[str, str], int]
    """(model's decision, founder's label) → count."""


def categorical(pairs: Sequence[tuple[str, str]]) -> Categorical | None:
    """``pairs`` are (model, label). ``None`` when there is nothing to compare."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(1 for model, label in pairs if model == label) / n
    model_counts = Counter(model for model, _ in pairs)
    label_counts = Counter(label for _, label in pairs)
    expected = sum(
        (model_counts[c] / n) * (label_counts[c] / n) for c in set(model_counts) | set(label_counts)
    )
    kappa = None if expected >= 1.0 else (observed - expected) / (1.0 - expected)
    return Categorical(n=n, accuracy=observed, kappa=kappa, confusion=dict(Counter(pairs)))
