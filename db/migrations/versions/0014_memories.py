"""memories

Revision ID: 0014_memories
Revises: 0013_conversation_subject
Create Date: 2026-07-12 00:00:00.000000

Phase 5 (final slice). Per-learner memory: durable facts/preferences/summaries extracted from
conversations, retrieved by embedding similarity. ``embedding`` is ``Vector(768)`` — same
constraint as ``chunks.embedding`` (0007): it must match ``GURU_EMBED_DIM`` and the EMBED
model's output; changing the dim is a new migration. No ``tsv`` column (unlike ``chunks``) —
retrieval is vector-only for v1.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0014_memories"
down_revision: str | Sequence[str] | None = "0013_conversation_subject"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_memories_learner_id"), "memories", ["learner_id"], unique=False)
    op.create_index(
        op.f("ix_memories_conversation_id"), "memories", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_memories_kind"), "memories", ["kind"], unique=False)
    op.create_index(
        "ix_memories_embedding_hnsw",
        "memories",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_memories_embedding_hnsw", table_name="memories")
    op.drop_index(op.f("ix_memories_kind"), table_name="memories")
    op.drop_index(op.f("ix_memories_conversation_id"), table_name="memories")
    op.drop_index(op.f("ix_memories_learner_id"), table_name="memories")
    op.drop_table("memories")
