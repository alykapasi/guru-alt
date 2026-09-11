"""Dataset models for mined real-data eval sets (Phase 9b, extended for replay by S56)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Step(BaseModel):
    """One graded observation in a learner's per-KC sequence.

    ``score`` and ``difficulty`` are all a *scorer* needs. Everything below them is what an
    exact *replay* needs, and is present only on events recorded at schema version 2 or later
    (see ``app.learning.mastery.EVENT_SCHEMA_VERSION``). A sequence missing any of it can still
    be scored; it cannot be replayed, and ``ObservationSequence.replayable`` says so rather
    than letting the replay quietly substitute defaults.
    """

    score: float
    difficulty: float

    # How much of the item's evidence this KC received (``weight``) and how much the whole
    # item was discounted for being assisted (``credit``). Production multiplies them; the
    # replay that ignored both applied every multi-KC item at full strength to every KC.
    weight: float | None = None
    credit: float | None = None
    # The decay gap production actually applied, taken from the update's own clock rather
    # than inferred from row timestamps — see the note in ``record_observation``.
    elapsed_days: float | None = None
    # What the model predicted before seeing this answer, and the estimate either side of the
    # update. The posterior is what makes a replay checkable step by step instead of only at
    # the end, which is how a compensating pair of errors used to survive.
    predicted: float | None = None
    prior_ability: float | None = None
    prior_uncertainty: float | None = None
    posterior_ability: float | None = None
    posterior_uncertainty: float | None = None


class Seed(BaseModel):
    """A placement prior set before any evidence existed (``seed_prior``)."""

    ability: float
    uncertainty: float
    source: str


class ObservationSequence(BaseModel):
    """Time-ordered observations for one (learner, KC) pair."""

    learner_id: str
    kc_id: str
    steps: list[Step]
    # A placed learner does not start at the population average. Replaying from the default
    # prior made every sequence for such a learner wrong from its first step.
    seed: Seed | None = None
    # The estimator that produced these events, and the parameters it held at the time.
    estimator: str | None = None
    estimator_config: dict[str, float] | None = None

    @property
    def replayable(self) -> bool:
        """Whether every step carries what an exact replay needs."""
        if self.estimator is None or self.estimator_config is None:
            return False
        return all(
            s.weight is not None
            and s.credit is not None
            and s.elapsed_days is not None
            and s.posterior_ability is not None
            and s.posterior_uncertainty is not None
            for s in self.steps
        )


class CalibrationDataset(BaseModel):
    """A mined (or synthetic) set of observation sequences."""

    sequences: list[ObservationSequence]

    def to_file(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2))

    @classmethod
    def from_file(cls, path: str | Path) -> CalibrationDataset:
        return cls.model_validate_json(Path(path).read_text())
