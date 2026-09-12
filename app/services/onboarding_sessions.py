"""Who owns an onboarding negotiation (S33), durably (S17).

The goal-refinement gate was keyed by a session id that the *client* invented and the server
used verbatim as the checkpointer's thread key, with no learner attached. Anyone who knew or
guessed another learner's id could resume their onboarding — read the proposal, feed it
different feedback, or commit a goal on their behalf. Ordinary conversations already check
ownership; this path did not.

Two independent things fix that, and both are here because either alone leaves a gap:

- The id is **issued by the server** and recorded against the learner who asked for it, so a
  resume can be checked rather than assumed.
- The checkpointer's thread key is **derived from the learner and the id**, so even an
  unrecorded id cannot address another learner's state. A leaked id is then worth nothing on
  its own, and this half holds even when the record is missing.

The record used to be a dict, deliberately exactly as strong as the ``InMemorySaver`` behind it:
a durable registry in front of volatile state would only have promised more than the state could
keep. The checkpointer is durable now, so this is a table. The two move together in the other
direction too — a durable negotiation whose ownership record died with the process is one that
cannot be safely resumed, which discards it just as surely as losing the state did.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import OnboardingSession

PURPOSE_GOAL_REFINEMENT = "goal_refinement"


class NotYourSession(LookupError):
    """The caller does not own this onboarding session, or it no longer exists."""


async def issue(
    session: AsyncSession, learner_id: uuid.UUID, *, purpose: str = PURPOSE_GOAL_REFINEMENT
) -> OnboardingSession:
    """Mint a server-owned session id for ``learner_id``."""
    record = OnboardingSession(session_id=uuid.uuid4().hex, learner_id=learner_id, purpose=purpose)
    session.add(record)
    await session.commit()
    return record


async def require(
    session: AsyncSession,
    session_id: str,
    learner_id: uuid.UUID,
    *,
    purpose: str = PURPOSE_GOAL_REFINEMENT,
) -> OnboardingSession:
    """The session, if this learner owns it and it is for this purpose.

    Raises :class:`NotYourSession` otherwise — the same answer for "someone else's" and "never
    existed", so the endpoint cannot be used to discover which ids are real. The query asks for
    all three at once rather than fetching by id and comparing, so a row that exists but belongs
    to someone else never enters the process at all.
    """
    record = await session.scalar(
        select(OnboardingSession).where(
            OnboardingSession.session_id == session_id,
            OnboardingSession.learner_id == learner_id,
            OnboardingSession.purpose == purpose,
        )
    )
    if record is None:
        raise NotYourSession(session_id)
    return record


def thread_key(session_id: str, learner_id: uuid.UUID) -> str:
    """The checkpointer thread key for this learner's session.

    Namespacing by learner is the part that holds even if the record is gone — after a restore
    from a backup taken before the id was issued, say — because two learners presenting the same
    id still address different threads.
    """
    return f"{learner_id}:{session_id}"


async def forget(session: AsyncSession, session_id: str) -> None:
    """Drop a session (its negotiation is over). Safe for an id that was never issued."""
    await session.execute(
        delete(OnboardingSession).where(OnboardingSession.session_id == session_id)
    )
    await session.commit()
