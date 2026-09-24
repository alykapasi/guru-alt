"""Add the learner's detour guidance setting (S11).

``lesson_plans.guidance`` is how much say the learner has over a prerequisite detour: a plan
in ``"guided"`` mode takes a detour as soon as it is triggered, exactly as before this slice;
a plan in ``"exploration"`` mode only proposes one, and the learner accepts or skips it
(``app.learning.lesson_plan.decide_detour``). The column is per-plan rather than per-learner
because it is a teaching-style choice about one subject's plan, not a global account setting.

No backfill. The server default ``'guided'`` *is* today's behaviour for every existing plan —
there is nothing to compute, and every row reads correctly without one.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_detour_guidance"
down_revision: str | Sequence[str] | None = "0058_goal_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lesson_plans",
        sa.Column("guidance", sa.Text(), nullable=False, server_default="guided"),
    )


def downgrade() -> None:
    op.drop_column("lesson_plans", "guidance")
