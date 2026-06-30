"""Conversations, messages, and the per-call LLM cost log."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A chat thread between a learner and a guru."""

    __tablename__ = "conversations"

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(default=None)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class LLMCall(UUIDPrimaryKeyMixin, Base):
    """Audit log of every LLM call: token usage + estimated cost, tagged by role/model."""

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
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
