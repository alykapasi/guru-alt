"""rename profile proxies to what they measure; drop the reading-level hint

Three dimension keys asserted findings the code does not establish (S44). ``reading_level``
was a readability score of the learner's own chat messages — a measure of how they write to a
tutor, not how well they read — and it was fed into note generation as "write at roughly this
reading level", so short casual questions asked for simpler explanations.
``cognitive_load_tolerance`` was a within-session score drift over questions whose difficulty
is not held constant. ``format_effectiveness`` was a mean score per question format, with no
matching on difficulty or topic.

The rows are renamed rather than dropped: the underlying measurements are still worth keeping
and showing, under names that say what they are. ``lesson_plans.reading_level_hint`` is
dropped outright — it existed only to carry the inference into generation.

Revision ID: 0031_honest_profile_proxies
Revises: 0030_turn_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_honest_profile_proxies"
down_revision: str | Sequence[str] | None = "0030_turn_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES = [
    ("reading_level", "message_writing_complexity"),
    ("cognitive_load_tolerance", "within_session_accuracy_drift"),
    ("format_effectiveness", "score_by_format"),
]


def _rename(pairs: Sequence[tuple[str, str]]) -> None:
    for old, new in pairs:
        op.execute(
            sa.text("UPDATE profile_dimensions SET key = :new WHERE key = :old").bindparams(
                new=new, old=old
            )
        )


def upgrade() -> None:
    _rename(_RENAMES)
    op.drop_column("lesson_plans", "reading_level_hint")


def downgrade() -> None:
    op.add_column("lesson_plans", sa.Column("reading_level_hint", sa.Float(), nullable=True))
    _rename([(new, old) for old, new in _RENAMES])
