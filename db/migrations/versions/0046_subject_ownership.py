"""give a subject an owner, so a learner's curriculum is theirs

Subjects were global and unscoped (S25). Listing returned everybody's; a duplicate name was
rejected across all learners, so the first person to study Calculus took the name from everyone
after them; and any authenticated learner could add topics, components and prerequisite edges to
any subject, including one somebody else was actively being taught from.

`owner_learner_id` is the boundary, and the NULL is meaningful rather than missing: **NULL means
curated** — shared, visible to everyone, not editable through the learner API. A learner id means
a private curriculum, visible and editable only by its owner.

**Every existing row is backfilled to NULL, i.e. curated, and that is the conservative
direction.** Making them curated leaves them visible to everyone and editable by no one, which
is a loss of capability that shows up immediately and is fixed by assigning an owner. Guessing an
owner instead — from whoever uploaded a source into it, say — would hand one learner private
control of a subject others may already be studying, and that is a change nobody would notice
until the graph moved under them.

CASCADE, because closing an account takes that learner's own curriculum with it. Curated rows
carry NULL and no account deletion touches them; `app.services.retention` states both halves.

Revision ID: 0046_subject_ownership
Revises: 0045_concepts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_subject_ownership"
down_revision: str | Sequence[str] | None = "0045_concepts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subjects", sa.Column("owner_learner_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_subjects_owner_learner_id"), "subjects", ["owner_learner_id"])
    op.create_foreign_key(
        "fk_subjects_owner_learner_id_learners",
        "subjects",
        "learners",
        ["owner_learner_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_subjects_owner_learner_id_learners", "subjects", type_="foreignkey")
    op.drop_index(op.f("ix_subjects_owner_learner_id"), table_name="subjects")
    op.drop_column("subjects", "owner_learner_id")
