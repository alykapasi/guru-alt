"""Dataset models for mined real-data eval sets (Phase 9b)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Step(BaseModel):
    """One graded observation in a learner's per-KC sequence."""

    score: float
    difficulty: float


class ObservationSequence(BaseModel):
    """Time-ordered observations for one (learner, KC) pair."""

    learner_id: str
    kc_id: str
    steps: list[Step]


class CalibrationDataset(BaseModel):
    """A mined (or synthetic) set of observation sequences."""

    sequences: list[ObservationSequence]

    def to_file(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2))

    @classmethod
    def from_file(cls, path: str | Path) -> CalibrationDataset:
        return cls.model_validate_json(Path(path).read_text())
