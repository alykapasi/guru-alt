"""Who owns an onboarding negotiation.

The goal-refinement gate is keyed by a session id that the *client* invented and the server
used verbatim as the checkpointer's thread key, with no learner attached. Anyone who knew or
guessed another learner's id could resume their onboarding — read the proposal, feed it
different feedback, or commit a goal on their behalf. Ordinary conversations already check
ownership; this path did not.

Two independent things fix that, and both are here because either alone leaves a gap:

- The id is **issued by the server** and recorded against the learner who asked for it, so a
  resume can be checked rather than assumed.
- The checkpointer's thread key is **derived from the learner and the id**, so even an
  unrecorded id cannot address another learner's state. A leaked id is then worth nothing on
  its own.

Registry state is in-process, exactly as strong as the guarantee the graph already relies on:
``app/agent/refinement.py`` compiles with ``InMemorySaver``, so a paused negotiation can only
be resumed by the process that paused it. A durable registry in front of a volatile
checkpointer would only promise more than the state behind it can keep — the same reasoning
as ``app/services/turn_lock.py``. When those checkpointers become durable, both move together.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

PURPOSE_GOAL_REFINEMENT = "goal_refinement"


@dataclass(frozen=True)
class OnboardingSession:
    session_id: str
    learner_id: uuid.UUID
    purpose: str
    created_at: datetime


class NotYourSession(LookupError):
    """The caller does not own this onboarding session, or it no longer exists."""


_sessions: dict[str, OnboardingSession] = {}


def issue(learner_id: uuid.UUID, *, purpose: str = PURPOSE_GOAL_REFINEMENT) -> OnboardingSession:
    """Mint a server-owned session id for ``learner_id``."""
    record = OnboardingSession(
        session_id=uuid.uuid4().hex,
        learner_id=learner_id,
        purpose=purpose,
        created_at=datetime.now(UTC),
    )
    _sessions[record.session_id] = record
    return record


def require(
    session_id: str, learner_id: uuid.UUID, *, purpose: str = PURPOSE_GOAL_REFINEMENT
) -> OnboardingSession:
    """The session, if this learner owns it and it is for this purpose.

    Raises :class:`NotYourSession` otherwise — the same answer for "someone else's" and "never
    existed", so the endpoint cannot be used to discover which ids are real.
    """
    record = _sessions.get(session_id)
    if record is None or record.learner_id != learner_id or record.purpose != purpose:
        raise NotYourSession(session_id)
    return record


def thread_key(session_id: str, learner_id: uuid.UUID) -> str:
    """The checkpointer thread key for this learner's session.

    Namespacing by learner is the part that holds even if the registry is empty — after a
    restart, say — because two learners presenting the same id still address different threads.
    """
    return f"{learner_id}:{session_id}"


def forget(session_id: str) -> None:
    """Drop a session (its negotiation is over). Safe for an id that was never issued."""
    _sessions.pop(session_id, None)
