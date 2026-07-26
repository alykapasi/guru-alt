"""conv_sources_citations

Revision ID: 0015_conv_sources_citations
Revises: 0014_memories
Create Date: 2026-07-13 00:00:00.000000

Phase 7. ``conversation_sources`` narrows a conversation's retrieval scope to specific sources
within its subject (no rows = "all sources under the subject"). ``messages.citations`` mirrors
``content_blocks.citations``' shape (thin {marker, chunk_id, source_id} references) for the
chat/agentic/workflow citation-marker protocol — see app/services/turn_common.py.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015_conv_sources_citations"
down_revision: str | Sequence[str] | None = "0014_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "conversation_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "source_id"),
    )
    op.create_index(
        op.f("ix_conversation_sources_conversation_id"),
        "conversation_sources",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_conversation_sources_source_id"),
        "conversation_sources",
        ["source_id"],
        unique=False,
    )
    op.add_column(
        "messages",
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "citations")
    op.drop_index(op.f("ix_conversation_sources_source_id"), table_name="conversation_sources")
    op.drop_index(
        op.f("ix_conversation_sources_conversation_id"), table_name="conversation_sources"
    )
    op.drop_table("conversation_sources")
