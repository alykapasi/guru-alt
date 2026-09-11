"""conversation phase + active practice item

Records what a conversation is waiting for instead of leaving the frontend to infer it (S52).
The inference — "no goal and the last message is from the assistant" — could not tell a goal
proposal from an agentic reply given before any goal was committed.

Existing rows get 'chatting', which is the honest default: whatever they were mid-negotiating
lived in an in-memory graph checkpoint that no deploy survives anyway, and 'chatting' is the
state the turn dispatcher already degrades such a conversation to.

Revision ID: 0027_conversation_phase
Revises: 0026_source_job_lease
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_conversation_phase"
down_revision: str | Sequence[str] | None = "0026_source_job_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("phase", sa.String(), nullable=False, server_default="chatting"),
    )
    op.add_column("conversations", sa.Column("active_item_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_conversations_active_item_id",
        "conversations",
        "items",
        ["active_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column("conversations", "phase", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_conversations_active_item_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "active_item_id")
    op.drop_column("conversations", "phase")
