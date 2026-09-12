"""Conversations, messages, and the per-call LLM cost log."""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, func
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


class TurnStatus(StrEnum):
    """Where one conversation turn stands (S51).

    A turn used to exist only as a request in flight. The learner's message committed before
    generation started and the assistant's only after streaming finished, so an interruption
    between the two left a learner message with no reply and nothing saying why — on reload
    indistinguishable from a tutor that read the question and ignored it.
    """

    PENDING = "pending"  # generation is in flight
    COMPLETED = "completed"  # a terminal event was produced and its reply committed
    FAILED = "failed"  # the flow reported an error, or generation raised
    CANCELLED = "cancelled"  # the stream ended with no terminal event (client gone, restart)


class Turn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One attempt at answering one learner message, with a durable identity and status.

    Opened and committed *before* generation begins, so the record of an attempt cannot be
    lost by the thing it exists to survive. ``content`` is stored here rather than only as a
    ``Message`` because a turn that failed before its flow persisted anything still has to be
    retryable, and the retry must not append the learner's message a second time.

    **Partial-output policy:** a reply interrupted mid-generation is discarded, never written
    to ``messages``. A truncated explanation can stop mid-derivation and still read as
    finished, and carrying one forward as history presents it to the model as a complete
    assistant turn. The interruption is recorded instead, and the retry regenerates from the
    same learner message. What this costs is honest and recorded in the tracker: tokens
    already billed for the discarded text.
    """

    __tablename__ = "turns"
    # NULLs compare distinct in Postgres, so turns sent without a key (an older client, or a
    # server-initiated turn) are unconstrained while a client-supplied key is exactly once.
    __table_args__ = (UniqueConstraint("conversation_id", "client_turn_id"),)

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    # The client's idempotency key for this turn. A retry carries the same one, which is what
    # makes "retry" mean *this* turn again rather than a second turn saying the same thing.
    client_turn_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    flow: Mapped[str] = mapped_column()  # TurnFlow: refinement | tutor | agentic | workflow
    status: Mapped[str] = mapped_column(index=True, default=TurnStatus.PENDING)
    # What the learner said. Duplicated from the Message deliberately — see the class docstring.
    content: Mapped[str] = mapped_column(Text)
    # The learner message this turn answers, written in the same transaction that opens the
    # turn. Recorded rather than inferred: a retry has to know whether the transcript already
    # holds its message, and "the flow usually writes it first" is not a guarantee.
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    # The reply this turn committed, when it got that far. SET NULL: the audit of the attempt
    # outlives the message, as with LLMCall.
    assistant_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), default=None
    )
    error: Mapped[str | None] = mapped_column(Text, default=None)


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
    # Tutor replies given while ``active_item_id`` was still unanswered (S15). A learner who
    # asks "what does that even mean?" and attempts only after the explanation has not made an
    # independent demonstration, and this is how much help the eventual attempt carries into
    # ``assistance.evidence_credit`` — the same discount a guided-practice hint gets. Counted
    # here rather than derived from message timestamps because ``created_at`` is transaction
    # time: the reply that poses a check and the row that records the check are written in
    # different transactions, and ordering the two by clock is exactly the kind of inference
    # this column exists to avoid.
    active_item_scaffolds: Mapped[int] = mapped_column(server_default="0", default=0)
    # The newest message memory extraction has already read. Extraction used to take the last
    # N messages regardless, so a conversation that grew by more than N between write-backs
    # had the middle silently skipped — and one that grew by nothing paid a model call to
    # re-read what it had already extracted (S43). Same cursor idea as Note's watermarks (S38).
    memory_watermark: Mapped[datetime | None] = mapped_column(default=None)

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


class OnboardingSession(Base, TimestampMixin):
    """Who owns a goal-refinement negotiation (S33, made durable by S17).

    The id used to be invented by the client and used verbatim as the checkpointer's thread
    key, so anyone who guessed another learner's id could resume their onboarding. It is issued
    by the server now and recorded here against the learner who asked for it.

    This table exists because the checkpointer became durable. While the graph's state lived in
    one process's heap, an in-process registry was exactly as strong as the thing it guarded and
    a durable one would have promised more than the state behind it could keep. Now the state
    outlives the process, so the record of who owns it has to as well — otherwise a restart
    leaves a resumable negotiation that nothing can prove the ownership of, and the only safe
    answer becomes "no", which discards it just as surely as losing it did.
    """

    __tablename__ = "onboarding_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(64))
