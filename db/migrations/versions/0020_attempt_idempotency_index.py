"""Make a supplied attempt_id an idempotency key: one attempt updates mastery once.

A retried answer submission previously wrote a second full set of observations, so a flaky
network could hand a learner extra mastery evidence for one piece of work. Callers can now
supply the attempt_id; this partial unique index is what makes the guarantee hold under
concurrency rather than only in the happy path.

Uniqueness is per (learner, attempt, KC) because one attempt legitimately writes one row per
tagged KC. Rows with no attempt_id — written before 0019, and non-attempt events such as
placement_seed — are excluded and keep standing alone.

0019's backfill grouped rows by (learner, item, instant), so two genuinely separate answers to
the same item within the same transaction would have been merged under one attempt_id and now
collide. Those extras are given fresh ids first, which restores "one row is its own attempt".

Revision ID: 0020_attempt_idempotency_index
Revises: 0019_learning_event_attempt_id
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_attempt_idempotency_index"
down_revision: str | Sequence[str] | None = "0019_learning_event_attempt_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SPLIT_COLLIDING_ROWS = """
UPDATE learning_events AS e
SET attempt_id = gen_random_uuid()
FROM (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY learner_id, attempt_id, kc_id ORDER BY created_at, id
        ) AS rn
    FROM learning_events
    WHERE attempt_id IS NOT NULL
) AS d
WHERE e.id = d.id AND d.rn > 1
"""

INDEX_NAME = "uq_learning_events_learner_attempt_kc"


def upgrade() -> None:
    op.execute(_SPLIT_COLLIDING_ROWS)
    op.create_index(
        INDEX_NAME,
        "learning_events",
        ["learner_id", "attempt_id", "kc_id"],
        unique=True,
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )


def downgrade() -> None:
    # The id reassignment above is not reversed: the rows it touched were already ambiguous,
    # and the split is the more accurate reading of them.
    op.drop_index(
        INDEX_NAME,
        table_name="learning_events",
        postgresql_where=sa.text("attempt_id IS NOT NULL"),
    )
