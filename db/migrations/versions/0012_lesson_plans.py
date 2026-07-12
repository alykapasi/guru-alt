"""lesson_plans

Revision ID: 0012_lesson_plans
Revises: 0011_learner_profile
Create Date: 2026-07-12 00:00:00.000000

Phase 5. The adaptive teaching policy (MASTERPLAN §4.6): one row per (learner_id, subject_id),
steps carrying per-step status so the plan is a live record of what's been learned, not a
static document.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012_lesson_plans"
down_revision: str | Sequence[str] | None = "0011_learner_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "lesson_plans",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=True),
        sa.Column("pacing", sa.String(), nullable=False),
        sa.Column("example_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reading_level_hint", sa.Float(), nullable=True),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learner_id", "subject_id"),
    )
    op.create_index(
        op.f("ix_lesson_plans_learner_id"), "lesson_plans", ["learner_id"], unique=False
    )
    op.create_index(
        op.f("ix_lesson_plans_subject_id"), "lesson_plans", ["subject_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_lesson_plans_subject_id"), table_name="lesson_plans")
    op.drop_index(op.f("ix_lesson_plans_learner_id"), table_name="lesson_plans")
    op.drop_table("lesson_plans")
