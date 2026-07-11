"""Refinement-gate service + HTTP-level tests: persistence orchestration around the gate graph."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ModelRole
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.services.refinement import run_refinement_turn
from app.services.turn_common import TurnEvent

API = "/api/v1"
REPLY = "Sounds like light reactions."


async def _conversation(session: AsyncSession) -> Conversation:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def _drain(
    session: AsyncSession,
    llm: LLMClient,
    conv: Conversation,
    *,
    user_content: str,
    satisfied: bool = False,
    resume: bool = False,
    max_rounds: int = 3,
) -> list[TurnEvent]:
    return [
        ev
        async for ev in run_refinement_turn(
            session,
            llm,
            learner_id=conv.learner_id,
            conversation=conv,
            user_content=user_content,
            satisfied=satisfied,
            max_tokens=256,
            max_rounds=max_rounds,
            resume=resume,
        )
    ]


async def test_start_persists_goal_and_proposal_and_awaits_reply(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    events = await _drain(db_session, fake_llm_client(REPLY), conv, user_content="photosynthesis")

    assert "".join(e.text for e in events if e.type == "token") == REPLY
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.text == REPLY
    assert awaiting.detail == "round 1"
    assert conv.goal is None

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "photosynthesis"
    assert messages[1].content == REPLY

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "fast"


async def test_resume_unsatisfied_persists_feedback_and_new_proposal(
    db_session: AsyncSession,
) -> None:
    conv = await _conversation(db_session)
    llm = fake_llm_client(REPLY)
    await _drain(db_session, llm, conv, user_content="photosynthesis")

    events = await _drain(db_session, llm, conv, user_content="more on dark reactions", resume=True)
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.detail == "round 2"
    assert conv.goal is None

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 2


async def test_resume_satisfied_commits_without_reproposing(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    llm = fake_llm_client(REPLY)
    await _drain(db_session, llm, conv, user_content="photosynthesis")

    events = await _drain(
        db_session, llm, conv, user_content="yes exactly", satisfied=True, resume=True
    )
    committed = next(e for e in events if e.type == "committed")
    assert committed.text == REPLY
    assert committed.detail == "accepted"
    assert conv.goal == REPLY
    # No new tokens streamed on the committing call (propose didn't re-run).
    assert [e for e in events if e.type == "token"] == []

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    # user goal, assistant proposal, user "yes exactly" -- no duplicate assistant message.
    assert [m.role for m in messages] == ["user", "assistant", "user"]

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1  # not double-logged on the committing call


async def test_max_rounds_auto_commits(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    llm = fake_llm_client(REPLY)
    await _drain(db_session, llm, conv, user_content="photosynthesis", max_rounds=2)
    await _drain(db_session, llm, conv, user_content="no", resume=True, max_rounds=2)
    events = await _drain(db_session, llm, conv, user_content="no", resume=True, max_rounds=2)

    committed = next(e for e in events if e.type == "committed")
    assert committed.detail == "auto"
    assert conv.goal == REPLY


class _BoomProvider(FakeProvider):
    """A provider whose stream raises immediately (simulates mid-generation failure)."""

    async def stream(self, *, model, messages, system=None, max_tokens=1024):
        raise RuntimeError("boom")
        yield ChatChunk()  # unreachable; makes this an async generator


async def test_stream_failure_persists_user_only(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    client = LLMClient(
        {"fake": _BoomProvider()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )
    events = await _drain(db_session, client, conv, user_content="photosynthesis")

    assert events[-1].type == "error"
    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert [m.role for m in messages] == ["user"]
    assert (await db_session.scalars(select(LLMCall))).all() == []


# --- HTTP-level: the router's dispatch between the gate and plain chat ---


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_dispatch_gate_then_plain_chat(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={})
    conversation_id = r.json()["id"]

    # First message on a fresh conversation -> the gate, not plain generation.
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "I want to learn photosynthesis"},
    )
    events = _parse_sse(r.text)
    assert "".join(e["text"] for e in events if e["type"] == "token") == REPLY
    awaiting = next(e for e in events if e["type"] == "awaiting_reply")
    assert awaiting["detail"] == "round 1"
    assert not any(e["type"] == "done" for e in events)

    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None and conversation.goal is None

    # Accept the proposal -> the gate commits, still no plain-generation "done" event.
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "yes exactly", "satisfied": True},
    )
    events = _parse_sse(r.text)
    committed = next(e for e in events if e["type"] == "committed")
    assert committed["goal"] == REPLY
    assert not any(e["type"] in ("token", "done") for e in events)

    await db_session.refresh(conversation)
    assert conversation.goal == REPLY

    # A third, ordinary message now flows through plain generation.
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "let's begin"},
    )
    events = _parse_sse(r.text)
    assert "".join(e["text"] for e in events if e["type"] == "token") == REPLY
    done = next(e for e in events if e["type"] == "done")
    assert done["usage"]["output_tokens"] == len(REPLY.split())

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert sorted(c.role for c in calls) == ["fast", "smart"]
