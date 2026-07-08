"""LangGraph tutor substrate: the graph + node stream and fill state via FakeProvider."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tutor import TutorState, build_tutor_graph
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ChatMessage, ChatRole, ModelRole, Usage
from app.models.chat import Conversation, LLMCall, Message
from app.models.learner import Learner
from app.services.chat import TurnEvent, run_tutor_turn

REPLY = "Let us explore this together."


def _state(user: str = "What is a derivative?") -> TutorState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content=user)],
        "system": "You are a tutor.",
        "max_tokens": 256,
        "reply": "",
        "usage": Usage(),
    }


async def test_generate_node_fills_reply_and_usage() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    final = await graph.ainvoke(_state())
    assert final["reply"] == REPLY
    assert final["usage"].output_tokens == len(REPLY.split())


async def test_graph_streams_tokens_incrementally() -> None:
    graph = build_tutor_graph(fake_llm_client(REPLY))
    tokens: list[str] = []
    async for mode, payload in graph.astream(_state(), stream_mode=["custom", "values"]):
        if mode == "custom":
            tokens.append(payload["token"])  # ty: ignore[invalid-argument-type]
    # One custom payload per streamed word chunk (proves incremental, not buffered).
    assert len(tokens) == len(REPLY.split())
    assert "".join(tokens) == REPLY


class _BoomProvider(FakeProvider):
    """A provider whose stream raises immediately (simulates mid-generation failure)."""

    async def stream(self, *, model, messages, system=None, max_tokens=1024):
        raise RuntimeError("boom")
        yield ChatChunk()  # unreachable; makes this an async generator


async def _conversation(session: AsyncSession) -> Conversation:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def _drain(session: AsyncSession, llm: LLMClient, conv: Conversation) -> list[TurnEvent]:
    return [
        ev
        async for ev in run_tutor_turn(
            session,
            llm,
            learner_id=conv.learner_id,
            conversation_id=conv.id,
            history=[],
            user_content="What is a derivative?",
            max_tokens=256,
        )
    ]


async def test_run_tutor_turn_persists_turn_and_logs_call(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    events = await _drain(db_session, fake_llm_client(REPLY), conv)

    assert [e.type for e in events if e.type != "token"][-1] == "done"
    assert "".join(e.text for e in events if e.type == "token") == REPLY
    done = next(e for e in events if e.type == "done")
    assert done.message_id is not None
    assert done.usage.output_tokens == len(REPLY.split())

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == REPLY
    assert messages[1].model == "fake-1"

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "smart"
    assert calls[0].output_tokens == len(REPLY.split())


async def test_run_tutor_turn_stream_failure_persists_user_only(db_session: AsyncSession) -> None:
    conv = await _conversation(db_session)
    client = LLMClient(
        {"fake": _BoomProvider()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )

    events = await _drain(db_session, client, conv)

    assert events[-1].type == "error"
    assert events[-1].detail == "generation failed"
    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert [m.role for m in messages] == ["user"]  # user persisted, no assistant
    assert (await db_session.scalars(select(LLMCall))).all() == []
