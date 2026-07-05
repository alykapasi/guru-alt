"""chunk_kcs (per-chunk KC auto-tagging)

Revision ID: 0009_chunk_kcs
Revises: 0008_content
Create Date: 2026-07-03 00:00:00.000000

Phase 4b. A join table linking a chunk to the KC(s) it teaches, with the auto-tagger's
confidence. FK to chunks is ``ON DELETE CASCADE`` so a re-ingest (which replaces a source's
chunks) drops the old tags with them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_chunk_kcs"
down_revision: str | Sequence[str] | None = "0008_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chunk_kcs",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("kc_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id"], ["chunks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kc_id"], ["kcs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chunk_id", "kc_id"),
    )
    op.create_index(op.f("ix_chunk_kcs_chunk_id"), "chunk_kcs", ["chunk_id"], unique=False)
    op.create_index(op.f("ix_chunk_kcs_kc_id"), "chunk_kcs", ["kc_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_chunk_kcs_kc_id"), table_name="chunk_kcs")
    op.drop_index(op.f("ix_chunk_kcs_chunk_id"), table_name="chunk_kcs")
    op.drop_table("chunk_kcs")
