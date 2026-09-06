"""Chat API e2e with a FakeProvider-backed registry (offline, deterministic)."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.api.v1 import chat as chat_router
from app.learning import item_generation
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ChatMessage, ModelRole, ToolCall, ToolDef
from app.main import app
from app.models.chat import Conversation, LLMCall, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.models.memory import Memory, MemoryKind
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.services import chat as chat_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services import memory as memory_svc
from app.services import turn_lock

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
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        self._systems.append(system)
        async for chunk in super().stream(
            model=model, messages=messages, system=system, max_tokens=max_tokens
        ):
            yield chunk


@pytest.fixture
def agentic_llm() -> Iterator[None]:
    """A scripted client: one tool-call turn, then a final text turn."""
    script = [
        FakeTurn(tool_calls=[ToolCall(id="t1", name="search_materials", input={"query": "x"})]),
        FakeTurn(text="here is your answer"),
    ]
    client = LLMClient(
        {"fake": FakeProvider(script=script)}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


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


async def test_rename_conversation(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Original"})
    conversation_id = r.json()["id"]

    r = await api_client.patch(f"{API}/conversations/{conversation_id}", json={"title": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed"

    r = await api_client.get(f"{API}/conversations")
    renamed = next(c for c in r.json() if c["id"] == conversation_id)
    assert renamed["title"] == "Renamed"


async def test_rename_missing_conversation_404(api_client: AsyncClient) -> None:
    r = await api_client.patch(f"{API}/conversations/{uuid.uuid4()}", json={"title": "Renamed"})
    assert r.status_code == 404


async def test_rename_conversation_empty_title_422(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/conversations", json={})
    conversation_id = r.json()["id"]

    r = await api_client.patch(f"{API}/conversations/{conversation_id}", json={"title": ""})
    assert r.status_code == 422


async def test_delete_conversation_cascades_messages(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "To delete"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200

    r = await api_client.delete(f"{API}/conversations/{conversation_id}")
    assert r.status_code == 204

    assert await db_session.get(Conversation, uuid.UUID(conversation_id)) is None
    remaining = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == uuid.UUID(conversation_id))
        )
    ).all()
    assert remaining == []

    r = await api_client.get(f"{API}/conversations")
    assert conversation_id not in [c["id"] for c in r.json()]


async def test_delete_missing_conversation_404(api_client: AsyncClient) -> None:
    r = await api_client.delete(f"{API}/conversations/{uuid.uuid4()}")
    assert r.status_code == 404


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


async def test_create_conversation_defaults_to_chat_kind(
    api_client: AsyncClient, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "chat"


async def test_create_session_conversation_round_trips(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add(subject)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations",
        json={"kind": "session", "subject_id": str(subject.id), "title": "Practice: Atoms"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "session"


async def test_create_session_conversation_without_subject_400(
    api_client: AsyncClient, fake_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"kind": "session"})
    assert r.status_code == 400


async def _learner_source(
    session: AsyncSession, learner_id: uuid.UUID, *, subject_id: uuid.UUID | None = None
) -> Source:
    source = Source(
        learner_id=learner_id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        status=SourceStatus.DONE,
        subject_id=subject_id,
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def test_create_conversation_with_narrowed_sources_round_trips(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add(subject)
    await db_session.flush()
    source = await _learner_source(db_session, learner.id, subject_id=subject.id)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations",
        json={"subject_id": str(subject.id), "source_ids": [str(source.id)]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["source_ids"] == [str(source.id)]


async def test_create_conversation_with_unknown_source_404(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add(subject)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations",
        json={"subject_id": str(subject.id), "source_ids": [str(uuid.uuid4())]},
    )
    assert r.status_code == 404


async def test_create_conversation_source_from_wrong_subject_400(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    subject_a = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    subject_b = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add_all([subject_a, subject_b])
    await db_session.flush()
    source = await _learner_source(db_session, learner.id, subject_id=subject_b.id)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations",
        json={"subject_id": str(subject_a.id), "source_ids": [str(source.id)]},
    )
    assert r.status_code == 400


async def test_create_conversation_source_ids_without_subject_400(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    source = await _learner_source(db_session, learner.id)
    await db_session.commit()

    r = await api_client.post(f"{API}/conversations", json={"source_ids": [str(source.id)]})
    assert r.status_code == 400


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


async def test_answering_a_served_item_through_the_real_endpoint_advances_the_next_turn(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    """The literal "session runner follows/updates the plan" DoD, end-to-end through the real
    HTTP endpoints (not direct service calls): a due review surfaces as a served item on one
    turn; answering it via ``POST /items/{id}/answer`` auto-revises the plan (assessment.py's
    existing, unchanged trigger); the very next turn serves something different."""
    learner = await _get_dev_learner(api_client, db_session)
    subject, kc = await _seeded_subject(db_session, learner.id, "Physics", "Kinematics")
    # Pre-seed the bank so the session runner reuses this item rather than generating one
    # through the recording provider (whose canned reply isn't valid flashcard JSON).
    flashcard_reply = json.dumps({"stem": "What is velocity?", "answer": "Speed with direction"})
    seeded_item, _ = await item_generation.generate_flashcard_item(
        db_session, fake_llm_client(flashcard_reply), kc
    )
    assert seeded_item is not None
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=kc.id,
            ability=0.8,
            uncertainty=0.4,
            due_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
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
    first_done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    first_item = first_done["item"]
    assert first_item is not None  # the due review, served as a flashcard

    r = await api_client.post(
        f"{API}/items/{first_item['id']}/answer", json={"response": {"recalled": True}}
    )
    assert r.status_code == 200, r.text

    # The review step is no longer due (FSRS rescheduled it forward), so it flips to done and
    # the plan's active step moves on to the KC's still-pending "new" step — proven on the plan
    # itself, since the session runner may legitimately re-serve the same (only) bank item for
    # the new step too (reuse-then-generate economics), so served-item identity alone can't
    # prove the plan advanced.
    plan = await lesson_plan_svc.get_lesson_plan(db_session, learner.id, subject.id)
    assert plan is not None
    review_step = next(
        s for s in plan.steps if s["kc_id"] == str(kc.id) and s["step_type"] == "review"
    )
    new_step = next(s for s in plan.steps if s["kc_id"] == str(kc.id) and s["step_type"] == "new")
    assert review_step["status"] == "done"
    assert new_step["status"] == "active"

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "next"}
    )
    assert r.status_code == 200
    second_done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert second_done["item"] is not None


# --- memory: retrieval wired into the tutor turn ------------------------------


async def test_tutor_turn_reflects_a_previously_written_memory(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    content = "Studying for the MCAT, mornings only."
    embedding = (await fake_llm_client().embed(ModelRole.EMBED, [content]))[0]
    db_session.add(
        Memory(learner_id=learner.id, kind=MemoryKind.FACT, content=content, embedding=embedding)
    )
    await db_session.commit()

    r = await api_client.post(f"{API}/conversations", json={"title": "Chem help"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand acids"
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages", json={"content": "hi"}
    )
    assert r.status_code == 200

    system = recording_llm[0]
    assert system is not None
    assert content in system


async def test_write_back_then_a_fresh_turn_reflects_the_written_memory(
    api_client: AsyncClient, db_session: AsyncSession, recording_llm: list[str | None]
) -> None:
    """The Phase 5 DoD check: write memory in one conversation, see it shape a later one."""
    learner = await _get_dev_learner(api_client, db_session)

    r = await api_client.post(f"{API}/conversations", json={"title": "First chat"})
    conv_a_id = uuid.UUID(r.json()["id"])
    db_session.add_all(
        [
            Message(
                conversation_id=conv_a_id,
                role="user",
                content="I'm studying for the MCAT, mornings only.",
            ),
            Message(conversation_id=conv_a_id, role="assistant", content="Got it."),
        ]
    )
    await db_session.commit()

    fact_reply = json.dumps({"memories": [{"kind": "fact", "content": "Studying for the MCAT."}]})
    written = await memory_svc.write_back(
        db_session, fake_llm_client(fact_reply), conversation_id=conv_a_id
    )
    assert len(written) == 1
    assert written[0].learner_id == learner.id

    r = await api_client.post(f"{API}/conversations", json={"title": "Second chat"})
    conv_b_id = r.json()["id"]
    conv_b = await db_session.get(Conversation, uuid.UUID(conv_b_id))
    assert conv_b is not None
    conv_b.goal = "Understand derivatives"
    await db_session.commit()

    r = await api_client.post(f"{API}/conversations/{conv_b_id}/messages", json={"content": "hi"})
    assert r.status_code == 200

    assert len(recording_llm) == 1  # write_back never calls .stream() — only .complete()/.embed()
    system = recording_llm[0]
    assert system is not None
    assert "Studying for the MCAT." in system


async def test_agentic_mode_bypasses_the_refinement_gate_and_streams_a_tool_call(
    api_client: AsyncClient, agentic_llm: None
) -> None:
    r = await api_client.post(f"{API}/conversations", json={"title": "Agentic test"})
    assert r.status_code == 201, r.text
    conversation_id = r.json()["id"]

    # No goal, no history — a plain "chat" turn here would trigger the refinement gate
    # (see test_chat_streams_and_persists's comment). mode="agentic" must skip it entirely.
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "search my notes for x", "mode": "agentic"},
    )
    assert r.status_code == 200, r.text
    events = _parse_sse(r.text)

    assert not any(e["type"] in ("awaiting_reply", "committed") for e in events)

    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["detail"] == "search_materials"

    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert "".join(tokens) == "here is your answer"

    done = next(e for e in events if e["type"] == "done")
    assert done["detail"] == ""


# --- citations (Phase 7): conversation scope + retrieval-grounded generation ------------


async def test_tutor_turn_cites_retrieved_materials(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add(subject)
    await db_session.flush()
    source = await _learner_source(db_session, learner.id, subject_id=subject.id)
    chunk = Chunk(
        source_id=source.id,
        ordinal=0,
        text="Mitochondria produce ATP through cellular respiration.",
        embedding=(await fake_llm_client().embed(ModelRole.EMBED, ["seed"]))[0],
        provenance={"method": "text"},
    )
    db_session.add(chunk)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/conversations", json={"title": "Bio help", "subject_id": str(subject.id)}
    )
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand cell biology"
    await db_session.commit()

    cited_reply = "The mitochondria produce ATP for the cell [1]."
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(cited_reply)
    try:
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "What produces energy in a cell?"},
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.status_code == 200, r.text

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["citations"] == [
        {"marker": 1, "chunk_id": str(chunk.id), "source_id": str(source.id)}
    ]

    messages = (
        await db_session.scalars(
            select(Message)
            .where(Message.conversation_id == uuid.UUID(conversation_id))
            .order_by(Message.created_at)
        )
    ).all()
    assert messages[1].citations == done["citations"]


async def test_general_conversation_has_no_citations(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A subject-less ("general") conversation never retrieves — no citations, no grounding —
    even when matching materials exist elsewhere for this learner."""
    learner = await _get_dev_learner(api_client, db_session)
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Biology")
    db_session.add(subject)
    await db_session.flush()
    source = await _learner_source(db_session, learner.id, subject_id=subject.id)
    db_session.add(
        Chunk(
            source_id=source.id,
            ordinal=0,
            text="Mitochondria produce ATP.",
            embedding=(await fake_llm_client().embed(ModelRole.EMBED, ["seed"]))[0],
            provenance={},
        )
    )
    await db_session.commit()

    r = await api_client.post(f"{API}/conversations", json={"title": "General chat"})
    conversation_id = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Just chatting"
    await db_session.commit()

    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("Sure, happy to help [1]!")
    try:
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "What produces energy in a cell?"},
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.status_code == 200, r.text

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    # The reply happens to contain "[1]" but nothing was ever retrieved to cite — no hits
    # means extract_citations has nothing to map it to.
    assert done["citations"] == []


