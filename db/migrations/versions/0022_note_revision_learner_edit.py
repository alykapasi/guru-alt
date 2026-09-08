"""Store the markdown a learner actually submitted, alongside what was made of it.

Absorbing an edit *reinterprets* it into atoms through a model, so the words the learner typed
were the one version of their own note that was never stored anywhere. Nullable because it is
meaningful only on a `learner_edit` revision; existing rows keep NULL rather than pretending
to a fidelity they never had.

Revision ID: 0022_note_revision_learner_edit
Revises: 0021_plan_revision_pending
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_note_revision_learner_edit"
down_revision: str | Sequence[str] | None = "0021_plan_revision_pending"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("note_revisions", sa.Column("learner_edit_md", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("note_revisions", "learner_edit_md")
