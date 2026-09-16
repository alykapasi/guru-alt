"""record how long a streamed call took to start, not only what it cost

0043 gave `llm_calls` a `latency_ms` and said streaming would stay NULL by design: a stream has
no single end, and time-to-first-token and time-to-completion are different questions one column
would blur. That reasoning is still right and its consequence was not thought through — the
streamed calls are the tutoring turn and the refinement gate, so the only calls a learner sits
and waits for were the only ones with no timing at all. Anything reporting latency from this
table would have described grading, curriculum design and embedding, and silently omitted every
turn whose slowness a person actually notices.

So this measures the other half rather than collapsing both into one column. `first_token_ms` is
the model's contribution to a turn feeling slow; the rest of a stream arrives while the learner
is already reading. Time-to-completion for a stream is still not recorded, and is still the
ambiguous one — it would be as much a claim about how long the answer was as about how fast the
model is.

Nullable with no backfill, and NULL keeps meaning "not measured" rather than "instant": no
existing row was timed, a non-streamed call has no first token to time, and neither does a
streamed turn that only called a tool.

Revision ID: 0048_llm_call_first_token
Revises: 0047_learner_is_admin
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048_llm_call_first_token"
down_revision: str | Sequence[str] | None = "0047_learner_is_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("first_token_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_calls", "first_token_ms")
