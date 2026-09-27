"""Explicit learner preferences, global and per subject (S02, V09).

Backfills each lesson plan's non-default guidance as a subject override, so nobody's choice
changes. A plan on the default needs no row: it resolves to guided either way. The old column
cannot tell a plan explicitly switched back to guided from one never touched, so neither gets a
row (see the spec). ``lesson_plans.guidance`` stays, unread, until a later cleanup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0068_learner_preferences"
down_revision: str | Sequence[str] | None = "0067_account_deletion_erasures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "learner_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_learner_preferences_learner_id", "learner_preferences", ["learner_id"])
    op.create_index(
        "uq_learner_preferences_scope_key",
        "learner_preferences",
        ["learner_id", "subject_id", "key"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.execute(
        "INSERT INTO learner_preferences (id, learner_id, subject_id, key, value) "
        "SELECT gen_random_uuid(), learner_id, subject_id, 'guidance', guidance "
        "FROM lesson_plans WHERE guidance <> 'guided'"
    )


def downgrade() -> None:
    op.drop_index("uq_learner_preferences_scope_key", table_name="learner_preferences")
    op.drop_index("ix_learner_preferences_learner_id", table_name="learner_preferences")
    op.drop_table("learner_preferences")
