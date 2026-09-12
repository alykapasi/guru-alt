"""how much help a conversational answer had

A practice item posed in chat is now a check that stays open until the learner answers it
(S15), and a learner who asks for an explanation before attempting has not made an independent
demonstration. This counts the tutor replies given while the question still stood, so the
eventual attempt reaches the tracer discounted the same way a guided-practice hint is.

Not derived from message timestamps: ``created_at`` is transaction time, and the reply that
poses a check is written in a different transaction from the row that records it.

Revision ID: 0037_conversation_scaffolds
Revises: 0036_event_item_exposure
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_conversation_scaffolds"
down_revision: str | Sequence[str] | None = "0036_event_item_exposure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default so existing rows land at 0 rather than NULL: an open check from before
    # this migration has no recorded help, and "none recorded" is the honest reading of it.
    op.add_column(
        "conversations",
        sa.Column("active_item_scaffolds", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("conversations", "active_item_scaffolds")
