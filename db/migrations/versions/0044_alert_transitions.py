"""record when an alert condition starts and stops firing

Alerts are evaluated on demand, which answers "is anything wrong now" and cannot answer "was
anything wrong at three in the morning". A condition could fire, resolve, and leave nobody any
the wiser — which was the honest gap in the alerting entry, beside the absence of anything that
polls at all.

**Transitions, not evaluations.** Recording every poll would write a row a minute forever and
bury the two rows anybody wants. Recording only changes means the table's length is the number
of things that actually happened, the current state of a condition is the `firing` flag on its
most recent row, and a condition with no rows has never fired.

`detail` and `action` are stored rather than joined: they are what the alert *said at the time*,
and a threshold retuned later would otherwise silently rewrite the history of every incident it
was involved in.

Ordering is by `seq`, a database sequence, and not by `created_at`. `now()` in Postgres is the
*transaction* timestamp, so every row one sweep writes carries the same value and ordering by it
falls through to a random UUID — which would let "the latest row for this condition" be the
wrong one whenever a sweep recorded more than one transition. The same tie has been the defect
behind three earlier findings (S56, S14, S62).

Revision ID: 0044_alert_transitions
Revises: 0043_llm_call_latency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044_alert_transitions"
down_revision: str | Sequence[str] | None = "0043_llm_call_latency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("firing", sa.Boolean(), nullable=False),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq"),
    )
    # Leading on `name` so it also serves "the latest row for this condition"; there is
    # deliberately no second index on `name` alone.
    op.create_index("ix_alert_transitions_name_seq", "alert_transitions", ["name", "seq"])
    op.create_index(op.f("ix_alert_transitions_created_at"), "alert_transitions", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_alert_transitions_created_at"), table_name="alert_transitions")
    op.drop_index("ix_alert_transitions_name_seq", table_name="alert_transitions")
    op.drop_table("alert_transitions")
