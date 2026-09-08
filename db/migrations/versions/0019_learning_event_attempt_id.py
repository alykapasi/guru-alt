"""Tie a graded answer's per-KC event fan-out together with a shared attempt_id.

record_observation writes one learning_event per tagged KC, all carrying the same score,
latency, hints and item — only ``payload["weight"]`` differs. Anything counting those rows as
independent learner actions (activity volume, pace, help-seeking, format effectiveness)
therefore counted a three-KC answer three times.

Historical rows are backfilled by grouping an observation's (learner, item, instant), which
is exactly how the fan-out was written. Rows with no item id keep NULL and are treated as
their own attempt.

Revision ID: 0019_learning_event_attempt_id
Revises: 0018_note_per_stream_watermarks
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_learning_event_attempt_id"
down_revision: str | Sequence[str] | None = "0018_note_per_stream_watermarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = """
UPDATE learning_events AS e
SET attempt_id = g.attempt_id
FROM (
    SELECT learner_id,
           created_at,
           payload ->> 'item_id' AS item_id,
           gen_random_uuid()     AS attempt_id
    FROM learning_events
    WHERE event_type = 'observation'
      AND payload ->> 'item_id' IS NOT NULL
    GROUP BY learner_id, created_at, payload ->> 'item_id'
) AS g
WHERE e.learner_id = g.learner_id
  AND e.created_at = g.created_at
  AND e.payload ->> 'item_id' = g.item_id
  AND e.event_type = 'observation'
"""


def upgrade() -> None:
    op.add_column("learning_events", sa.Column("attempt_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_learning_events_attempt_id"), "learning_events", ["attempt_id"], unique=False
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_index(op.f("ix_learning_events_attempt_id"), table_name="learning_events")
    op.drop_column("learning_events", "attempt_id")
