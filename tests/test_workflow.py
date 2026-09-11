"""Guided-practice workflow service-level tests: persistence orchestration around the
workflow graph. Mirrors test_refinement.py's structure. HTTP-level dispatch tests are added
alongside the router wiring (see the mode="workflow" dispatch commit)."""

import json
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DEV_LEARNER_HANDLE, get_llm_client
from app.llm.providers import FakeProvider
from app.llm.providers.fake import FakeTurn
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatChunk, ModelRole
from app.main import app
from app.models.assessment import ItemType
from app.models.chat import Conversation, ConversationPhase, LLMCall, Message
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.schemas.assessment import ItemCreate, ItemKCRef
from app.services import assessment as assessment_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.turn_common import TurnEvent
from app.services.workflow import is_awaiting_reply, run_workflow_turn
from tests.embedding import FAKE_SPACE

API = "/api/v1"
PRESENT = "Here's a worked example. Now try: explain photosynthesis."
RESPOND_1 = "Not quite — here's a hint, try again."
RESPOND_2 = "Great job, you've got it!"
WRONG_GRADE = '{"score": 0.2, "rationale": "missing detail"}'
RIGHT_GRADE = '{"score": 0.9, "rationale": "much better"}'


async def _learner_and_subject_with_active_step(
    session: AsyncSession, *, handle: str | None = None
) -> tuple[Learner, Subject]:
    learner = Learner(handle=handle or f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="photosynthesis", name="Photosynthesis")
    session.add(kc)
    await session.flush()
    # Seed a bank SHORT item directly (no LLM call) so scripted FakeProvider turns in tests
    # below map 1:1 to present/grade/respond calls, not an extra item-generation call.
    await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.SHORT,
            stem="Explain photosynthesis in your own words.",
            kcs=[ItemKCRef(kc_id=kc.id)],
        ),
    )
    await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    await session.commit()
    await session.refresh(learner)
    await session.refresh(subject)
    return learner, subject


async def _conversation_with_active_step(session: AsyncSession) -> Conversation:
    learner, subject = await _learner_and_subject_with_active_step(session)
    conversation = Conversation(learner_id=learner.id, subject_id=subject.id, goal="learn biology")
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def _conversation_without_a_plan(session: AsyncSession) -> Conversation:
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
    resume: bool = False,
    max_rounds: int = 3,
) -> list[TurnEvent]:
    return [
        ev
        async for ev in run_workflow_turn(
            session,
            llm,
            learner_id=conv.learner_id,
            conversation=conv,
            user_content=user_content,
            max_tokens=256,
            max_rounds=max_rounds,
            resume=resume,
        )
    ]


async def test_start_persists_presentation_and_awaits_reply(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    events = await _drain(db_session, fake_llm_client(PRESENT), conv, user_content="let's practice")

    assert "".join(e.text for e in events if e.type == "token") == PRESENT
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.text == PRESENT
    assert awaiting.detail == "practice"
    assert awaiting.item is not None
    assert awaiting.item.item_type.value == "short"
    assert not any(e.type == "done" for e in events)

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].content == PRESENT

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "smart"


async def test_present_cites_retrieved_materials(db_session: AsyncSession) -> None:
    """The `present` step (worked example) grounds + cites; `respond` (Phase 7) doesn't —
    see test_resume_round_never_cites_even_if_respond_text_has_marker_syntax below."""
    conv = await _conversation_with_active_step(db_session)
    source = Source(
        learner_id=conv.learner_id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        status=SourceStatus.DONE,
        subject_id=conv.subject_id,
        meta={},
    )
    db_session.add(source)
    await db_session.flush()
    chunk = Chunk(
        embedding_space=FAKE_SPACE,
        source_id=source.id,
        ordinal=0,
        text="Photosynthesis converts light energy into chemical energy in chloroplasts.",
        embedding=(await fake_llm_client().embed(ModelRole.EMBED, ["seed"])).vectors[0],
        provenance={},
    )
    db_session.add(chunk)
    await db_session.commit()

    cited_present = "Here's a worked example [1]. Now try: explain photosynthesis."
    events = await _drain(
        db_session, fake_llm_client(cited_present), conv, user_content="let's practice"
    )
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.citations == [
        {"marker": 1, "chunk_id": str(chunk.id), "source_id": str(source.id)}
    ]

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert messages[1].citations == awaiting.citations


async def test_resume_wrong_loops_and_persists_second_round(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=WRONG_GRADE), FakeTurn(text=RESPOND_1)]
    )
    await _drain(db_session, llm, conv, user_content="let's practice")

    events = await _drain(db_session, llm, conv, user_content="wrong answer", resume=True)
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.text == RESPOND_1
    assert awaiting.detail == "practice"

    messages = (
        await db_session.scalars(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.created_at)
        )
    ).all()
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]

    # present (round 1) + grade's self-logged rubric call + respond (round 2's feedback).
    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 3


async def test_resume_correct_reaches_done_with_mastered_detail(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)]
    )
    await _drain(db_session, llm, conv, user_content="let's practice")

    events = await _drain(db_session, llm, conv, user_content="sunlight -> sugars", resume=True)
    done = next(e for e in events if e.type == "done")
    assert done.detail == "mastered"
    assert not any(e.type == "awaiting_reply" for e in events)

    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert [m.role for m in messages] == ["user", "assistant", "user", "assistant"]


