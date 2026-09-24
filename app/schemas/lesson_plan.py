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
    # Set only on a "detour" step (S11): the component the learner was actually working
    # towards when a prerequisite turned out to be blocking them, and why the detour was
    # taken. Without these a detour is indistinguishable from the plan simply changing its
    # mind about the order, which is the thing a learner would reasonably lose trust over.
    detour_for: uuid.UUID | None = None
    detour_reason: str | None = None


class GoalStatusRead(BaseModel):
    """What is and is not known about the learner's progress toward this plan's goal.

    Four separate facts, deliberately not collapsed into one enum: a goal can be achieved and
    stale, or closed and unfinished, and an enum would need a member per combination.
    """

    # Every component the goal needs. 0 on plans generated before objectives were recorded,
    # which is why a consumer must read 0 as "unknown" and not as "nothing left to do".
    objective_kc_count: int
    # Components with a recorded achievement. Historical: never decreases. Overlaps both
    # counts below — an achieved component is also current or stale — so these three must
    # never be summed.
    achieved_kc_count: int
    # Measured, evidence still within the window, and the estimate *decayed to now* meets
    # the bar: "they can do this today".
    current_kc_count: int
    # Measured, evidence older than the window, and the estimate *as of that measurement*
    # met the bar: "we saw them do this, and it was a long time ago". Disjoint from
    # `current_kc_count` by the freshness test; the other estimate is used on purpose, or a
    # component that drifted would fail the bar *because* it is old and fall out of both.
    stale_kc_count: int
    # Set when every objective component has been achieved; the latest of their dates.
    achieved_at: datetime | None
    # The learner's own closure. Independent of everything above, and with no path to any
    # estimate: closing a goal does not fabricate assessment evidence.
    closed_at: datetime | None


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
    steps: list[LessonStepRead]
    objective_kc_count: int = 0
    deferred_kc_count: int = 0
    # Computed, never stored. The default exists so `model_validate(plan)` stays total
    # against an ORM row that has no such attribute; `plan_read` always overwrites it, and
    # an all-zero status renders as nothing at all (objective_kc_count 0 means "unknown").
    goal_status: GoalStatusRead = GoalStatusRead(
        objective_kc_count=0,
        achieved_kc_count=0,
        current_kc_count=0,
        stale_kc_count=0,
        achieved_at=None,
        closed_at=None,
    )
    updated_at: datetime
