"""Exact replay of a mined learner history (S56).

Before model comparison means anything, the replay has to reproduce the state production
actually stored. This module re-walks a mined sequence through the *production* update path —
seed, then decay, then a weight- and assistance-scaled update — and compares each step against
what was recorded at the time.

What it reports is fidelity, not accuracy: a clean run says a replayed history lands where the
real one did, which is the precondition for attributing any later difference to the model
rather than to the harness. Scoring lives in ``calibration.py``.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.learning.tracer import Estimate, MasteryEstimator, estimator_from
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence

# Replay runs the same float operations in the same order, so agreement should be exact up to
# JSON's round-trip of a double. This is a float-noise allowance, not a modelling tolerance:
# widening it to make a run pass would hide exactly the drift the check exists to catch.
REPLAY_TOLERANCE = 1e-9


class StepDeviation(BaseModel):
    index: int
    ability: float
    uncertainty: float


class SequenceReplay(BaseModel):
    learner_id: str
    kc_id: str
    replayable: bool
    # Largest absolute disagreement between replayed and recorded state, over all steps.
    max_ability_error: float | None = None
    max_uncertainty_error: float | None = None
    worst: StepDeviation | None = None
    reason: str | None = None

    @property
    def faithful(self) -> bool:
        if not self.replayable:
            return False
        return (
            self.max_ability_error is not None
            and self.max_uncertainty_error is not None
            and max(self.max_ability_error, self.max_uncertainty_error) <= REPLAY_TOLERANCE
        )


class ReplayReport(BaseModel):
    sequences_total: int
    sequences_replayable: int
    sequences_faithful: int
    max_ability_error: float | None
    max_uncertainty_error: float | None
    failures: list[SequenceReplay]

    @property
    def clean(self) -> bool:
        """Every sequence that claimed to be replayable reproduced its recorded state."""
        return self.sequences_faithful == self.sequences_replayable


def initial_estimate(seq: ObservationSequence) -> Estimate:
    """Where production started this KC: its placement prior, else the population default."""
    if seq.seed is None:
        return Estimate()
    return Estimate(ability=seq.seed.ability, uncertainty=seq.seed.uncertainty)


def replay_sequence(
    seq: ObservationSequence, *, estimator: MasteryEstimator | None = None
) -> list[Estimate]:
    """Re-apply every step the way ``record_observation`` did, returning the state after each.

    The estimator defaults to the one the events were recorded under, rebuilt from the
    configuration they carry — not today's default, which is the substitution that makes a
    replay look like a result.
    """
    if estimator is None:
        if seq.estimator is None or seq.estimator_config is None:
            raise ValueError("sequence does not record which estimator produced it")
        estimator = estimator_from(seq.estimator, seq.estimator_config)
    estimate = initial_estimate(seq)
    out: list[Estimate] = []
    for step in seq.steps:
        if step.weight is None or step.credit is None or step.elapsed_days is None:
            raise ValueError("sequence is missing the inputs an exact replay needs")
        decayed = estimator.decay(estimate, elapsed_days=step.elapsed_days)
        estimate = estimator.update(
            decayed,
            score=step.score,
            difficulty=step.difficulty,
            weight=step.weight * step.credit,
        )
        out.append(estimate)
    return out


def verify_sequence(seq: ObservationSequence) -> SequenceReplay:
    base = SequenceReplay(learner_id=seq.learner_id, kc_id=seq.kc_id, replayable=seq.replayable)
    if not seq.replayable:
        base.reason = "recorded before the replay fields existed"
        return base
    try:
        replayed = replay_sequence(seq)
    except ValueError as exc:
        base.replayable = False
        base.reason = str(exc)
        return base

    worst: StepDeviation | None = None
    max_ability = 0.0
    max_uncertainty = 0.0
    for index, (got, step) in enumerate(zip(replayed, seq.steps, strict=True)):
        assert step.posterior_ability is not None and step.posterior_uncertainty is not None
        d_ability = abs(got.ability - step.posterior_ability)
        d_uncertainty = abs(got.uncertainty - step.posterior_uncertainty)
        max_ability = max(max_ability, d_ability)
        max_uncertainty = max(max_uncertainty, d_uncertainty)
        if worst is None or max(d_ability, d_uncertainty) > max(worst.ability, worst.uncertainty):
            worst = StepDeviation(index=index, ability=d_ability, uncertainty=d_uncertainty)

    base.max_ability_error = max_ability
    base.max_uncertainty_error = max_uncertainty
    base.worst = worst
    return base


def verify_replay(dataset: CalibrationDataset) -> ReplayReport:
    """Replay every sequence and report where the reproduction failed."""
    results = [verify_sequence(seq) for seq in dataset.sequences]
    replayable = [r for r in results if r.replayable]
    return ReplayReport(
        sequences_total=len(results),
        sequences_replayable=len(replayable),
        sequences_faithful=sum(1 for r in replayable if r.faithful),
        max_ability_error=max((r.max_ability_error or 0.0 for r in replayable), default=None),
        max_uncertainty_error=max(
            (r.max_uncertainty_error or 0.0 for r in replayable), default=None
        ),
        failures=[r for r in results if not r.faithful],
    )
