"""who owns an onboarding negotiation, durably

The goal-refinement gate's state now survives a restart (S17), so the record of who owns it has
to as well: a resumable negotiation nobody can prove ownership of can only be refused, which
discards it exactly as surely as losing the state did. This table replaces the in-process dict
that was deliberately kept exactly as weak as the in-memory checkpointer behind it.

Revision ID: 0038_onboarding_sessions
Revises: 0037_conversation_scaffolds
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_onboarding_sessions"
down_revision: str | Sequence[str] | None = "0037_conversation_scaffolds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        # CASCADE: a deleted learner's onboarding sessions are meaningless, and leaving them
        # would keep a thread key addressable after the person it belonged to is gone.
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_onboarding_sessions_learner_id", "onboarding_sessions", ["learner_id"])


def downgrade() -> None:
    op.drop_index("ix_onboarding_sessions_learner_id", table_name="onboarding_sessions")
    op.drop_table("onboarding_sessions")
