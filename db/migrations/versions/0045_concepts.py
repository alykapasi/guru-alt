"""give the same concept one identity across subjects

A `KC` belongs to exactly one topic of one subject, so derivatives taught in Calculus and
derivatives taught in Physics were two unrelated ids with nothing anywhere recording that they
were about the same thing. A learner who had studied one looked, to every query in the system,
exactly like a learner who had studied neither (S24).

`concepts` is that missing identity, and a `KC` becomes a *presentation* of one. The column is
nullable because a KC whose concept cannot be determined is a real state and inventing one for
it would be asserting an identity nobody claimed; `SET NULL` for the same reason — losing the
concept row must leave the curriculum standing, not delete it.

**The backfill is a claim, and a deliberately weak one.** Existing KCs are grouped by the
canonical form of their name, so two rows called "Derivatives" come out sharing a concept. That
is evidence about names, not proof the two are the same thing, which is exactly why nothing
downstream is allowed to treat a shared concept as transferred mastery: "Functions" in Calculus
and "Functions" in a programming course would merge here, and a system that silently marked the
second mastered because of the first would stop teaching something the learner had never seen.
The identity is reported so a person can act on it. It is not applied on their behalf.

The canonical form must match `app.services.knowledge.concept_key`, so it is spelled out in SQL
here rather than imported: a migration that calls into application code runs against whatever
that code says *today*, not what it said when the revision was written.

Revision ID: 0045_concepts
Revises: 0044_alert_transitions
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_concepts"
down_revision: str | Sequence[str] | None = "0044_alert_transitions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _canonical(column: str) -> str:
    """lower, strip anything that is not a letter, digit or space, collapse runs of whitespace
    to a single "-", and trim. Mirrors `knowledge.concept_key`, which has a test running both
    over the same names and asserting they agree.

    The column is qualified by the caller rather than written bare: `concepts` also has a
    `name`, so the unqualified form is ambiguous in the UPDATE below and Postgres refuses it.
    """
    return (
        f"trim(both '-' from regexp_replace("
        f"regexp_replace(lower({column}), '[^a-z0-9\\s]', '', 'g'), '\\s+', '-', 'g'))"
    )


def upgrade() -> None:
    op.create_table(
        "concepts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_concepts_key"), "concepts", ["key"], unique=True)

    op.add_column("kcs", sa.Column("concept_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_kcs_concept_id"), "kcs", ["concept_id"])
    op.create_foreign_key(
        "fk_kcs_concept_id_concepts", "kcs", "concepts", ["concept_id"], ["id"], ondelete="SET NULL"
    )

    # One concept per distinct canonical name. `min(name)` picks a stable display name among
    # the presentations that produced the key — they differ only in case and punctuation, and
    # an arbitrary one of those is better than a second column nobody maintains.
    canonical = _canonical("kcs.name")
    op.execute(
        f"""
        INSERT INTO concepts (id, key, name, created_at, updated_at)
        SELECT gen_random_uuid(), {canonical} AS key, min(kcs.name), now(), now()
        FROM kcs
        WHERE {canonical} <> ''
        GROUP BY {canonical}
        """
    )
    op.execute(
        f"""
        UPDATE kcs SET concept_id = concepts.id
        FROM concepts WHERE concepts.key = {canonical}
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_kcs_concept_id_concepts", "kcs", type_="foreignkey")
    op.drop_index(op.f("ix_kcs_concept_id"), table_name="kcs")
    op.drop_column("kcs", "concept_id")
    op.drop_index(op.f("ix_concepts_key"), table_name="concepts")
    op.drop_table("concepts")
