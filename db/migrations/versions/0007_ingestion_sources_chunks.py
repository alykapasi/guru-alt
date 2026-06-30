"""sources, chunks (pgvector HNSW + tsvector GIN)

Revision ID: 0007_ingestion
Revises: 0006_kc_state_fsrs
Create Date: 2026-06-30 21:30:00.000000

The ingestion substrate (Phase 4a). ``chunks.embedding`` is ``Vector(768)`` — it must match
``GURU_EMBED_DIM`` and the EMBED model's output (nomic = 768); changing the dim is a new
migration. Hybrid retrieval reads an HNSW index (cosine) on the embedding and a GIN index on
the generated ``tsv`` full-text vector.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007_ingestion"
down_revision: str | Sequence[str] | None = "0006_kc_state_fsrs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "sources",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("blob_key", sa.String(), nullable=True),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("subject_id", sa.Uuid(), nullable=True),
        sa.Column("topic_id", sa.Uuid(), nullable=True),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_id"], ["subjects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sources_learner_id"), "sources", ["learner_id"], unique=False)
    op.create_index(op.f("ix_sources_kind"), "sources", ["kind"], unique=False)
    op.create_index(op.f("ix_sources_status"), "sources", ["status"], unique=False)
    op.create_index(op.f("ix_sources_subject_id"), "sources", ["subject_id"], unique=False)
    op.create_index(op.f("ix_sources_topic_id"), "sources", ["topic_id"], unique=False)

    op.create_table(
        "chunks",
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_chunks_source_id"), "chunks", ["source_id"], unique=False)
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_chunks_tsv", table_name="chunks")
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
    op.drop_index(op.f("ix_chunks_source_id"), table_name="chunks")
    op.drop_table("chunks")
    op.drop_index(op.f("ix_sources_topic_id"), table_name="sources")
    op.drop_index(op.f("ix_sources_subject_id"), table_name="sources")
    op.drop_index(op.f("ix_sources_status"), table_name="sources")
    op.drop_index(op.f("ix_sources_kind"), table_name="sources")
    op.drop_index(op.f("ix_sources_learner_id"), table_name="sources")
    op.drop_table("sources")
