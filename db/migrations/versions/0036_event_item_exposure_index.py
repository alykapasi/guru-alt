"""which items a learner has already answered

Practice now picks the item this learner has gone longest without answering, rather than the
oldest one in the bank (S14). That asks "when did this learner last answer this item" once per
candidate, and the item id lives inside the event payload rather than in a column — so without
an expression index each of those is a scan of every observation the learner has ever
produced.

Partial on ``event_type = 'observation'`` because only observations carry an item at all;
placement seeds and anything later do not.

Revision ID: 0036_event_item_exposure
Revises: 0035_source_simhash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_event_item_exposure"
down_revision: str | Sequence[str] | None = "0035_source_simhash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "ix_learning_events_learner_item"


def upgrade() -> None:
    op.create_index(
        _NAME,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')")],
        unique=False,
        postgresql_where=sa.text("event_type = 'observation'"),
    )


def downgrade() -> None:
    op.drop_index(
        _NAME,
        table_name="learning_events",
        postgresql_where=sa.text("event_type = 'observation'"),
    )
