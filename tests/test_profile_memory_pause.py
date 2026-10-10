"""Pausing memory also stops the profile reading what the learner types (O07).

"Remember things from my conversations" off: the two dimensions estimated from message text
keep their last value and are marked paused; everything estimated from answers goes on. After
a resume, what was typed during the pause is never read.
"""

import json
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.services import memory as memory_svc
from app.services import profile as svc
from app.services import refresh_schedule
from tests.test_profile import _learner, _rich_learner, _sequenced_client

API = "/api/v1"
MESSAGE_KEYS = {"interests", "message_writing_complexity"}
ERROR_TYPE = json.dumps({"classifications": [{"item": 1, "type": "conceptual"}]})
GOALS = json.dumps({"orientation": "mastery", "confidence": 0.9})


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _calls(session: AsyncSession, learner: Learner) -> int:
    rows = await session.scalars(select(LLMCall).where(LLMCall.learner_id == learner.id))
    return len(rows.all())


async def _say(session: AsyncSession, learner: Learner, content: str, at: datetime) -> None:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.learner_id == learner.id)
    )
    if conversation is None:
        conversation = Conversation(learner_id=learner.id)
        session.add(conversation)
        await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role="user", content=content, created_at=at)
    )
    await session.flush()


async def test_a_paused_learners_profile_does_not_read_their_messages(
    db_session: AsyncSession,
) -> None:
    learner = await _rich_learner(db_session)
    await memory_svc.set_remember(db_session, learner.id, False)

    dims = await svc.refresh_profile(db_session, learner.id, _sequenced_client([ERROR_TYPE, GOALS]))

    keys = {d.key for d in dims}
    assert keys.isdisjoint(MESSAGE_KEYS)
    assert "pace" in keys
    assert await _calls(db_session, learner) == 2  # error_type, goal_orientation


async def test_pausing_keeps_what_the_messages_already_showed(db_session: AsyncSession) -> None:
    learner = await _rich_learner(db_session)
    interests = json.dumps({"interests": ["basketball", "cooking"]})
    await svc.refresh_profile(
        db_session, learner.id, _sequenced_client([ERROR_TYPE, GOALS, interests])
    )
    await memory_svc.set_remember(db_session, learner.id, False)
    await _say(db_session, learner, "I have taken up chess and knitting lately.", _now())

    dims = await svc.refresh_profile(
        db_session, learner.id, _sequenced_client([ERROR_TYPE, GOALS]), force=True
    )

    by_key = {d.key: d.value for d in dims}
    assert by_key["interests"] == ["basketball", "cooking"]
    assert await _calls(db_session, learner) == 3 + 2  # no interests call while paused


async def test_after_resuming_what_was_typed_while_paused_is_never_read(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    await memory_svc.set_remember(db_session, learner.id, False)
    await _say(db_session, learner, "Said while paused.", _now() - timedelta(minutes=5))
    await memory_svc.set_remember(db_session, learner.id, True)
    await _say(db_session, learner, "Said after resuming.", _now() + timedelta(minutes=5))

    assert [m.content for m in await svc._load_own_messages(db_session, learner.id)] == [
        "Said after resuming."
    ]


async def test_a_message_alone_is_not_new_evidence_while_paused(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await memory_svc.set_remember(db_session, learner.id, False)
    await _say(db_session, learner, "Just chatting.", _now() - timedelta(hours=2))

    assert await svc.latest_evidence_at(db_session, learner.id) is None
    due = await refresh_schedule.due_learners(
        db_session, quiet_before=_now(), retry_before=None, limit=None
    )
    assert learner.id not in due


async def test_the_profile_marks_message_dimensions_paused(
    api_client: AsyncClient, api_learner: Learner, db_session: AsyncSession
) -> None:
    long_message = " ".join(["I like basketball and cooking pasta after school."] * 5)
    await _say(db_session, api_learner, long_message, _now())
    interests = json.dumps({"interests": ["basketball", "cooking"]})
    app.dependency_overrides[get_llm_client] = lambda: _sequenced_client([interests])
    try:
        r = await api_client.post(f"{API}/profile/refresh")
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.status_code == 200
    await api_client.put(f"{API}/me/memory-setting", json={"remember": False})

    dims = (await api_client.get(f"{API}/profile")).json()["dimensions"]

    paused = {d["key"] for d in dims if d["paused"]}
    assert paused == {d["key"] for d in dims} & MESSAGE_KEYS
    assert paused  # the message dimension exists and is the one marked
