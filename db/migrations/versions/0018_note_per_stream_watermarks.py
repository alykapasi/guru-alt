"""Split Note.watermark into per-stream cursors.

Catch-up distillation pages two independent activity streams (subject messages and KC
outcome events) with separate limits. One shared cursor advanced to the newest row across
both, so whichever stream its page had truncated lost the remainder permanently. Each
stream now carries its own cursor, seeded from the old shared value.

Revision ID: 0018_note_per_stream_watermarks
Revises: 0017_notes
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_note_per_stream_watermarks"
down_revision: str | Sequence[str] | None = "0017_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WATERMARK_EPOCH = sa.text("'1970-01-01 00:00:00'")


def upgrade() -> None:
    op.add_column(
        "notes",
        sa.Column(
            "messages_watermark", sa.DateTime(), nullable=False, server_default=WATERMARK_EPOCH
        ),
    )
    op.add_column(
        "notes",
        sa.Column(
            "events_watermark", sa.DateTime(), nullable=False, server_default=WATERMARK_EPOCH
        ),
    )
    # Seed both from the value they replace: never re-read consumed activity, never skip.
    op.execute("UPDATE notes SET messages_watermark = watermark, events_watermark = watermark")
    op.drop_column("notes", "watermark")


def downgrade() -> None:
    op.add_column(
        "notes",
        sa.Column("watermark", sa.DateTime(), nullable=False, server_default=WATERMARK_EPOCH),
    )
    # The safe inverse is the EARLIER cursor: re-reading activity is recoverable, skipping is not.
    op.execute("UPDATE notes SET watermark = LEAST(messages_watermark, events_watermark)")
    op.drop_column("notes", "messages_watermark")
    op.drop_column("notes", "events_watermark")
