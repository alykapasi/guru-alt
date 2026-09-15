"""Who is using this deployment, and what their use costs (P10).

``llm_calls`` has carried ``learner_id`` since Phase 1 and only two things ever read it: the
per-learner spend cap (S47) and account deletion. So the deployment could answer "what did we
spend" and "is this one learner over their cap", and could not answer the question in between —
which learners are actually here, and which of them account for the bill. During an alpha that
is the question, because the answer is a handful of rows and each one is a person somebody can
go and talk to.

A learner with no calls at all is included deliberately. They are the most interesting row on
the page: somebody registered and never came back, and a query that inner-joined the call log
would report the alpha as healthier than it is by omitting exactly those people.

**The list is ordered by cost and it is capped, so the total is reported beside it.** Those two
facts together are a way to lie: the quiet learners sort last, so a cap silently removes the
very rows the paragraph above argues for, and a page showing a hundred rows out of two hundred
looks exactly like a page showing all of them. A browser journey caught this against a database
with 188 accounts — the freshly registered one was not on the page and nothing said so. So the
count is part of the answer rather than something a reader is left to infer from the length.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import LLMCall
from app.models.learner import Learner


class LearnerUsage(BaseModel):
    """One learner, and what they did inside the window."""

    id: uuid.UUID
    handle: str
    display_name: str | None
    email: str | None
    is_admin: bool
    created_at: datetime
    calls: int
    # A floor whenever ``unpriced_calls`` is non-zero, on exactly the reasoning in
    # ``app.services.spend``: NULL is "this model has no known price", not "free".
    cost_usd: float
    unpriced_calls: int
    # None means no model call inside the window — which is not the same as never, and the
    # window is stated beside it so the distinction is readable.
    last_call_at: datetime | None


class LearnerRoster(BaseModel):
    """The learners listed, and how many there were to list.

    ``total`` is every account, not every returned row. A truncated page and a complete one
    look identical without it, which is the same failure as a percentile printed without the
    calls behind it.
    """

    total: int
    window_hours: int
    learners: list[LearnerUsage]


async def learner_usage(session: AsyncSession, *, hours: int, limit: int = 100) -> LearnerRoster:
    """Every learner, most expensive first, with their activity inside the window."""
    # Naive UTC: `llm_calls.created_at` is TIMESTAMP WITHOUT TIME ZONE, and comparing it to an
    # aware value would raise rather than quietly compare wrong.
    since = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)

    # The window lives in the JOIN condition rather than a WHERE clause. In a WHERE it would
    # discard the learners whose rows are all outside it — which is to say the inactive ones,
    # who are the ones worth seeing.
    cost = func.coalesce(func.sum(LLMCall.cost_usd), 0.0)
    rows = await session.execute(
        select(
            Learner,
            func.count(LLMCall.id),
            cost,
            func.count(LLMCall.id).filter(LLMCall.cost_usd.is_(None)),
            func.max(LLMCall.created_at),
        )
        .outerjoin(LLMCall, (LLMCall.learner_id == Learner.id) & (LLMCall.created_at >= since))
        .group_by(Learner.id)
        # `created_at` is a transaction clock, so rows written by one transaction share it and
        # the order would fall to a random UUID (the tie behind S56, S14 and S62). `id` last is
        # arbitrary but stable, which is what a paged list needs.
        .order_by(cost.desc(), func.count(LLMCall.id).desc(), Learner.created_at.desc(), Learner.id)
        .limit(limit)
    )
    total = await session.scalar(select(func.count()).select_from(Learner)) or 0
    listed = [
        LearnerUsage(
            id=learner.id,
            handle=learner.handle,
            display_name=learner.display_name,
            email=learner.email,
            is_admin=learner.is_admin,
            created_at=learner.created_at,
            calls=calls,
            cost_usd=float(spent),
            unpriced_calls=unpriced,
            last_call_at=last_call_at,
        )
        for learner, calls, spent, unpriced, last_call_at in rows
    ]
    return LearnerRoster(total=total, window_hours=hours, learners=listed)
