"""durable conversation turns

A turn had no record of its own (S51): the learner's message committed before generation and
the assistant's only after streaming finished, so anything that interrupted the gap left a
question with no answer and no explanation. ``turns`` records the attempt itself — opened
before generation, closed on every path out of it, and keyed by the client's idempotency key
so a retry is the same turn again rather than a second one saying the same thing.

Nothing backfills: turns that predate this table were never recorded, and inventing
'completed' rows for them would assert an outcome nobody observed.

Revision ID: 0030_turn_lifecycle
Revises: 0029_memory_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_turn_lifecycle"
down_revision: str | Sequence[str] | None = "0029_memory_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("client_turn_id", sa.Uuid(), nullable=True),
        sa.Column("flow", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("user_message_id", sa.Uuid(), nullable=True),
        sa.Column("assistant_message_id", sa.Uuid(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "client_turn_id"),
    )
    op.create_index("ix_turns_conversation_id", "turns", ["conversation_id"])
    op.create_index("ix_turns_status", "turns", ["status"])


def downgrade() -> None:
    op.drop_index("ix_turns_status", table_name="turns")
    op.drop_index("ix_turns_conversation_id", table_name="turns")
    op.drop_table("turns")
