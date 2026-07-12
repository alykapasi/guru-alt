"""learner_profiles, profile_dimensions

Revision ID: 0011_learner_profile
Revises: 0010_conversation_goal
Create Date: 2026-07-12 00:00:00.000000

Phase 5. The "how they learn" model (MASTERPLAN §4.8), EAV-shaped so the dimension catalog
(app/learning/profile_estimators.py) can grow or shrink without further migrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011_learner_profile"
down_revision: str | Sequence[str] | None = "0010_conversation_goal"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "learner_profiles",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_learner_profiles_learner_id"), "learner_profiles", ["learner_id"], unique=True
    )
    op.create_table(
        "profile_dimensions",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uncertainty", sa.Float(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("learner_id", "key"),
    )
    op.create_index(
        op.f("ix_profile_dimensions_learner_id"), "profile_dimensions", ["learner_id"], unique=False
    )
    op.create_index(op.f("ix_profile_dimensions_key"), "profile_dimensions", ["key"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_profile_dimensions_key"), table_name="profile_dimensions")
    op.drop_index(op.f("ix_profile_dimensions_learner_id"), table_name="profile_dimensions")
    op.drop_table("profile_dimensions")
    op.drop_index(op.f("ix_learner_profiles_learner_id"), table_name="learner_profiles")
    op.drop_table("learner_profiles")
