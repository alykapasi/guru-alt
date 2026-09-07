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
    """A plan, plus how much of its objective is not yet in the plan.

    ``steps`` is the window the learner is working on, not the whole goal: a goal needing more
    components than the step cap keeps the rest deferred and pulls them in as work completes.
    ``objective_kc_count``/``deferred_kc_count`` exist so finishing the window is not presented
    as finishing the goal. Both are 0 on plans generated before objectives were recorded.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    goal: str | None
    pacing: str
    example_tags: list[str]
    reading_level_hint: float | None
    steps: list[LessonStepRead]
    objective_kc_count: int = 0
    deferred_kc_count: int = 0
    updated_at: datetime
