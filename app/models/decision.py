"""One Jev answer, recorded beside what today's model decided (S82)."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import UUIDPrimaryKeyMixin


class DecisionCall(UUIDPrimaryKeyMixin, Base):
    """A question Jev was asked in shadow or live mode, its answer, and the baseline.

    Written on its own transaction like ``LLMCall``, so a turn that rolls back still leaves the
    request recorded. One row per *question*; two questions sharing one request share a
    ``request_id``, which is what keeps the report from counting that request's cost twice.

    No learner text is stored here. The report reaches examples through ``attempt_id`` and the
    conversation's own messages.
    """

    __tablename__ = "decision_calls"

    request_id: Mapped[uuid.UUID] = mapped_column(index=True)
    learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True, default=None
    )
    item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL"), index=True, default=None
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(index=True, default=None)
    question: Mapped[str] = mapped_column(index=True)  # intent | fully_correct
    mode: Mapped[str] = mapped_column()  # shadow | live
    provider: Mapped[str] = mapped_column()
    model: Mapped[str] = mapped_column()
    # ok | timeout | rate_limited | auth | server | invalid
    status: Mapped[str] = mapped_column()
    answer: Mapped[str | None] = mapped_column(default=None)  # the label; NULL for yes/no
    # The label distribution, or {"yes": p} for a yes/no question. NULL on failure.
    probabilities: Mapped[dict | None] = mapped_column(JSONB, default=None)
    confidence: Mapped[float | None] = mapped_column(default=None)  # choice questions only
    baseline_intent: Mapped[str | None] = mapped_column(default=None)  # the FAST gate's intent
    baseline_score: Mapped[float | None] = mapped_column(default=None)  # the SMART grade
    # True when Jev's answer decided the outcome (live, at or above threshold).
    used: Mapped[bool] = mapped_column(default=False)
    # NULL = not known when the row was written (the request failed, or was still running).
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    cost_usd: Mapped[float | None] = mapped_column(default=None)
    latency_ms: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
