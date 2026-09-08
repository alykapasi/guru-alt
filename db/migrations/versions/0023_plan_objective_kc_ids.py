"""Store a plan's complete objective, separately from the window it is showing.

Plan generation topologically sorts the goal's prerequisite closure and then slices it to
lesson_plan_max_steps (20). Topo order puts prerequisites first, so the slice drops the tail —
including the actual target, which sorts last. A prerequisite-heavy goal therefore produced a
plan that never reached the thing the learner asked to learn, and finishing that plan looked
like finishing the goal.

The full ordered objective lives here; `steps` stays the active horizon and is extended from
this list as work completes. Existing plans get an empty list, which reads as "no deferred
work" — exactly their behaviour today — and they gain a real objective on their next
regenerate.

Revision ID: 0023_plan_objective_kc_ids
Revises: 0022_note_revision_learner_edit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_plan_objective_kc_ids"
down_revision: str | Sequence[str] | None = "0022_note_revision_learner_edit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lesson_plans",
        sa.Column(
            "objective_kc_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("lesson_plans", "objective_kc_ids", server_default=None)


def downgrade() -> None:
    op.drop_column("lesson_plans", "objective_kc_ids")
