"""The knowledge graph: Subject → Topic → KC, plus prerequisite edges.

This is the backbone the whole learning engine hangs off (see docs/MASTERPLAN §4.1).
Content, mastery, and sequencing all reference KCs.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Subject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A top-level domain (e.g. "Calculus"), either curated or one learner's own (S25).

    ``owner_learner_id`` is the boundary between the two, and the null is meaningful rather
    than missing data: **NULL means curated** — shared, visible to everyone, and not editable
    through the learner API. A learner id means a private curriculum, visible only to that
    learner and editable only by them.

    Before this every subject was global and unscoped: listing returned everybody's, a
    duplicate name was rejected across all learners so the first person to study Calculus
    took the name from everyone after them, and any authenticated learner could add topics,
    components and prerequisite edges to any subject — including one somebody else was
    actively being taught from.

    **Slugs are unique per owner, not globally (S25b D8).** The global constraint outlived that
    sweep and kept leaking the same fact in a quieter way: de-duplication counted every slug in
    the table, so naming a subject what a stranger had privately named theirs returned
    ``name_2`` — an answer about their library, obtainable by anyone willing to create, read and
    delete. Two constraints replace it. ``uq_subjects_owner_slug`` keeps one learner's own
    subjects distinct; the partial index below keeps *curated* subjects unique among themselves,
    which the first cannot do because Postgres does not compare NULL owners as equal. Slugs are
    display and ordering only — nothing is ever looked up by one — so scoping them costs nothing.
    """

    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("owner_learner_id", "slug", name="uq_subjects_owner_slug"),
        Index(
            "uq_subjects_curated_slug",
            "slug",
            unique=True,
            postgresql_where=text("owner_learner_id IS NULL"),
        ),
    )

    slug: Mapped[str] = mapped_column(index=True)
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)
    # CASCADE: a learner's own curriculum is theirs, and closing the account takes it. Curated
    # subjects carry NULL here and are untouched by any account deletion — see
    # `app.services.retention`, which states both halves.
    owner_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), default=None, index=True
    )
    # A latch (S25b D4): set the moment source material reaches this graph, never cleared, and
    # never taken from a request body. A subject carrying it cannot be published, because
    # publishing it would put one learner's private uploads in the shared library.
    private_source_derived: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # What this subject may draw on (S26, V05). Untagged sources — most uploads, since the tag
    # is optional — are admitted only when the owner switches this on; nothing unassigned is
    # added to a subject silently.
    include_untagged_sources: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Teach only from the subject's sources: where they do not cover something, say so rather
    # than answering from general knowledge. Read by every grounding path (app.rag.scope).
    sources_only: Mapped[bool] = mapped_column(server_default=false(), default=False)
    # Set on a *published copy*, naming the decision that created it. NULL on everything else,
    # including the author's original, which is not itself published.
    #
    # ``use_alter``: this and ``publications.source_subject_id`` point at each other, and without
    # it SQLAlchemy cannot order the two tables and drops every foreign key between them from
    # consideration — with a warning that says it may become an error. Adding this one by ALTER
    # after both exist breaks the cycle, which is also exactly what `0056` does by hand.
    publication_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "publications.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_subjects_publication_id",
        ),
        default=None,
    )
    # A newer approved version of the same author's subject. Unlists this one from the catalog
    # without removing it: learners already studying it keep their plan and their access.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), default=None
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    withdrawn_reason: Mapped[str | None] = mapped_column(Text, default=None)

    topics: Mapped[list["Topic"]] = relationship(
        back_populates="subject", cascade="all, delete-orphan"
    )


class Topic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A grouping of related KCs within a subject (e.g. "Integrals")."""

    __tablename__ = "topics"
    __table_args__ = (UniqueConstraint("subject_id", "slug"),)

    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str]
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)

    subject: Mapped["Subject"] = relationship(back_populates="topics")
    kcs: Mapped[list["KC"]] = relationship(back_populates="topic", cascade="all, delete-orphan")


class Concept(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One thing that can be known, independent of which subject teaches it (S24).

    A ``KC`` is a *presentation* of a concept inside one topic of one subject. Derivatives
    taught in Calculus and derivatives taught in Physics are two presentations, and before
    this they were two unrelated ids with nothing recording that they were about the same
    thing — so a learner who had studied one looked, to every query in the system, exactly
    like a learner who had studied neither.

    **What sharing a concept does and does not mean.** It means the two presentations are
    claimed to be about the same thing, on the evidence of their names. It does *not* mean
    mastery transfers: evidence stays per-presentation, because "Functions" in Calculus and
    "Functions" in a programming course share a name and almost nothing else, and a system
    that silently marked the second mastered because of the first would stop teaching
    something the learner had never seen — a failure invisible to every check downstream.
    The identity is reported so a person can act on it; it is not applied on their behalf.
    """

    __tablename__ = "concepts"

    # Canonical form of the name (see ``knowledge.concept_key``). Unique, because the whole
    # point is that two presentations reaching the same key reach the same row.
    key: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]


class KC(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A knowledge component — the smallest unit of "knowing" we track."""

    __tablename__ = "kcs"
    __table_args__ = (UniqueConstraint("topic_id", "slug"),)

    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str]
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)
    # Nullable, and SET NULL rather than CASCADE: a KC whose concept row went away is a KC
    # whose identity is unknown, which is the truth — deleting it with the concept would
    # destroy the curriculum over a bookkeeping row.
    concept_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("concepts.id", ondelete="SET NULL"), default=None, index=True
    )

    topic: Mapped["Topic"] = relationship(back_populates="kcs")


class KCEdge(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A directed prerequisite edge: mastering ``prereq_kc`` should precede ``kc``."""

    __tablename__ = "kc_edges"
    __table_args__ = (
        UniqueConstraint("prereq_kc_id", "kc_id"),
        CheckConstraint("prereq_kc_id <> kc_id", name="kc_edge_no_self_loop"),
    )

    prereq_kc_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kcs.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    weight: Mapped[float] = mapped_column(default=1.0)


class ConceptLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A claim that two presentations in different subjects are the same idea (S24).

    A shared concept key makes a pair a *candidate*; it is this row, endorsed and then accepted
    by a learner, that lets evidence carry across. Two agreements, deliberately: the endorser
    (an administrator for a curated pair, the LLM judge for a pair touching a learner's own
    material) says the two are the same idea, and the learner says it applies to them. Neither
    alone links anything — see ``concept_links.links_in_effect``.
    """

    __tablename__ = "concept_links"
    __table_args__ = (
        UniqueConstraint("kc_a_id", "kc_b_id"),
        CheckConstraint("kc_a_id < kc_b_id", name="ck_concept_links_ordered"),
    )

    kc_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    kc_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str]  # "curated" | "private"
    # The learner whose subjects a private pair spans; NULL for a curated pair.
    owner_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), default=None, index=True
    )
    verdict: Mapped[str | None] = mapped_column(default=None)  # "endorsed" | "rejected" | NULL
    endorsed_by: Mapped[str | None] = mapped_column(default=None)  # "admin" | "judge"
    decided_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), default=None
    )
    reason: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ConceptLinkDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One learner's answer to one endorsed link (S24): accepted, declined, or revoked."""

    __tablename__ = "concept_link_decisions"
    __table_args__ = (UniqueConstraint("learner_id", "link_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("concept_links.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str]  # "accepted" | "declined" | "revoked"
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
