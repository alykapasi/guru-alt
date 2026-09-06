"""Request/response schemas for assessment items and grading."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.assessment import AUTO_GRADABLE, ItemType


class ItemKCRef(BaseModel):
    """A KC this item assesses, with its apportioning weight."""

    kc_id: uuid.UUID
    weight: float = Field(default=1.0, gt=0.0)


class ItemCreate(BaseModel):
    item_type: ItemType
    stem: str = Field(min_length=1)
    kcs: list[ItemKCRef] = Field(min_length=1)
    answer_key: dict | None = None
    difficulty: float = 0.0
    rubric_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _objective_items_need_a_key(self) -> "ItemCreate":
        if self.item_type in AUTO_GRADABLE and not self.answer_key:
            raise ValueError(f"{self.item_type} items require an answer_key")
        return self


class ItemKCRead(BaseModel):
    kc_id: uuid.UUID
    weight: float


class ItemRead(BaseModel):
    """An item as presented to a learner — the ``answer_key`` is deliberately withheld.

    ``presentation`` carries the part of the answer key the learner legitimately needs to
    answer (an MCQ's ``choices``, never its ``correct`` index). See
    :func:`app.learning.item_presentation.public_presentation`.
    """

    id: uuid.UUID
    item_type: ItemType
    stem: str
    difficulty: float
    rubric_id: uuid.UUID | None
    kcs: list[ItemKCRead]
    presentation: dict | None = None


class AnswerSubmit(BaseModel):
    """A learner's response. ``response`` shape depends on the item type (see grading)."""

    response: dict
    latency_ms: int | None = Field(default=None, ge=0)
    hints_used: int | None = Field(default=None, ge=0)


class KCEstimateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kc_id: uuid.UUID
    ability: float
    uncertainty: float


class GradeRead(BaseModel):
    """The grade plus the per-KC mastery the answer produced."""

    score: float
    correct: bool
    detail: dict
    estimates: list[KCEstimateRead]


class ReviewItemRead(BaseModel):
    """A KC whose FSRS review is due (the review-queue projection).

    ``item`` is an answerable practice item resolved for the KC (typically a flashcard —
    see ``session_runner.due_review_items``), or ``None`` past the request's item-resolution
    cap (``reviews_due_item_limit``) — the due list itself is bounded separately (much more
    generously, see ``mastery.due_reviews``'s ``due_reviews_limit``), only item resolution
    beyond ``reviews_due_item_limit`` is skipped.
    """

    kc_id: uuid.UUID
    due_at: datetime
    ability: float
    uncertainty: float
    item: ItemRead | None = None
