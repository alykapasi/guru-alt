"""rubrics, items, item_kcs

Revision ID: 0005_assessment
Revises: 0004_kc_state_tz
Create Date: 2026-06-30 19:41:37.940922

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_assessment"
down_revision: str | Sequence[str] | None = "0004_kc_state_tz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "rubrics",
        sa.Column("kc_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["kc_id"], ["kcs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rubrics_kc_id"), "rubrics", ["kc_id"], unique=False)
    op.create_table(
        "items",
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("stem", sa.String(), nullable=False),
        sa.Column("answer_key", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("difficulty", sa.Float(), nullable=False),
        sa.Column("rubric_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["rubric_id"], ["rubrics.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_items_item_type"), "items", ["item_type"], unique=False)
    op.create_table(
        "item_kcs",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("kc_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kc_id"], ["kcs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "kc_id"),
    )
    op.create_index(op.f("ix_item_kcs_item_id"), "item_kcs", ["item_id"], unique=False)
    op.create_index(op.f("ix_item_kcs_kc_id"), "item_kcs", ["kc_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_item_kcs_kc_id"), table_name="item_kcs")
    op.drop_index(op.f("ix_item_kcs_item_id"), table_name="item_kcs")
    op.drop_table("item_kcs")
    op.drop_index(op.f("ix_items_item_type"), table_name="items")
    op.drop_table("items")
    op.drop_index(op.f("ix_rubrics_kc_id"), table_name="rubrics")
    op.drop_table("rubrics")
