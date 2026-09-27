"""How far a forgotten memory's suppression reaches (S42, decision B).

``learner``: a single-memory Forget or "Forget everything" — never re-extracted from anywhere.
``conversation``: forgotten with the conversation it came from — re-extraction is suppressed
from that conversation only, so another conversation can teach it again. Existing tombstones
become ``learner``: how they were made is unknown, and that is today's behaviour.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0069_memory_forgotten_scope"
down_revision: str | Sequence[str] | None = "0068_learner_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("forgotten_scope", sa.Text(), nullable=True))
    op.execute("UPDATE memories SET forgotten_scope = 'learner' WHERE status = 'deleted'")


def downgrade() -> None:
    op.drop_column("memories", "forgotten_scope")
