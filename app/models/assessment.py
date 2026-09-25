"""Assessment items and grading rubrics (see docs/TECHNICAL_DESIGN §7.1, §7.6).

An ``Item`` is one assessment prompt tagged to one or more KCs (with apportioning
weights, via ``ItemKC``). Objective types carry an ``answer_key`` and grade
deterministically; open types (short/long) carry a ``rubric_id`` and are graded by the
SMART model later. A ``Rubric`` holds the per-KC criteria that rubric grading consumes.
"""

import uuid
from enum import StrEnum

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ItemType(StrEnum):
    """The kinds of assessment item. The first three grade deterministically."""

    MCQ = "mcq"
    CLOZE = "cloze"
    FILL_BLANK = "fill_blank"
    SHORT = "short"
    LONG = "long"
    FLASHCARD = "flashcard"


AUTO_GRADABLE: frozenset[ItemType] = frozenset({ItemType.MCQ, ItemType.CLOZE, ItemType.FILL_BLANK})
"""Types with a deterministic answer key; the rest need rubric (or self) grading."""

RUBRIC_GRADABLE: frozenset[ItemType] = frozenset({ItemType.SHORT, ItemType.LONG})
"""Open types the SMART model grades against a rubric (§7.6)."""

SELF_GRADABLE: frozenset[ItemType] = frozenset({ItemType.FLASHCARD})
"""Flashcards: the learner self-rates recall, which drives FSRS scheduling (slice 5)."""


class EvidenceKind(StrEnum):
    """Whether an attempt's score was *judged* or *reported* (S56).

    A rubric grade, an MCQ key and a cloze match are all judgements: something other than the
    learner decided how the answer went. A flashcard self-rating is the learner's own account
    of their recall. Both are real evidence and neither is noise — but they are evidence about
    different things, and the tracer must not fold them into the same number.

    Derived on the server from the grading path (see ``app.learning.grading``), never accepted
    from a request body. A bit the client can set is a bit the client can drop.
    """

    DEMONSTRATED = "demonstrated"
    SELF_REPORTED = "self_reported"


class ItemOrigin(StrEnum):
    """Authorship provenance; visibility separately defines sharing authority."""

    GENERATED = "generated"
    LEARNER = "learner"


class AssessmentVisibility(StrEnum):
    """Sharing authority is independent of authorship or generation provenance."""

    PRIVATE = "private"
    CURATED = "curated"


class Rubric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-KC grading criteria consumed by LLM rubric grading (§7.6)."""

    __tablename__ = "rubrics"

    visibility: Mapped[str] = mapped_column(default=AssessmentVisibility.PRIVATE, index=True)
    owner_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), default=None, index=True
    )

    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(default=None)
    criteria: Mapped[dict] = mapped_column(JSONB, default=dict)


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One assessment prompt. ``answer_key`` shape depends on ``item_type`` (see grading)."""

    __tablename__ = "items"

    visibility: Mapped[str] = mapped_column(default=AssessmentVisibility.PRIVATE, index=True)
    owner_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), default=None, index=True
    )

    item_type: Mapped[str] = mapped_column(index=True)
    stem: Mapped[str]
    answer_key: Mapped[dict | None] = mapped_column(JSONB, default=None)
    difficulty: Mapped[float] = mapped_column(default=0.0)
    rubric_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rubrics.id", ondelete="SET NULL"), default=None
    )
    # Provenance never grants publication authority.
    origin: Mapped[str] = mapped_column(index=True, default=ItemOrigin.GENERATED)
    author_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )

    kc_links: Mapped[list["ItemKC"]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )
    rubric: Mapped[Rubric | None] = relationship()


class ItemKC(UUIDPrimaryKeyMixin, Base):
    """Join row tagging an item to a KC with an apportioning ``weight`` (§7.4)."""

    __tablename__ = "item_kcs"
    __table_args__ = (UniqueConstraint("item_id", "kc_id"),)

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    weight: Mapped[float] = mapped_column(default=1.0)

    item: Mapped["Item"] = relationship(back_populates="kc_links")