async def test_agentic_mode_cites_search_materials_results(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    learner = await _get_dev_learner(api_client, db_session)
    source = await _learner_source(db_session, learner.id)
    chunk = Chunk(
        source_id=source.id,
        ordinal=0,
        text="The learner's notes say photosynthesis occurs in chloroplasts.",
        embedding=(await fake_llm_client().embed(ModelRole.EMBED, ["seed"]))[0],
        provenance={},
    )
    db_session.add(chunk)
    await db_session.commit()

    script = [
        FakeTurn(tool_calls=[ToolCall(id="t1", name="search_materials", input={"query": "x"})]),
        FakeTurn(text="Photosynthesis happens in the chloroplasts [1]."),
    ]
    client = LLMClient(
        {"fake": FakeProvider(script=script)}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        r = await api_client.post(f"{API}/conversations", json={"title": "Agentic cite test"})
        conversation_id = r.json()["id"]
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "where does photosynthesis happen?", "mode": "agentic"},
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
    assert r.status_code == 200, r.text

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["citations"] == [
        {"marker": 1, "chunk_id": str(chunk.id), "source_id": str(source.id)}
    ]


# --- one turn at a time per conversation (S34) --------------------------------


async def _goal_set_conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    """A conversation past the refinement gate, so a message runs a plain tutor turn."""
    conversation_id = (
        await api_client.post(f"{API}/conversations", json={"title": "Busy"})
    ).json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return conversation_id


async def test_a_second_turn_while_one_is_running_is_refused(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """Overlapping turns interleave messages and can resume the same paused graph twice."""
    conversation_id = await _goal_set_conversation(api_client, db_session)
    assert turn_lock.claim(uuid.UUID(conversation_id))
    try:
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages", json={"content": "and again?"}
        )
        assert r.status_code == 409
    finally:
        turn_lock.release(uuid.UUID(conversation_id))

    # Nothing was written for the refused turn.
    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == uuid.UUID(conversation_id))
        )
    ).all()
    assert messages == []