async def test_resume_round_never_cites_even_if_respond_text_has_marker_syntax(
    db_session: AsyncSession,
) -> None:
    """`respond` (feedback on an attempt) is out of scope for citations even on a fresh start's
    first resume — gated on `resume`, not merely on whether `[N]` happens to appear in the
    text, since a learner's own answer or the model's phrasing could coincidentally contain it."""
    conv = await _conversation_with_active_step(db_session)
    respond_with_marker_syntax = "See problem [1] above — not quite, try again."
    llm = fake_llm_client(
        script=[
            FakeTurn(text=PRESENT),
            FakeTurn(text=WRONG_GRADE),
            FakeTurn(text=respond_with_marker_syntax),
        ]
    )
    await _drain(db_session, llm, conv, user_content="let's practice")

    events = await _drain(db_session, llm, conv, user_content="a wrong answer", resume=True)
    awaiting = next(e for e in events if e.type == "awaiting_reply")
    assert awaiting.citations == []


async def test_max_rounds_reaches_done_with_capped_detail(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=WRONG_GRADE), FakeTurn(text=RESPOND_1)]
    )
    await _drain(db_session, llm, conv, user_content="let's practice", max_rounds=1)

    events = await _drain(
        db_session, llm, conv, user_content="still wrong", resume=True, max_rounds=1
    )
    done = next(e for e in events if e.type == "done")
    assert done.detail == "capped"


async def test_no_active_plan_step_yields_clean_error(db_session: AsyncSession) -> None:
    conv = await _conversation_without_a_plan(db_session)
    llm = fake_llm_client(PRESENT)

    events = await _drain(db_session, llm, conv, user_content="let's practice")
    assert [e.type for e in events] == ["error"]

    assert (await db_session.scalars(select(LLMCall))).all() == []
    # No graph checkpoint was ever created — later dispatch still sees this as fresh.
    assert await is_awaiting_reply(llm, db_session, conv.id, learner_id=conv.learner_id) is False


class _BoomProvider(FakeProvider):
    """A provider whose stream raises immediately (simulates mid-generation failure)."""

    async def stream(self, *, model, messages, system=None, max_tokens=1024, tools=None):
        raise RuntimeError("boom")
        yield ChatChunk()  # unreachable; makes this an async generator


async def test_stream_failure_persists_user_only(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    client = LLMClient(
        {"fake": _BoomProvider()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}
    )
    events = await _drain(db_session, client, conv, user_content="let's practice")

    assert events[-1].type == "error"
    messages = (
        await db_session.scalars(select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert [m.role for m in messages] == ["user"]
    assert (await db_session.scalars(select(LLMCall))).all() == []


# --- HTTP-level: the router's dispatch to the workflow, and its priority over plain chat ---


@pytest.fixture
def fake_llm() -> Iterator[None]:
    # One shared client/FakeProvider instance across both HTTP requests below — a fresh
    # instance per dependency resolution (as in test_refinement.py's fixture, harmless there
    # since it's unscripted) would reset the scripted _call_index each request.
    script = [FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)]
    client = fake_llm_client(script=script)
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _parse_sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_dispatch_mode_workflow_then_resume_without_re_specifying_mode(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    # The stub auth seam always resolves to the dev learner — seed the plan/item for that
    # exact learner, then create the conversation through the real endpoint (not a raw DB
    # insert with an unrelated learner_id, which the router would 404 on).
    _learner, subject = await _learner_and_subject_with_active_step(
        db_session, handle=DEV_LEARNER_HANDLE
    )
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    conversation_id = r.json()["id"]

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "let's practice", "mode": "workflow"},
    )
    events = _parse_sse(r.text)
    awaiting = next(e for e in events if e["type"] == "awaiting_reply")
    assert awaiting["text"] == PRESENT
    assert awaiting["item"] is not None
    assert not any(e["type"] == "done" for e in events)

    # A follow-up turn with the default mode ("chat") still resumes the paused workflow —
    # is_awaiting_reply takes priority over the client's mode, exactly like the refinement gate.
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "sunlight -> sugars"},
    )
    events = _parse_sse(r.text)
    done = next(e for e in events if e["type"] == "done")
    assert done["detail"] == "mastered"


async def test_a_paused_session_records_the_item_it_is_waiting_on(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    """Without this the item exists only inside one SSE event, so a refresh loses the
    question the learner was on (S52)."""
    _learner, subject = await _learner_and_subject_with_active_step(
        db_session, handle=DEV_LEARNER_HANDLE
    )
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    conversation_id = r.json()["id"]

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "let's practice", "mode": "workflow"},
    )
    awaiting = next(e for e in _parse_sse(r.text) if e["type"] == "awaiting_reply")

    # What a reload sees, with no live stream to read from.
    listed = (await api_client.get(f"{API}/conversations")).json()
    row = next(c for c in listed if c["id"] == conversation_id)
    assert row["phase"] == ConversationPhase.AWAITING_ANSWER
    assert row["active_item_id"] == awaiting["item"]["id"]

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "sunlight -> sugars"},
    )
    assert next(e for e in _parse_sse(r.text) if e["type"] == "done")["detail"] == "mastered"

    # A finished run is no longer waiting on anything, and says so.
    listed = (await api_client.get(f"{API}/conversations")).json()
    row = next(c for c in listed if c["id"] == conversation_id)
    assert row["phase"] == ConversationPhase.CHATTING
    assert row["active_item_id"] is None
