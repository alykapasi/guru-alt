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