async def test_a_finished_turn_frees_the_conversation(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _goal_set_conversation(api_client, db_session)
    for _ in range(2):
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages", json={"content": "hello"}
        )
        assert r.status_code == 200
    assert turn_lock.is_active(uuid.UUID(conversation_id)) is False


async def test_a_turn_that_fails_to_start_frees_the_conversation(
    api_client: AsyncClient,
    db_session: AsyncSession,
    fake_llm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claim that outlived a failed dispatch would wedge the conversation permanently."""
    conversation_id = await _goal_set_conversation(api_client, db_session)

    async def boom(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("checkpoint lookup exploded")

    monkeypatch.setattr(chat_router.workflow_svc, "is_awaiting_reply", boom)
    with pytest.raises(RuntimeError):
        await api_client.post(
            f"{API}/conversations/{conversation_id}/messages", json={"content": "hello"}
        )
    assert turn_lock.is_active(uuid.UUID(conversation_id)) is False


async def test_a_different_conversation_is_unaffected(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """The claim is per conversation, not a global chat lock."""
    busy = await _goal_set_conversation(api_client, db_session)
    other = await _goal_set_conversation(api_client, db_session)
    assert turn_lock.claim(uuid.UUID(busy))
    try:
        r = await api_client.post(
            f"{API}/conversations/{other}/messages", json={"content": "hello"}
        )
        assert r.status_code == 200
    finally:
        turn_lock.release(uuid.UUID(busy))
