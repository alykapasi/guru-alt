"""Chat API e2e with a FakeProvider-backed registry (offline, deterministic)."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, LLMCall, Message

API = "/api/v1"
REPLY = "Let us explore this together."


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_chat_streams_and_persists(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc help"})
    assert r.status_code == 201, r.text
    conversation_id = r.json()["id"]

    # A message to a goal-less, history-less conversation triggers the refinement gate
    # (see tests/test_refinement.py). Set a goal directly to exercise plain generation here.
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "What is a derivative?"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)

    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert "".join(tokens) == REPLY

    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["usage"]["output_tokens"] == len(REPLY.split())

    # Both turns persisted, in order.
    messages = (
        await db_session.scalars(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(conversation_id))
            .order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == REPLY

    # Token/cost logged for the call.
    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "smart"
    assert calls[0].output_tokens == len(REPLY.split())


async def test_send_to_missing_conversation_404(api_client: AsyncClient, fake_llm: None) -> None:
    r = await api_client.post(
        f"{API}/conversations/{uuid.uuid4()}/messages", json={"content": "hi"}
    )
    assert r.status_code == 404


async def test_empty_message_422(api_client: AsyncClient, fake_llm: None) -> None:
    r = await api_client.post(f"{API}/conversations", json={})
    conversation_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": ""}
    )
    assert r.status_code == 422
