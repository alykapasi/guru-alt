"""Side discussions pause practice; resuming and skipping are explicit (S52, V07)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.api.deps import get_llm_client
from app.api.v1.chat import TurnFlow, _phase_after
from app.learning.conversation_evidence import TurnIntent
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.assessment import Item
from app.models.chat import Conversation, ConversationPhase
from app.models.knowledge import KC
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.services import practice
from app.services import workflow as workflow_svc
from tests.test_workflow import (
    API,
    PRESENT,
    RESPOND_2,
    RIGHT_GRADE,
    _conversation_with_active_step,
    _drain,
    _flashcard_for,
    _learner_and_subject_with_active_step,
    _parse_sse,
)


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        await session.scalars(select(LearningEvent).where(LearningEvent.learner_id == learner_id))
    )


async def _presented(session: AsyncSession, script: list[FakeTurn]):
    conv = await _conversation_with_active_step(session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT), *script])
    await _drain(session, llm, conv, user_content="let's begin")
    conv.phase = ConversationPhase.AWAITING_ANSWER
    await session.commit()
    return conv, llm


async def _reload(session: AsyncSession, conv: Conversation) -> Conversation:
    """Force a genuine DB round-trip instead of reusing the in-memory object.

    This suite's ``db_session`` fixture sets ``expire_on_commit=False``, so an object a caller
    just mutated keeps holding whatever Python value was assigned — here, the actual
    ``ConversationPhase`` enum member ``pause``/``resume`` assigned to ``.phase``. A real
    request never gets that: it loads a fresh row, and asyncpg hands back a plain ``str`` for a
    text column. Expunging and re-fetching reproduces that plain-``str`` phase, which is exactly
    what exposed an `is`-vs-`==` phase comparison bug in review (V07): the comparison passed
    against a same-process enum object but silently failed — every time — against a freshly
    loaded row.
    """
    session.expunge(conv)
    reloaded = await session.get(Conversation, conv.id)
    assert reloaded is not None
    return reloaded


async def _make_paused_question_stale(session: AsyncSession, conv: Conversation) -> None:
    """Copied from tests/test_workflow.py's
    test_a_paused_question_the_learner_has_since_outgrown_is_not_resumed: record enough
    ability on the practised KC that `checkpoints.paused_practice_is_current` says mastered."""
    kc = await session.scalar(select(KC).where(KC.slug == "photosynthesis"))
    assert kc is not None
    session.add(
        LearnerKCState(
            learner_id=conv.learner_id,
            kc_id=kc.id,
            ability=1.5,
            uncertainty=0.4,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def test_a_side_question_is_classified_as_a_deferral(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text='{"intent": "deferral"}')])
    item_id = await workflow_svc.paused_item_id(
        llm, db_session, conv.id, learner_id=conv.learner_id
    )
    assert item_id is not None
    intent = await practice.classify_paused_message(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        item_id=item_id,
        content="wait, what is chlorophyll?",
    )
    assert intent is TurnIntent.DEFERRAL


async def test_a_gate_failure_is_a_deferral(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text="not json at all")])
    item_id = await workflow_svc.paused_item_id(
        llm, db_session, conv.id, learner_id=conv.learner_id
    )
    assert item_id is not None
    intent = await practice.classify_paused_message(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        item_id=item_id,
        content="sunlight makes sugar",
    )
    assert intent is TurnIntent.DEFERRAL


async def test_pause_then_resume_returns_the_same_question(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    events_before = await _events(db_session, conv.learner_id)
    paused = await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert paused.phase is ConversationPhase.PRACTICE_PAUSED
    assert conv.active_item_id is not None

    # Reload the row rather than reuse the object `pause` just mutated — see `_reload`. This is
    # what a real request's `resume` call actually receives.
    conv = await _reload(db_session, conv)
    assert type(conv.phase) is str

    resumed = await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert resumed.ended is False
    assert resumed.prompt == PRESENT
    assert resumed.item is not None and resumed.item.id == conv.active_item_id
    assert conv.phase == ConversationPhase.AWAITING_ANSWER
    assert await _events(db_session, conv.learner_id) == events_before


async def test_help_during_a_pause_counts_against_the_next_attempt(
    db_session: AsyncSession,
) -> None:
    conv, llm = await _presented(db_session, [FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    conv.practice_scaffolds = 2  # two tutor replies during the side discussion (Task 6 counts them)
    await db_session.commit()
    await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)

    await _drain(db_session, llm, conv, user_content="sunlight -> sugars", resume=True)
    observed = [
        e for e in await _events(db_session, conv.learner_id) if e.event_type == "observation"
    ]
    assert observed and all(e.payload["hints_used"] == 2 for e in observed)


async def test_skip_records_nothing_and_ends_practice(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    before = await _events(db_session, conv.learner_id)
    state = await practice.skip(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert state.phase is ConversationPhase.CHATTING
    assert conv.active_item_id is None and conv.practice_scaffolds == 0
    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )
    assert await _events(db_session, conv.learner_id) == before


async def test_starting_again_after_a_skip_carries_no_old_help(db_session: AsyncSession) -> None:
    """Review focus 4."""
    conv, llm = await _presented(db_session, [])
    conv.practice_scaffolds = 3
    await db_session.commit()
    await practice.skip(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    conv.practice_scaffolds = 3  # even if something left it set, a fresh start clears it
    await db_session.commit()
    await _drain(
        db_session, fake_llm_client(script=[FakeTurn(text=PRESENT)]), conv, user_content="again"
    )
    assert conv.practice_scaffolds == 0


async def test_resume_after_the_plan_moved_on_reports_that_practice_ended(
    db_session: AsyncSession,
) -> None:
    conv, llm = await _presented(db_session, [])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    # Make the paused question stale the way tests/test_workflow.py's
    # test_a_paused_question_the_learner_has_since_outgrown_is_not_resumed does.
    await _make_paused_question_stale(db_session, conv)
    state = await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)
    assert state.ended is True
    assert conv.phase == ConversationPhase.CHATTING


async def test_resume_when_not_paused_is_a_conflict(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    with pytest.raises(practice.PracticeConflict):
        await practice.resume(db_session, llm, learner_id=conv.learner_id, conversation=conv)


async def test_pause_without_live_practice_is_a_conflict(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    with pytest.raises(practice.PracticeConflict):
        await practice.pause(
            db_session, fake_llm_client(), learner_id=conv.learner_id, conversation=conv
        )


async def test_pause_when_already_paused_is_a_conflict(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)

    # Reload, same as the resume test above: the already-paused guard has to work against a
    # freshly loaded row's plain-`str` phase, not only the enum object `pause` left in memory.
    conv = await _reload(db_session, conv)
    assert type(conv.phase) is str

    with pytest.raises(practice.PracticeConflict):
        await practice.pause(db_session, llm, learner_id=conv.learner_id, conversation=conv)


# --- routing: what the router does with a message sent while practice waits (Task 6) ----------

TUTOR_REPLY = "Chlorophyll is the green pigment that captures light."


def _install(script: list[FakeTurn]) -> None:
    client = fake_llm_client(script=script)
    app.dependency_overrides[get_llm_client] = lambda: client


@pytest.fixture
def restore_llm() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def _started(api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner) -> str:
    _l, subject = await _learner_and_subject_with_active_step(db_session, learner=api_learner)
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    cid = r.json()["id"]
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "go", "mode": "workflow"}
    )
    return cid


async def _row(api_client: AsyncClient, cid: str) -> dict:
    return next(c for c in (await api_client.get(f"{API}/conversations")).json() if c["id"] == cid)


async def _graded_events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        await session.scalars(
            select(LearningEvent).where(
                LearningEvent.learner_id == learner_id,
                LearningEvent.event_type.in_(("observation", "self_report")),
            )
        )
    )


async def test_a_side_question_pauses_and_is_not_graded(
    api_client, db_session, api_learner, restore_llm
) -> None:
    # The fourth turn is the paused tutor's reply to the second message. It reads as an attempt
    # on purpose: were that message wrongly gated, the gate would take it as one and grade the
    # answer (turns five and six) — a gate that merely failed would fall back to a deferral and
    # hide the bug.
    _install(
        [
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "deferral"}'),
            FakeTurn(text=TUTOR_REPLY),
            FakeTurn(text='{"intent": "attempt"}'),
            FakeTurn(text=RIGHT_GRADE),
            FakeTurn(text=RESPOND_2),
        ]
    )
    cid = await _started(api_client, db_session, api_learner)
    item_id = (await _row(api_client, cid))["active_item_id"]

    r = await api_client.post(
        f"{API}/conversations/{cid}/messages",
        json={"content": "wait, what is chlorophyll?", "mode": "workflow"},
    )
    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["item"] is None  # the tutor opened no check of its own
    row = await _row(api_client, cid)
    assert (row["phase"], row["active_item_id"]) == ("practice_paused", item_id)

    # While paused, even an answer-shaped message is conversation, not a grade — no gate call.
    await api_client.post(
        f"{API}/conversations/{cid}/messages",
        json={"content": "sunlight -> sugars", "mode": "workflow"},
    )
    assert (await _row(api_client, cid))["phase"] == "practice_paused"
    assert await _graded_events(db_session, api_learner.id) == []
    conv = await db_session.get(Conversation, uuid.UUID(cid))
    assert conv is not None
    await db_session.refresh(conv)
    assert conv.practice_scaffolds == 2


async def test_an_attempt_is_still_graded(api_client, db_session, api_learner, restore_llm) -> None:
    _install(
        [
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "attempt"}'),
            FakeTurn(text=RIGHT_GRADE),
            FakeTurn(text=RESPOND_2),
        ]
    )
    cid = await _started(api_client, db_session, api_learner)
    r = await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "sunlight -> sugars"}
    )
    assert next(e for e in _parse_sse(r.text) if e["type"] == "done")["detail"] == "mastered"


async def test_a_withdrawal_skips_without_evidence(
    api_client, db_session, api_learner, restore_llm
) -> None:
    _install(
        [
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "withdrawal"}'),
            FakeTurn(text=TUTOR_REPLY),
        ]
    )
    cid = await _started(api_client, db_session, api_learner)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "I'd rather not"})
    row = await _row(api_client, cid)
    assert (row["phase"], row["active_item_id"]) == ("chatting", None)
    assert await _graded_events(db_session, api_learner.id) == []


async def test_an_agentic_turn_does_not_end_a_pause(
    api_client, db_session, api_learner, restore_llm
) -> None:
    """Only the explicit control resumes (spec §4.2) — an agentic interjection steps around the
    pause rather than ending it. As in the side-question test, the paused tutor's reply reads as
    an attempt so that a message wrongly sent through the gate would be graded."""
    _install(
        [
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "deferral"}'),
            FakeTurn(text=TUTOR_REPLY),
            FakeTurn(text="Here is what I found."),
            FakeTurn(text='{"intent": "attempt"}'),
            FakeTurn(text=RIGHT_GRADE),
            FakeTurn(text=RESPOND_2),
        ]
    )
    cid = await _started(api_client, db_session, api_learner)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "what is ATP?"})
    paused = await _row(api_client, cid)
    assert paused["phase"] == "practice_paused"

    await api_client.post(
        f"{API}/conversations/{cid}/messages",
        json={"content": "search my notes for ATP", "mode": "agentic"},
    )
    row = await _row(api_client, cid)
    assert (row["phase"], row["active_item_id"]) == ("practice_paused", paused["active_item_id"])

    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "sunlight -> sugars"}
    )
    assert (await _row(api_client, cid))["phase"] == "practice_paused"
    assert await _graded_events(db_session, api_learner.id) == []


async def test_a_stale_pause_is_released_to_ordinary_chat(
    api_client, db_session, api_learner, restore_llm
) -> None:
    """Review focus 5."""
    _install(
        [
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "deferral"}'),
            FakeTurn(text=TUTOR_REPLY),
            FakeTurn(text=TUTOR_REPLY),
        ]
    )
    cid = await _started(api_client, db_session, api_learner)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "what is ATP?"})
    assert (await _row(api_client, cid))["phase"] == "practice_paused"
    await checkpointing.discard_thread(cid)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "thanks"})
    assert (await _row(api_client, cid))["phase"] != "practice_paused"


async def test_a_flashcard_rating_is_graded_without_asking_the_gate(
    api_client, db_session, api_learner, restore_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec guard 14: a rating is unambiguous, so it must not spend (or be lost to) a gate call.

    The script holds no intent turn. Were the rating gated, the gate would consume RESPOND_2,
    fail to parse it, and pause practice as a deferral — and the rating would never be graded.
    """
    _l, subject = await _learner_and_subject_with_active_step(db_session, learner=api_learner)
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    cid = r.json()["id"]
    conv = await db_session.get(Conversation, uuid.UUID(cid))
    assert conv is not None
    card = await _flashcard_for(db_session, conv)

    async def _card(*_args: object, **_kwargs: object) -> Item:
        return card

    monkeypatch.setattr("app.services.workflow.short_answer_item_for_kc", _card)
    _install([FakeTurn(text=PRESENT), FakeTurn(text=RESPOND_2)])
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "go", "mode": "workflow"}
    )
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Good", "rating": 3}
    )
    assert [e.event_type for e in await _graded_events(db_session, api_learner.id)] == [
        "self_report"
    ]


def test_a_paused_tutor_turn_keeps_the_pause() -> None:
    assert (
        _phase_after(
            TurnFlow.TUTOR,
            awaiting_reply=False,
            workflow_paused=True,
            check_open=False,
            practice_paused=True,
        )
        is ConversationPhase.PRACTICE_PAUSED
    )
