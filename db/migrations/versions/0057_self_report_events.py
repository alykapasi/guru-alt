"""Let the item-lookup index cover self-rated attempts too (S56).

``ix_learning_events_learner_item`` answers "has this learner answered this item, and when",
asked once per candidate item every time practice picks a question. It was partial on
``event_type = 'observation'``; self-rated attempts now write ``'self_report'`` and the same
question is asked of them, so without this the lookup falls back to a scan of the learner's
whole history.

Index only — no data migration. Existing rows keep the ``observation`` type and are read as
demonstrated, which is what they were recorded as.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_self_report_events"
down_revision: str | Sequence[str] | None = "0056_publication"
branch_labels = None
depends_on = None

_NAME = "ix_learning_events_learner_item"


def upgrade() -> None:
    op.drop_index(_NAME, table_name="learning_events")
    op.create_index(
        _NAME,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')")],
        unique=False,
        postgresql_where=sa.text("event_type IN ('observation', 'self_report')"),
    )


def downgrade() -> None:
    op.drop_index(_NAME, table_name="learning_events")
    op.create_index(
        _NAME,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')")],
        unique=False,
        postgresql_where=sa.text("event_type = 'observation'"),
    )
