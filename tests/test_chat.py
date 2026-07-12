"""Chat API e2e with a FakeProvider-backed registry (offline, deterministic)."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ChatMessage, ModelRole
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.services import chat as chat_svc
from app.services import lesson_plan as lesson_plan_svc

API = "/api/v1"
REPLY = "Let us explore this together."


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


class _RecordingProvider(FakeProvider):
    """Records the ``system`` prompt each ``stream`` call was given."""

    def __init__(self, reply: str, systems: list[str | None]) -> None:
        super().__init__(reply=reply)
        self._systems = systems

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[ChatChunk]:
        self._systems.append(system)
        async for chunk in super().stream(
            model=model, messages=messages, system=system, max_tokens=max_tokens
        ):
            yield chunk


@pytest.fixture
def recording_llm() -> Iterator[list[str | None]]:
    systems: list[str | None] = []
    provider = _RecordingProvider(REPLY, systems)
    client = LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})
    app.dependency_overrides[get_llm_client] = lambda: client
    yield systems
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


# --- subject-scoped conversations ---------------------------------------------------


async def test_create_conversation_with_subject_round_trips(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add(subject)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations", json={"title": "Chem help", "subject_id": str(subject.id)}
    )
    assert r.status_code == 201, r.text
    assert r.json()["subject_id"] == str(subject.id)


async def test_create_conversation_with_unknown_subject_404(
    api_client: AsyncClient, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(uuid.uuid4())})
    assert r.status_code == 404


# --- lesson-plan grounding ---------------------------------------------------


async def test_tutor_turn_is_grounded_by_the_active_lesson_plan_step(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Chem help"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand acids"
    await db_session.commit()

    # The conversation endpoint lazily creates the stub "dev" learner — reuse it so the plan
    # generated directly below is grounding the *same* learner's tutor turn.
    learner = await db_session.scalar(select(Learner).where(Learner.handle == "dev"))
    assert learner is not None
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug="acids", name="Acids and Bases")
    db_session.add(kc)
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200

    assert len(recording_llm) == 1
    system = recording_llm[0]
    assert system is not None
    assert "Understand acids" in system  # goal grounding still present
    assert "Acids and Bases" in system  # plan grounding


async def test_tutor_turn_without_a_plan_is_unaffected(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc help"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200
    assert recording_llm[0] == (
        f"{chat_svc.TUTOR_SYSTEM_PROMPT}\n\n"
        "The learner's stated goal for this conversation: Understand derivatives"
    )
