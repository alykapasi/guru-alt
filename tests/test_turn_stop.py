"""A learner can stop a reply and keep what they saw; a turn cannot run forever (S47)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_llm_client
from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, Message, Turn, TurnStatus
from app.models.learner import Learner
from app.services import turn as turn_svc
from app.services import turn_control, turn_lock
from app.services.turn_common import TurnEvent
from tests.conftest import sign_in

API = "/api/v1"


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("A whole reply.")
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(body: str) -> list[dict]:
    return [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]


async def _conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc"})
    cid = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(cid))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return cid


async def _turn(db_session: AsyncSession, cid: str) -> Turn:
    return (
        await db_session.scalars(select(Turn).where(Turn.conversation_id == uuid.UUID(cid)))
    ).one()


async def _assistant(db_session: AsyncSession, cid: str) -> list[Message]:
    rows = await db_session.scalars(
        select(Message).where(
            Message.conversation_id == uuid.UUID(cid), Message.role == "assistant"
        )
    )
    return list(rows.all())


def _stopping_flow():
    """Streams two tokens, then the learner presses Stop, then it would go on for ever."""

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Half ")
        yield TurnEvent(type="token", text="an answer")
        for control in list(turn_control._local.values()):
            control.stop()
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    return flow


async def test_the_stream_names_its_turn_first(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    events = _sse(r.text)
    assert events[0] == {"type": "turn", "turn_id": str((await _turn(db_session, cid)).id)}


async def test_a_stopped_reply_keeps_its_text(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _stopping_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    events = _sse(r.text)
    [reply] = await _assistant(db_session, cid)
    assert events[-1] == {"type": "stopped", "message_id": str(reply.id)}
    assert (reply.content, reply.interrupted) == ("Half an answer", "stopped")
    turn = await _turn(db_session, cid)
    assert (turn.status, turn.assistant_message_id, turn.error) == (
        TurnStatus.STOPPED,
        reply.id,
        None,
    )


async def test_a_stop_before_any_text_saves_no_reply(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        for control in list(turn_control._local.values()):
            control.stop()
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", flow)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {"type": "stopped", "message_id": None}
    assert await _assistant(db_session, cid) == []
    assert (await _turn(db_session, cid)).status == TurnStatus.STOPPED


def _short_deadline(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"turn_deadline_seconds": 0.1})
    monkeypatch.setattr("app.api.v1.chat.get_settings", lambda: settings)


def _slow_flow():
    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Slow start")
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    return flow


async def test_a_turn_past_its_deadline_is_cut_off_keeping_its_text(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    _short_deadline(monkeypatch)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _slow_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {
        "type": "error",
        "detail": "This reply took too long and was cut off.",
        "code": "deadline",
    }
    [reply] = await _assistant(db_session, cid)
    assert (reply.content, reply.interrupted) == ("Slow start", "timed_out")
    turn = await _turn(db_session, cid)
    assert (turn.status, turn.error) == (TurnStatus.FAILED, "deadline")


async def test_a_timed_out_turn_can_be_retried(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    key = str(uuid.uuid4())
    _short_deadline(monkeypatch)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _slow_flow())
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )
    monkeypatch.undo()

    r = await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )

    assert r.status_code == 200
    assert _sse(r.text)[-1]["type"] == "done"
    assert (await _turn(db_session, cid)).status == TurnStatus.COMPLETED


async def test_the_next_turn_sees_a_stopped_reply(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _stopping_flow())
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    seen: list = []

    async def capture(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        seen.append(kwargs.get("history", args[3] if len(args) > 3 else None))
        yield TurnEvent(type="done")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", capture)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Go on"})

    assert any(getattr(m, "content", None) == "Half an answer" for m in seen[0])


# --- the Stop route ------------------------------------------------------------------------


async def test_stop_asks_a_running_turn_to_stop(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, engine: AsyncEngine
) -> None:
    cid = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Hi"
    )
    claim = await turn_lock.claim(engine, uuid.UUID(cid))
    assert claim is not None
    try:
        async with turn_control.TurnControl(turn.id, deadline_s=5.0) as control:
            r = await api_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
            assert (r.status_code, r.json()) == (202, {"status": "stopping"})
            assert control.stopped
    finally:
        await claim.release()


async def test_stop_refuses_a_turn_that_is_not_running(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    assert r.status_code == 200
    turn = await _turn(db_session, cid)  # completed
    r = await api_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
    assert r.status_code == 409

    dead = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Again"
    )  # pending, but no process holds the conversation
    r = await api_client.post(f"{API}/conversations/{cid}/turns/{dead.id}/stop")
    assert r.status_code == 409


async def test_stop_refuses_a_turn_from_another_conversation(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    first = await _conversation(api_client, db_session)
    second = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(first), flow="tutor", content="Hi"
    )
    r = await api_client.post(f"{API}/conversations/{second}/turns/{turn.id}/stop")
    assert r.status_code == 404


async def test_stop_refuses_another_learners_conversation(
    api_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Hi"
    )
    other = Learner(handle=f"other-{uuid.uuid4().hex[:8]}", display_name="Other")
    db_session.add(other)
    await db_session.flush()
    await sign_in(anon_client, db_session, other)

    r = await anon_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
    assert r.status_code == 404
