"""Conversations, messages, and the per-call LLM cost log."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConversationPhase(StrEnum):
    """What the conversation is waiting for — recorded, not inferred (S52).

    The frontend used to reconstruct this from "no goal committed and the last message is from
    the assistant", which is true of a goal proposal and equally true of an agentic answer
    given before any goal was committed — so a tool-using reply was offered to the learner
    with accept/refine buttons. The backend always knew which flow ran; it just never said.
    """

    CHATTING = "chatting"  # nothing pending: an ordinary reply, whatever produced it
    GOAL_PROPOSED = "goal_proposed"  # the refinement gate proposed a goal; accept or refine
    AWAITING_ANSWER = "awaiting_answer"  # a practice item is in play and expects an answer


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A chat thread between a learner and a guru."""

    __tablename__ = "conversations"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(default=None)
    # "chat" (the refinement-gate + tutor-turn flow) or "session" (a guided-practice workflow
    # run, created by the Lessons page). Set once at creation, never changed — distinguishes
    # the two so the frontend can route a conversation to the right page (a session's paused
    # workflow reply must never be mistaken for the chat gate's goal-negotiation prompt).
    kind: Mapped[str] = mapped_column(default="chat")
    # Set once the refinement gate commits; grounds subsequent tutor turns. NULL until then.
    goal: Mapped[str | None] = mapped_column(Text, default=None)
    # Set once at creation, never changed. Scopes tutor-turn plan grounding + the session
    # runner's item to an exact (learner, subject) plan instead of a cross-subject heuristic.
    # Also the hard content boundary for retrieval (Phase 7): NULL = "general", no library
    # grounding at all. Memory retrieval stays learner-global regardless (see MASTERPLAN §5).
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), index=True, default=None
    )
    # What this conversation is waiting for, written by the turn that produced the last
    # assistant message — so a reload reads the same answer the live stream gave.
    phase: Mapped[str] = mapped_column(default=ConversationPhase.CHATTING)
    # The practice item in play while ``phase`` is AWAITING_ANSWER. Without it, a refresh
    # loses which question the learner was on: the item only ever existed in an SSE event.
    active_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), default=None
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    conversation_sources: Mapped[list["ConversationSource"]] = relationship(
        cascade="all, delete-orphan"
    )

    @property
    def source_ids(self) -> list[uuid.UUID]:
        """Sources this conversation is narrowed to; empty = every source under its subject."""
        return [cs.source_id for cs in self.conversation_sources]


class ConversationSource(UUIDPrimaryKeyMixin, Base):
    """Narrows a conversation's retrieval scope to specific sources within its subject.

    No row for a conversation = "all sources under its subject" (the common case) — this table
    only exists to record a learner's explicit narrowing at creation time.
    """

    __tablename__ = "conversation_sources"
    __table_args__ = (UniqueConstraint("conversation_id", "source_id"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), index=True
    )


class Message(UUIDPrimaryKeyMixin, Base):
    """One turn in a conversation. Append-only (no ``updated_at``)."""

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(index=True)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(default=None)  # set on assistant turns
    # Each entry: {"marker": int, "chunk_id": str, "source_id": str} — the literal [N] marker
    # as it appears in `content`, mapped to the chunk it cites. Mirrors ContentBlock.citations'
    # shape (thin references, not the chunk text inline — see app/services/turn_common.py).
    citations: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class LLMCall(UUIDPrimaryKeyMixin, Base):
    """Audit log of every LLM call: token usage + estimated cost, tagged by role/model.

    Written on its own transaction (``app.services.llm_log``), so a business transaction
    that rolls back after a paid call still leaves the call recorded.
    """

    __tablename__ = "llm_calls"

    learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True, default=None
    )
    role: Mapped[str] = mapped_column(index=True)  # fast | smart | genius | embed
    provider: Mapped[str] = mapped_column()
    model: Mapped[str] = mapped_column(index=True)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    # NULL = this model has no known price. Distinct from 0.0, which means "ran locally,
    # cost nothing" — collapsing the two reported unpriced spend as zero.
    cost_usd: Mapped[float | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
