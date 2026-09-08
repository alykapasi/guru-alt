"""The lesson plan: an adaptive teaching policy, not a static document (MASTERPLAN §4.6).

One row per ``(learner_id, subject_id)`` — regenerating replaces ``goal``/``steps``/the
plan-level hints in place, no versioning. ``steps`` carries per-step ``status``
(pending/active/done) so the plan is a live record of what will be learned and what has been
learned, not just a fixed sequence — see ``app/learning/lesson_plan.py``'s ``revise_steps``
for how status/order get recomputed as evidence (graded answers, due reviews, profile shifts)
arrives.
"""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LessonPlan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A learner's adaptive teaching policy for one subject."""

    __tablename__ = "lesson_plans"
    __table_args__ = (UniqueConstraint("learner_id", "subject_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    goal: Mapped[str | None] = mapped_column(Text, default=None)
    pacing: Mapped[str] = mapped_column(default="standard")
    example_tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    reading_level_hint: Mapped[float | None] = mapped_column(default=None)
    # list[dict]: kc_id, order, step_type ("new"|"review"), status ("pending"|"active"|"done"),
    # target_difficulty, hint_density, preferred_item_type.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # The complete prerequisite-ordered objective, as KC id strings. `steps` is only the
    # window of it the learner is working on now: a goal needing more components than
    # lesson_plan_max_steps used to have its tail — including the actual target, which sorts
    # last — silently dropped, so finishing the plan looked like finishing the goal. Empty on
    # plans generated before this existed; they gain one on their next regenerate.
    objective_kc_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)

    @property
    def objective_kc_count(self) -> int:
        """How many components the goal actually needs (0 on plans predating objectives)."""
        return len(self.objective_kc_ids)

    @property
    def deferred_kc_count(self) -> int:
        """Objective components not yet in ``steps``: work finishing the plan will not cover."""
        planned = {step["kc_id"] for step in self.steps if step["step_type"] == "new"}
        return sum(1 for kc_id in self.objective_kc_ids if kc_id not in planned)

    # Set when a revision this plan was owed failed *after* its triggering answer had already
    # committed. Mastery is authoritative and must be reported; the plan is derived, so it
    # records the debt instead and the next read pays it (see services.lesson_plan).
    revision_pending: Mapped[bool] = mapped_column(default=False)
