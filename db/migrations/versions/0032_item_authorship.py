"""item authorship: who may be assessed with an item

`items` is a global table, so any authenticated learner posting one added a question *and its
answer key* to a bank other learners are then examined against (S33). Being signed in is not
authority to author someone else's assessment.

Existing rows become 'generated'. The bank as it stands is generator output; marking it
'learner' with no author would make every item in it invisible to everyone and strand the
plans that reference them. Nothing distinguishes the two retrospectively, so the migration
preserves current behaviour rather than guessing.

Revision ID: 0032_item_authorship
Revises: 0031_honest_profile_proxies
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_item_authorship"
down_revision: str | Sequence[str] | None = "0031_honest_profile_proxies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "items", sa.Column("origin", sa.String(), nullable=False, server_default="generated")
    )
    op.add_column("items", sa.Column("author_learner_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_items_author_learner_id",
        "items",
        "learners",
        ["author_learner_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_items_origin", "items", ["origin"])
    op.create_index("ix_items_author_learner_id", "items", ["author_learner_id"])
    op.alter_column("items", "origin", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_items_author_learner_id", table_name="items")
    op.drop_index("ix_items_origin", table_name="items")
    op.drop_constraint("fk_items_author_learner_id", "items", type_="foreignkey")
    op.drop_column("items", "author_learner_id")
    op.drop_column("items", "origin")
