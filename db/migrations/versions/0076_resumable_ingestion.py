"""Resumable ingestion (S37): ``sources.stage`` and ``staged_chunks``.

``stage`` is where an interrupted job resumes — NULL (nothing to resume; every existing row),
``embed`` or ``tag``. ``staged_chunks`` holds embedded chunks until publish copies them into
``chunks``; nothing else reads it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0076_resumable_ingestion"
down_revision: str | Sequence[str] | None = "0075_profile_messages_since"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("stage", sa.String(), nullable=True))
    op.create_table(
        "staged_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Uuid(),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        # 768, as chunks.embedding (0007) and memories.embedding (0014): fixed here, not read
        # from settings, so this revision builds the same table everywhere.
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embedding_space", sa.String(), nullable=False),
        sa.Column("pipeline_version", sa.Integer(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_id", "ordinal", name="uq_staged_chunks_ordinal"),
    )
    op.create_index("ix_staged_chunks_source_id", "staged_chunks", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_staged_chunks_source_id", table_name="staged_chunks")
    op.drop_table("staged_chunks")
    op.drop_column("sources", "stage")
