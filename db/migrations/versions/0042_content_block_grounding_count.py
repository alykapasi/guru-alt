"""record how much grounding a block was actually given

``citations`` cannot answer "was this written from the learner's own material?". An empty list
means either that retrieval found nothing and the block came from general knowledge, or that the
model was handed six snippets and cited none of them. Those are different facts about the block,
and only the first is a statement about the sources (S28).

Nullable with no backfill, deliberately. A block written before this column recorded nothing
about its grounding set, and the set itself was never stored, so there is nothing to reconstruct
from — ``citations`` is a lower bound on what was cited, not a count of what was offered.
Inventing a number would put a measurement on rows that were never measured. NULL says "not
recorded", which is the true thing to say about them.

Revision ID: 0042_content_grounding_count
Revises: 0041_password_reset_throttle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_content_grounding_count"
down_revision: str | Sequence[str] | None = "0041_password_reset_throttle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("content_blocks", sa.Column("grounding_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("content_blocks", "grounding_count")
