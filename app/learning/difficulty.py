"""How hard the next question should be, and how to say that to a model.

Difficulty lives on the same logit scale as ability: an item of difficulty ``d`` is one the
learner has a 50% chance on when their ability is ``d``, because the tracer's expectation is
``sigmoid(theta - d)``. That makes a target difficulty a direct function of what we already
believe about the learner — pick the success rate you want, and the difficulty that produces
it follows.

Which success rate you want depends on what the question is *for*, and the two answers pull
in opposite directions:

* **Practice** wants the learner to mostly succeed. Work they fail three times in four does
  not teach, it discourages, and the tracer learns little from a rout either.
* **Assessment** wants the question that tells us the most. The Fisher information one
  interaction carries is ``E * (1 - E)`` — the estimator computes exactly that term — and it
  is maximised at ``E = 0.5``. The most informative question is the one we genuinely cannot
  predict.

So there is no single right target, and this module does not pretend otherwise: the caller
says which it wants. Placement asks to be informed; the session runner asks to teach.

Pure: no database, no clock, no model calls.
"""

import math

from app.learning.tracer import Estimate

INFORMATIVE_SUCCESS_RATE = 0.5
"""The assessment target. Not a preference — ``E * (1 - E)`` peaks here, so this is where one
answer moves the estimate furthest. Config knobs belong to taste; this one is arithmetic."""

DIFFICULTY_FLOOR = -4.0
DIFFICULTY_CEILING = 4.0
"""Bounds on what we will ask for, so a runaway estimate cannot have us record an absurd
difficulty on a real item.

Wider than the ability scale it serves, deliberately. Abilities beyond about +/-3 are already
past useful (that is a 95% or 5% expectation on an average item), but the target is the ability
*plus the shift the success rate implies* — and for practice that shift points downward. A
range that merely covered plausible abilities would clamp hardest on the learner who is
struggling most, which is exactly the learner who most needs the easier question."""


def target_for(estimate: Estimate, *, success_rate: float) -> float:
    """The item difficulty at which ``estimate``'s learner succeeds ``success_rate`` of the time.

    Inverts ``sigmoid(theta - d) = p`` for ``d``. Clamped to the band range.

    ``uncertainty`` is deliberately unused. A wide estimate arguably ought to pull the target
    toward the informative end — we cannot aim for comfort at an ability we do not know — but
    what that interpolation should be is exactly the kind of number S18 exists to calibrate,
    and inventing one here would bury a guess inside a formula that otherwise follows from the
    estimator's own definition.
    """
    if not 0.0 < success_rate < 1.0:
        raise ValueError(f"success_rate must be strictly between 0 and 1, got {success_rate!r}")
    target = estimate.ability - math.log(success_rate / (1.0 - success_rate))
    return max(DIFFICULTY_FLOOR, min(DIFFICULTY_CEILING, target))


_BANDS: tuple[tuple[float, str, str], ...] = (
    (-1.5, "introductory", "recall, or one direct application of a single idea"),
    (-0.5, "straightforward", "one step of reasoning beyond recall"),
    (0.5, "moderate", "two or three steps, or combining two ideas"),
    (1.5, "challenging", "multi-step reasoning, or the idea applied in an unfamiliar setting"),
    (math.inf, "demanding", "choosing the right approach, then reasoning through several steps"),
)
"""Upper bound (exclusive) -> band name -> what the band means, coarsest thing that survives
contact with a model. A number is not an instruction: "0.65" tells a generator nothing it can
act on, and asking one to calibrate its own output to a logit is asking for noise dressed as
precision. Five bands is about as fine as a model can reliably hit, and the bins are one logit
wide, which is roughly the gap between placement's "some" and "strong"."""


def band(difficulty: float) -> str:
    """The band ``difficulty`` falls in."""
    for upper, name, _ in _BANDS:
        if difficulty < upper:
            return name
    return _BANDS[-1][1]  # unreachable: the last bound is +inf


def describe(difficulty: float) -> str:
    """The band plus its gloss, as a phrase to drop into a prompt."""
    for upper, name, gloss in _BANDS:
        if difficulty < upper:
            return f"{name} ({gloss})"
    return f"{_BANDS[-1][1]} ({_BANDS[-1][2]})"
