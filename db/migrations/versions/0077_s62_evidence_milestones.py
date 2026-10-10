"""Index the last-answer lookup fully, and give learner_kc_state its evidence milestones (S62).

``ix_learning_events_learner_item`` gains ``created_at`` so ``max(created_at)`` for one learner
and item is an index read. The milestone columns say which components could owe a retention or
transfer check, so the checks stop reading every event.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0077_s62_evidence_milestones"
down_revision: str | Sequence[str] | None = "0076_resumable_ingestion"
branch_labels = None
depends_on = None

_INDEX = "ix_learning_events_learner_item"
_WHERE = sa.text("event_type IN ('observation', 'self_report')")


def upgrade() -> None:
    op.drop_index(_INDEX, table_name="learning_events")
    op.create_index(
        _INDEX,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')"), "created_at"],
        unique=False,
        postgresql_where=_WHERE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="learning_events")
    op.create_index(
        _INDEX,
        "learning_events",
        ["learner_id", sa.literal_column("(payload ->> 'item_id')")],
        unique=False,
        postgresql_where=_WHERE,
    )
