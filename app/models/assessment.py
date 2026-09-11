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


class ItemOrigin(StrEnum):
    """Who authored an item — which decides who may be assessed with it (S33).

    ``items`` is a global table, so any authenticated learner writing one used to add a
    question *and its answer key* to a bank other learners are then examined against. Being
    signed in is not authority to author someone else's assessment.

    Stored as its own column rather than inferred from ``author_learner_id is None``: the FK
    is ``ON DELETE SET NULL``, so deleting a learner would otherwise promote every private
    item they wrote into the shared bank.
    """

    GENERATED = "generated"  # produced by the platform's own generators; shared
    LEARNER = "learner"  # authored through POST /items; private to its author


class Rubric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-KC grading criteria consumed by LLM rubric grading (§7.6)."""

    __tablename__ = "rubrics"

    kc_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(default=None)
    criteria: Mapped[dict] = mapped_column(JSONB, default=dict)


class Item(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One assessment prompt. ``answer_key`` shape depends on ``item_type`` (see grading)."""

    __tablename__ = "items"

    item_type: Mapped[str] = mapped_column(index=True)
    stem: Mapped[str]
    answer_key: Mapped[dict | None] = mapped_column(JSONB, default=None)
    difficulty: Mapped[float] = mapped_column(default=0.0)
    rubric_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rubrics.id", ondelete="SET NULL"), default=None
    )
    # Authority to assess with this item — see ItemOrigin. A learner-authored item is theirs
    # alone; nothing here promotes one to the shared bank, because nothing in the system can
    # yet establish who is entitled to (S25/Phase 10 auth own that).
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
