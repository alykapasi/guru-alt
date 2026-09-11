"""a similarity hash, for the near-duplicates equality cannot reach

Both digests already on ``sources`` answer "identical?" — of the bytes, or of the canonical
text. Neither reaches a scan: OCR errors are per-character, so a photographed textbook differs
from its EPUB in thousands of places and no normalisation makes them equal.

``simhash`` is compared by distance instead. It is deliberately unindexed: the search is over
one learner's own sources, which is tens of rows, and a cross-learner search would need LSH
banding for something a learner is not permitted to observe in the first place.

Not backfilled, for the same reason as 0034 — the value only exists after extraction.

Revision ID: 0035_source_simhash
Revises: 0034_source_text_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_source_simhash"
down_revision: str | Sequence[str] | None = "0034_source_text_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("simhash", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "simhash")
