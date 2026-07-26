"""conversation_kind

Revision ID: 0016_conversation_kind
Revises: 0015_conv_sources_citations
Create Date: 2026-07-13 00:00:00.000000

Phase 7. ``conversations.kind`` distinguishes a plain "chat" (refinement-gate + tutor-turn
flow) from a "session" (a guided-practice workflow run created by the Lessons page) — the
frontend needs this to route a conversation to the right page, since a session's paused
workflow reply must never be mistaken for the chat gate's goal-negotiation prompt.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_conversation_kind"
down_revision: str | Sequence[str] | None = "0015_conv_sources_citations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "conversations",
        sa.Column("kind", sa.String(), server_default="chat", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("conversations", "kind")
