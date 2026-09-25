"""Record per-component achievement and learner goal closure (S01).

``learner_kc_state.achieved_at`` is when a component first met the achievement bar — a
conservative estimate at or above the bar with retention demonstrated. Set once and never
cleared: later evidence can lower the current estimate, which is what the estimate is for,
but it cannot unmake a demonstration that happened.

``lesson_plans.goal_closed_at`` is the learner's own choice to finish or archive the goal. It
has no path to any estimate — closing a goal does not fabricate assessment evidence.

No backfill, for two reasons. ``achieved_at`` means "the moment this was first earned", and
there is no such moment in the record for existing rows: the retention rule they would be
judged against is the one S14 just replaced, and applying the new rule retroactively would
date every achievement to this migration. A NULL on a component the learner has genuinely
mastered is self-correcting — the next unassisted demonstration sets it. The cost of not
backfilling is a date that starts late for existing learners; the cost of backfilling is a
date that is wrong for all of them.

Neither column is indexed: both are read as part of rows already fetched by the
``(learner_id, kc_id)`` unique constraint or by primary key, and neither is a filter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_goal_policy"
down_revision: str | Sequence[str] | None = "0057_self_report_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learner_kc_state",
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lesson_plans",
        sa.Column("goal_closed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lesson_plans", "goal_closed_at")
    op.drop_column("learner_kc_state", "achieved_at")
