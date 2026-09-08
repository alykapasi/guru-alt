"""incremental cursors for memory extraction and profile refresh

Both were on-demand recomputes with no record of what they had already read (S43): memory
extraction took the last N messages regardless of what it had seen, so a conversation growing
by more than N between runs had the middle skipped, and a profile refresh recomputed every
dimension from scratch even when no new evidence existed.

Existing rows get NULL cursors, which read as "nothing processed yet". For memory that means
the first write-back after this migration re-reads a window it may have seen before — a
duplicate extraction the near-duplicate check already discards, which is the safe direction.

Revision ID: 0028_refresh_cursors
Revises: 0027_conversation_phase
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_refresh_cursors"
down_revision: str | Sequence[str] | None = "0027_conversation_phase"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("memory_watermark", sa.DateTime(), nullable=True))
    op.add_column("learner_profiles", sa.Column("evidence_watermark", sa.DateTime(), nullable=True))
    op.add_column("learner_profiles", sa.Column("refreshed_at", sa.DateTime(), nullable=True))
    op.add_column("learner_profiles", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("learner_profiles", "last_error")
    op.drop_column("learner_profiles", "refreshed_at")
    op.drop_column("learner_profiles", "evidence_watermark")
    op.drop_column("conversations", "memory_watermark")
