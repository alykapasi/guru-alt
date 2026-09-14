"""The knowledge graph: Subject → Topic → KC, plus prerequisite edges.

This is the backbone the whole learning engine hangs off (see docs/MASTERPLAN §4.1).
Content, mastery, and sequencing all reference KCs.
"""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Subject(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A top-level domain (e.g. "Calculus")."""

    __tablename__ = "subjects"

    slug: Mapped[str] = mapped_column(unique=True, index=True)
    name: Mapped[str]
    description: Mapped[str | None] = mapped_column(default=None)

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
