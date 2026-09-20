"""Reviewed publication, plus the two privacy fixes that needed this migration to exist.

Three things land together because they share one reshaping of `subjects`:

**Publication (S25b).** `publications` records a request, the frozen snapshot the reviewer
judged, and what was decided. `subjects` gains the columns describing a published life:
`publication_id` on the copy, `superseded_by_id` when a newer version replaces it, and
`withdrawn_at`/`withdrawn_reason`. Foreign keys are SET NULL and the handles are kept as text,
so the record outlives the accounts and subjects involved — the same reason `impersonations`
does it.

**D5 — existing subjects are assumed source-derived.** Nothing ever recorded whether a subject
was generated from uploads, so the backfill sets the flag on every learner-owned row. The
direction is deliberate: a wrong "no" publishes somebody's private material, a wrong "yes" only
means recreating a subject without uploads.

**D8 — slugs go per-owner.** `subjects.slug` was globally unique, and the de-duplication suffix
that produced therefore answered "does a stranger have a subject by this name?" for any name
worth trying. Two constraints replace the one: `uq_subjects_owner_slug` keeps one learner's own
subjects distinct, and because Postgres does not treat NULL owners as equal, a partial unique
index keeps curated subjects — the shared library, which everybody sees — unique among
themselves.

**Order matters twice.** `publications` references `subjects` and `subjects.publication_id`
references `publications`, so the table is created first and the column added last; there is no
way to do both in one step. And on the way back down, the slug de-duplication must run *before*
the global unique index is restored — see `downgrade`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0056_publication"
down_revision: str | Sequence[str] | None = "0055_retire_passwords"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- D4/D5: the source-derived flag -------------------------------------------------
    op.add_column(
        "subjects",
        sa.Column(
            "private_source_derived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        "UPDATE subjects SET private_source_derived = true WHERE owner_learner_id IS NOT NULL"
    )

    # --- The published life of a subject -------------------------------------------------
    op.add_column(
        "subjects",
        sa.Column(
            "superseded_by_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("subjects", sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("subjects", sa.Column("withdrawn_reason", sa.Text(), nullable=True))

    # --- D8: slug uniqueness goes per-owner ----------------------------------------------
    # `0002` created this with `op.f`, so it is dropped by the same name it was given.
    op.drop_index(op.f("ix_subjects_slug"), table_name="subjects")
    # Still indexed, just no longer unique: `list_subjects` orders by it.
    op.create_index(op.f("ix_subjects_slug"), "subjects", ["slug"])
    op.create_unique_constraint("uq_subjects_owner_slug", "subjects", ["owner_learner_id", "slug"])
    # Owned rows are covered above. This covers the curated ones, which that constraint cannot
    # see, because NULL owners do not compare equal to one another.
    op.create_index(
        "uq_subjects_curated_slug",
        "subjects",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("owner_learner_id IS NULL"),
    )

    # --- D4: what the server observed at generation time ---------------------------------
    op.create_table(
        "curriculum_proposals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "learner_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("grounded_in_sources", sa.Boolean(), nullable=False),
        # Naive, matching TimestampMixin — `poe db-check` catches the mismatch if these drift.
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_curriculum_proposals_learner_id", "curriculum_proposals", ["learner_id"])

    # --- The publication record ------------------------------------------------------------
    op.create_table(
        "publications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "author_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Nullable so retention can clear it with the account (see `app.services.retention`).
        sa.Column("author_handle", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("author_note", sa.Text(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "reviewer_id",
            sa.Uuid(),
            sa.ForeignKey("learners.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewer_handle", sa.String(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "excluded_item_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "published_subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_publications_source_subject_id", "publications", ["source_subject_id"])
    op.create_index("ix_publications_author_id", "publications", ["author_id"])
    op.create_index("ix_publications_status", "publications", ["status"])
    # One open request per subject. In the database rather than in a service check, because two
    # concurrent requests would both read "none pending" and both insert.
    op.create_index(
        "uq_publications_one_pending",
        "publications",
        ["source_subject_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # Last: this closes the cycle between the two tables. Named explicitly to match the model,
    # which declares it `use_alter` for the same reason this is happening down here.
    op.add_column(
        "subjects",
        sa.Column(
            "publication_id",
            sa.Uuid(),
            sa.ForeignKey(
                "publications.id", ondelete="SET NULL", name="fk_subjects_publication_id"
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("subjects", "publication_id")
    op.drop_table("publications")
    op.drop_index("ix_curriculum_proposals_learner_id", table_name="curriculum_proposals")
    op.drop_table("curriculum_proposals")

    # Before the global unique index comes back, and this is the whole reason this downgrade is
    # more than a mirror of the upgrade. The revision above made per-owner slugs legal, so the
    # database may now hold two rows the old index forbids — and recreating it would then fail
    # on real data, during a rollback, having passed every empty-database round-trip in CI.
    #
    # Renaming loses nothing that matters: slugs are display and ordering only, never a lookup
    # key. Oldest row first, so the rename is deterministic and the row most likely to be linked
    # to keeps the name it had.
    op.execute(
        """
        UPDATE subjects SET slug = subjects.slug || '_' || ranked.position
        FROM (
            SELECT id, row_number() OVER (PARTITION BY slug ORDER BY created_at, id) AS position
            FROM subjects
        ) AS ranked
        WHERE subjects.id = ranked.id AND ranked.position > 1
        """
    )
    # A rename can itself collide, if a subject genuinely named `calculus_2` already exists. That
    # is left to fail loudly rather than looped over: it needs a human to choose the new name,
    # and a retry loop here would be guessing on their behalf in the middle of a rollback.
    op.drop_index("uq_subjects_curated_slug", table_name="subjects")
    op.drop_constraint("uq_subjects_owner_slug", "subjects", type_="unique")
    op.drop_index(op.f("ix_subjects_slug"), table_name="subjects")
    op.create_index(op.f("ix_subjects_slug"), "subjects", ["slug"], unique=True)

    op.drop_column("subjects", "withdrawn_reason")
    op.drop_column("subjects", "withdrawn_at")
    op.drop_column("subjects", "superseded_by_id")
    op.drop_column("subjects", "private_source_derived")
