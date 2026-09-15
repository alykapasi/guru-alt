"""record how long each model call took, beside what it cost

``llm_calls`` answered "what did we spend" and could not answer "why did that turn feel slow".
Those separate only with both numbers: a slow turn is either a slow model or a slow product,
and attributing it to the wrong one is the specific mistake S64 exists to prevent during
founder testing.

Nullable with no backfill. Existing rows were never timed, and 0 would claim an instantaneous
call rather than an unmeasured one. Streaming calls stay NULL by design — a stream has no single
end, and time-to-first-token and time-to-completion are different questions that one column
would blur.

Revision ID: 0043_llm_call_latency
Revises: 0042_content_grounding_count
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_llm_call_latency"
down_revision: str | Sequence[str] | None = "0042_content_grounding_count"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("latency_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "latency_ms")
