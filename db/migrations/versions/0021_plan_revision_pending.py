"""Let a lesson plan record that it owes itself a revision.

A graded answer commits mastery first and revises the derived plan afterwards, deliberately
outside that transaction. If the revision failed, the learner was told their answer had failed
even though it had not — and the plan silently fell behind the evidence. It now records the
debt here instead, and the next plan read pays it.

Existing rows are up to date by definition, so they backfill to false. The server default only
exists to make the column NOT NULL on a populated table; it is dropped immediately so the
column's shape matches the model exactly (`poe db-check`).

Revision ID: 0021_plan_revision_pending
Revises: 0020_attempt_idempotency_index
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_plan_revision_pending"
down_revision: str | Sequence[str] | None = "0020_attempt_idempotency_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lesson_plans",
        sa.Column("revision_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("lesson_plans", "revision_pending", server_default=None)


def downgrade() -> None:
    op.drop_column("lesson_plans", "revision_pending")
