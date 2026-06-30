"""content_blocks (KC-tagged, cached, grounded generation)

Revision ID: 0008_content
Revises: 0007_ingestion
Create Date: 2026-07-01 10:00:00.000000

The content engine (Phase 4a). A ``content_block`` is generated instruction tagged to one or
more KCs (``kc_ids``, GIN-indexed for containment lookups) and grounded in cited chunks. The
unique ``cache_key`` (a hash over learner + KCs + type + grounding set) makes generation
idempotent: an identical request reuses the stored block.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008_content"
down_revision: str | Sequence[str] | None = "0007_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "content_blocks",
        sa.Column("learner_id", sa.Uuid(), nullable=False),
        sa.Column("kc_ids", postgresql.ARRAY(sa.Uuid()), nullable=False),
        sa.Column("block_type", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cache_key", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["learner_id"], ["learners.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_content_blocks_learner_id"), "content_blocks", ["learner_id"], unique=False
    )
    op.create_index(
        op.f("ix_content_blocks_block_type"), "content_blocks", ["block_type"], unique=False
    )
    op.create_index(
        op.f("ix_content_blocks_cache_key"), "content_blocks", ["cache_key"], unique=True
    )
    op.create_index(
        "ix_content_blocks_kc_ids",
        "content_blocks",
        ["kc_ids"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_content_blocks_kc_ids", table_name="content_blocks")
    op.drop_index(op.f("ix_content_blocks_cache_key"), table_name="content_blocks")
    op.drop_index(op.f("ix_content_blocks_block_type"), table_name="content_blocks")
    op.drop_index(op.f("ix_content_blocks_learner_id"), table_name="content_blocks")
    op.drop_table("content_blocks")
