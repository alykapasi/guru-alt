"""A turn has a durable identity and a status that survives the thing that interrupted it (S51).

Before this a turn existed only as a request in flight. The learner's message committed before
generation, the assistant's only after streaming finished, and nothing recorded the gap — so a
disconnect, a restart, or a provider failure left a question with no answer, no status, and no
way to try again that did not append the question a second time.
"""

import json
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_llm_client
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, Message, Turn, TurnStatus
from app.services import turn as turn_svc
from app.services import turn_lock
from app.services.turn_common import TurnEvent

API = "/api/v1"
REPLY = "Let us explore this together."


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _parse_sse(text: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :]) for line in text.splitlines() if line.startswith("data: ")
    ]


async def _goal_conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    """A conversation with a committed goal, so turns take the plain tutor flow."""
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc help"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return conversation_id


async def _turns(db_session: AsyncSession, conversation_id: str) -> list[Turn]:
    result = await db_session.scalars(
        select(Turn)
        .where(Turn.conversation_id == uuid.UUID(conversation_id))
        .order_by(Turn.created_at, Turn.id)
    )
    return list(result.all())


async def _messages(db_session: AsyncSession, conversation_id: str) -> list[Message]:
    """This conversation's messages, in no particular order.

    Deliberately unordered: the whole suite runs inside one transaction, so ``created_at``
    (``now()``, the transaction's start time) is identical on every row and any tie-break
    falls to a random UUID. These tests assert *which* messages exist and what the turn
    points at, which is what S51 is actually about — transcript order is test_chat.py's.
    """
    result = await db_session.scalars(
        select(Message).where(Message.conversation_id == uuid.UUID(conversation_id))
    )
    return list(result.all())


def _roles(messages: list[Message]) -> list[str]:
    return sorted(m.role for m in messages)


# --- the record itself ---------------------------------------------------------------------


async def test_a_finished_turn_is_recorded_with_the_reply_it_produced(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _goal_conversation(api_client, db_session)

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "What is a limit?"}
    )
    assert r.status_code == 200

    turns = await _turns(db_session, conversation_id)
    assert len(turns) == 1
    assert turns[0].status == TurnStatus.COMPLETED
    assert turns[0].flow == "tutor"
    assert turns[0].content == "What is a limit?"
    assert turns[0].error is None
    # Both ends of the turn are on the record, not just the half that committed first.
    messages = {m.role: m for m in await _messages(db_session, conversation_id)}
    assert turns[0].user_message_id == messages["user"].id
    assert turns[0].assistant_message_id == messages["assistant"].id


async def test_a_failed_turn_is_recorded_as_failed_with_its_reason(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    """The learner's message is committed before generation, so a failure here used to leave a
    question with nothing after it — indistinguishable from a tutor that ignored it."""
    conversation_id = await _goal_conversation(api_client, db_session)

    async def _boom(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="error", detail="generation failed")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", _boom)
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "What is a limit?"}
    )
    assert r.status_code == 200
    assert any(e["type"] == "error" for e in _parse_sse(r.text))

    turns = await _turns(db_session, conversation_id)
    assert turns[0].status == TurnStatus.FAILED
    assert turns[0].error == "generation failed"


async def test_a_stream_that_ends_with_no_terminal_event_is_not_a_completed_turn(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    """EOF is not success. A flow that streams tokens and then simply stops produced a reply
    the client rendered as finished; the turn now says it never finished."""
    conversation_id = await _goal_conversation(api_client, db_session)

    async def _truncated(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="half an ans")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", _truncated)
    await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "What is a limit?"}
    )

    turns = await _turns(db_session, conversation_id)
    assert turns[0].status == TurnStatus.CANCELLED
    assert turns[0].error == "the stream ended without a terminal event"
    # Partial-output policy: the half-written reply is not in the transcript.
    assert _roles(await _messages(db_session, conversation_id)) == ["user"]


# --- interruptions become visible ----------------------------------------------------------


