"""Request/response schemas for the lesson plan."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LessonPlanSubmit(BaseModel):
    goal: str | None = None


class LessonStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kc_id: uuid.UUID
    order: int
    step_type: str
    status: str
    target_difficulty: float | None
    hint_density: str | None
    preferred_item_type: str | None


class LessonPlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    goal: str | None
    pacing: str
    example_tags: list[str]
    reading_level_hint: float | None
    steps: list[LessonStepRead]
    updated_at: datetime
