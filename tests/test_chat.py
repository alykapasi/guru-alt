"""Chat API e2e with a FakeProvider-backed registry (offline, deterministic)."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.learning import item_generation
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ChatMessage, ModelRole
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.services import chat as chat_svc
from app.services import lesson_plan as lesson_plan_svc

API = "/api/v1"
REPLY = "Let us explore this together."
MCQ_REPLY = json.dumps({"stem": "What is X?", "choices": ["A", "B", "C", "D"], "correct": 2})


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
    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["item"] is None


# --- session runner: subject-scoped conversations get a practice item --------


async def _seeded_subject(
    db_session: AsyncSession, learner_id: uuid.UUID, name: str, kc_name: str
) -> tuple[Subject, KC]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name=name)
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug=f"kc-{uuid.uuid4().hex[:8]}", name=kc_name)
    db_session.add(kc)
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner_id, subject_id=subject.id, goal=None
    )
    return subject, kc


async def _get_dev_learner(api_client: AsyncClient, db_session: AsyncSession) -> Learner:
    learner = await db_session.scalar(select(Learner).where(Learner.handle == "dev"))
    if learner is None:
        # The conversation endpoint lazily creates it; force that once, then reuse.
        r = await api_client.post(f"{API}/conversations", json={"title": "bootstrap"})
        assert r.status_code == 201
        learner = await db_session.scalar(select(Learner).where(Learner.handle == "dev"))
    assert learner is not None
    return learner


async def test_subject_scoped_turn_grounds_and_serves_an_item_for_its_own_subject(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    learner = await _get_dev_learner(api_client, db_session)

    subject_a, kc_a = await _seeded_subject(db_session, learner.id, "Physics", "Kinematics")
    # Pre-seed the bank so the session runner reuses this item rather than generating one
    # through the recording provider (whose canned reply isn't valid MCQ JSON).
    seeded_item, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), kc_a
    )
    assert seeded_item is not None
    _subject_b, _kc_b = await _seeded_subject(db_session, learner.id, "Biology", "Cells")
    # subject_b's plan is now the most-recently-updated one overall.

    r = await api_client.post(
        f"{API}/conversations", json={"title": "Physics help", "subject_id": str(subject_a.id)}
    )
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Learn physics"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200

    system = recording_llm[0]
    assert system is not None
    assert "Kinematics" in system  # subject_a's KC, not subject_b's, despite B being newer
    assert "Cells" not in system

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["item"] is not None
    assert done["item"]["id"] == str(seeded_item.id)


async def test_subject_scoped_turn_with_no_plan_yet_gets_no_grounding_or_item(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    learner = await _get_dev_learner(api_client, db_session)

    # The learner has a plan for subject A, but the conversation is scoped to subject B, which
    # has no plan — must not fall back to A's.
    _subject_a, _kc_a = await _seeded_subject(db_session, learner.id, "Physics", "Kinematics")
    subject_b = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add(subject_b)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations", json={"title": "Bio help", "subject_id": str(subject_b.id)}
    )
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Learn biology"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200

    system = recording_llm[0]
    assert system is not None
    assert "Kinematics" not in system  # no fallback to subject A's plan

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["item"] is None


async def test_subject_scoped_turn_with_a_fully_done_plan_gets_no_item(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    subject, kc = await _seeded_subject(db_session, learner.id, "Physics", "Kinematics")
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=1.5, uncertainty=0.3))
    await db_session.commit()
    await lesson_plan_svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject.id)

    r = await api_client.post(
        f"{API}/conversations", json={"title": "Physics help", "subject_id": str(subject.id)}
    )
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Learn physics"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200
    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["item"] is None