async def test_a_turn_abandoned_by_a_disconnect_is_reported_as_cancelled(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """A client that vanishes mid-stream never reaches the closing write, so the row stays
    ``pending``; the next read reaps it rather than reporting work still in progress."""
    conversation_id = await _goal_conversation(api_client, db_session)
    await turn_svc.open_turn(
        db_session,
        conversation_id=uuid.UUID(conversation_id),
        flow="tutor",
        content="What is a limit?",
    )

    r = await api_client.get(f"{API}/conversations/{conversation_id}/turns")

    assert r.status_code == 200
    assert [t["status"] for t in r.json()] == ["cancelled"]
    assert r.json()[0]["error"] == "the turn ended without completing"


async def test_a_live_turn_is_not_reaped_out_from_under_itself(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, engine: AsyncEngine
) -> None:
    """The claim is the liveness signal: while it is held, ``pending`` means running."""
    conversation_id = await _goal_conversation(api_client, db_session)
    await turn_svc.open_turn(
        db_session,
        conversation_id=uuid.UUID(conversation_id),
        flow="tutor",
        content="What is a limit?",
    )
    claim = await turn_lock.claim(engine, uuid.UUID(conversation_id))
    assert claim is not None
    try:
        r = await api_client.get(f"{API}/conversations/{conversation_id}/turns")
    finally:
        await claim.release()

    assert [t["status"] for t in r.json()] == ["pending"]


# --- retry ---------------------------------------------------------------------------------


async def test_retrying_a_failed_turn_does_not_ask_the_question_twice(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    conversation_id = await _goal_conversation(api_client, db_session)
    client_turn_id = str(uuid.uuid4())

    async def _boom(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="error", detail="generation failed")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", _boom)
    body = {"content": "What is a limit?", "client_turn_id": client_turn_id}
    await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)
    monkeypatch.undo()

    r = await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)

    assert r.status_code == 200
    turns = await _turns(db_session, conversation_id)
    assert len(turns) == 1  # the same turn, tried again
    assert turns[0].status == TurnStatus.COMPLETED
    # One question, one answer — not the question twice.
    assert _roles(await _messages(db_session, conversation_id)) == ["assistant", "user"]


async def test_a_retry_does_not_show_the_model_the_question_twice(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    """The retry's message is already in the transcript, and every flow appends the turn's
    content itself — so the history handed to the flow must not contain it as well."""
    conversation_id = await _goal_conversation(api_client, db_session)
    client_turn_id = str(uuid.uuid4())
    seen: list[list[str]] = []

    async def _failing(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="error", detail="generation failed")

    real = __import__("app.services.chat", fromlist=["run_tutor_turn"]).run_tutor_turn

    def _recording(*args, **kwargs):
        seen.append([m.content for m in kwargs["history"]])
        return real(*args, **kwargs)

    monkeypatch.setattr("app.services.chat.run_tutor_turn", _failing)
    body = {"content": "What is a limit?", "client_turn_id": client_turn_id}
    await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)
    monkeypatch.undo()

    monkeypatch.setattr("app.services.chat.run_tutor_turn", _recording)
    await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)

    assert seen == [[]]  # the retry's own message stripped, nothing else to carry


async def test_retrying_a_completed_turn_is_refused_rather_than_answered_twice(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _goal_conversation(api_client, db_session)
    body = {"content": "What is a limit?", "client_turn_id": str(uuid.uuid4())}
    assert (
        await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)
    ).status_code == 200

    r = await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)

    assert r.status_code == 409
    assert len(await _turns(db_session, conversation_id)) == 1
    assert len(await _messages(db_session, conversation_id)) == 2


async def test_a_retry_of_the_very_first_turn_still_reaches_the_refinement_gate(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    """The gate is chosen for a conversation with no history. The retry's own message is in
    the transcript by then, so a naive read of history would route it to plain chat instead."""
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc help"})
    conversation_id = r.json()["id"]
    client_turn_id = str(uuid.uuid4())

    async def _boom(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="error", detail="generation failed")

    monkeypatch.setattr("app.services.refinement.run_refinement_turn", _boom)
    body = {"content": "I want to learn calculus", "client_turn_id": client_turn_id}
    await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)
    assert (await _turns(db_session, conversation_id))[0].flow == "refinement"

    await api_client.post(f"{API}/conversations/{conversation_id}/messages", json=body)

    turns = await _turns(db_session, conversation_id)
    assert len(turns) == 1
    assert turns[0].flow == "refinement"


async def test_turns_without_a_client_key_stay_independent(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """A client that sends no key gets exactly the old behaviour: every send is its own turn."""
    conversation_id = await _goal_conversation(api_client, db_session)
    for _ in range(2):
        await api_client.post(
            f"{API}/conversations/{conversation_id}/messages", json={"content": "again"}
        )

    turns = await _turns(db_session, conversation_id)
    assert len(turns) == 2
    assert {t.status for t in turns} == {TurnStatus.COMPLETED}
    assert len(await _messages(db_session, conversation_id)) == 4
