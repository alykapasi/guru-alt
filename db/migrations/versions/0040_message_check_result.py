"""keep the grade a reply reported, past the reply

The learner-facing report for a graded conversational answer (S15) belonged to the turn and
nothing else: the SSE frame carried it, the page rendered it, and a reload lost it. The grade
itself was never at risk — ``learning_events`` has held it since Phase 3 — but the account of
it was, and a learner who wants to disagree with a mark they were given yesterday needs the
account, not the row.

Nullable with no backfill, deliberately. A message written before this column existed reported
no grade the learner could read, and that is the true thing to say about it. Reassembling one
from the event log would mean re-deriving the prior ability, the component split and the
recurrence count as they stood at the time — writing a statement the learner was never shown
and presenting it as the transcript.

Revision ID: 0040_message_check_result
Revises: 0039_learner_auth
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_message_check_result"
down_revision: str | Sequence[str] | None = "0039_learner_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("check_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("messages", "check_result")
