"""What the review tab is told about evaluation examples (S59).

Two shapes on purpose. An unlabelled example is served as :class:`EvalExampleBlind`, which has
no ``decision`` field at all, so the model's decision cannot reach the labeller before they
judge — not by a forgotten exclude, not by a schema change upstream. Only a saved label returns
:class:`EvalExampleRevealed`.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EvalAreaCounts(BaseModel):
    area: str
    unlabelled: int
    labelled: int
    available: int


class EvalDrawRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)


class EvalDrawResult(BaseModel):
    drawn: int


class EvalExampleBlind(BaseModel):
    id: uuid.UUID
    area: str
    snapshot: dict
    created_at: datetime


class EvalExampleRevealed(EvalExampleBlind):
    decision: dict
    label: dict | None
    note: str | None
    labelled_at: datetime | None


class EvalLabelRequest(BaseModel):
    label: dict
    note: str | None = Field(default=None, max_length=2000)
