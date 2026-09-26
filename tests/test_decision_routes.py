"""Each answer path, with Jev in shadow: what the learner gets is unchanged, and the rows exist.

Four routes reach grading (spec §3, §5): a conversational check (one read for both questions), a
paused guided-practice question (intent at the gate), a direct submission (grading only), and a
guided-practice answer graded inside the workflow graph itself (grading only, a fresh read).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client
from app.learning.turn_read import FULLY_CORRECT, INTENT
from app.llm.decisions import ChoiceAnswer, FakeDecisionClient, YesNoAnswer
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.decision import DecisionCall
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import chat as chat_svc
from app.services import practice
from app.services import workflow as workflow_svc
from tests.decision_support import runtime, using
from tests.test_assessment import RUBRIC_REPLY, _seed_kcs
from tests.test_conversation_evidence import GRADE_REPLY, _open_check, _role_client
from tests.test_practice_pause import _presented
from tests.test_workflow import (
    PRESENT,
    RESPOND_2,
    RIGHT_GRADE,
    _learner_and_subject_with_active_step,
    _parse_sse,
)

API = "/api/v1"


def _jev(intent: str = "attempt", yes: float = 0.97) -> FakeDecisionClient:
    return FakeDecisionClient(
        {
            INTENT: ChoiceAnswer(label=intent, probabilities={intent: 0.95}, confidence=0.95),
            FULLY_CORRECT: YesNoAnswer(probability=yes),
        }
    )


async def _rows(session: AsyncSession) -> list[DecisionCall]:
    return list((await session.scalars(select(DecisionCall).order_by(DecisionCall.question))).all())


async def test_a_conversational_check_asks_both_questions_in_one_request(
    db_session: AsyncSession,
) -> None:
    learner, conversation, item = await _open_check(db_session)
    llm, _ = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)
    jev = _jev()

    with using(runtime(jev, intent="shadow", fully_correct="shadow")):
        open_check, outcome = await chat_svc._resolve_check(
            db_session,
            llm,
            learner_id=learner.id,
            conversation=conversation,
            user_content="Velocity is speed with a direction.",
        )

    assert open_check is None and outcome is not None
    assert outcome.result.score == pytest.approx(0.9)  # SMART's grade, not Jev's
    assert len(jev.requests) == 1
    rows = await _rows(db_session)
    assert [r.question for r in rows] == ["fully_correct", "intent"]
    assert len({r.request_id for r in rows}) == 1
    grade_row, intent_row = rows
    assert intent_row.baseline_intent == "attempt"
    assert grade_row.baseline_score == pytest.approx(0.9)
    assert {r.conversation_id for r in rows} == {conversation.id}
    assert {r.item_id for r in rows} == {item.id}


async def test_a_withdrawal_leaves_the_grade_question_unrecorded_and_nothing_breaks(
    db_session: AsyncSession,
) -> None:
    """Review focus 2: the read asked `fully_correct`, but no grading follows a withdrawal."""
    learner, conversation, _ = await _open_check(db_session)
    llm, _ = _role_client(fast='{"intent": "withdrawal"}', smart=GRADE_REPLY)

    with using(runtime(_jev("withdrawal"), intent="shadow", fully_correct="shadow")):
        open_check, outcome = await chat_svc._resolve_check(
            db_session,
            llm,
            learner_id=learner.id,
            conversation=conversation,
            user_content="let's talk about something else",
        )

    assert open_check is None and outcome is None
    assert [r.question for r in await _rows(db_session)] == ["intent"]


async def test_a_paused_practice_question_is_gated_in_shadow(db_session: AsyncSession) -> None:
    conv, llm = await _presented(db_session, [])
    item_id = await workflow_svc.paused_item_id(
        llm, db_session, conv.id, learner_id=conv.learner_id
    )
    assert item_id is not None

    with using(runtime(_jev("attempt"), intent="shadow")):
        intent = await practice.classify_paused_message(
            db_session,
            llm,
            learner_id=conv.learner_id,
            conversation=conv,
            item_id=item_id,
            content="wait, what is chlorophyll?",
        )

    (row,) = await _rows(db_session)
    # The gate's own verdict stands; Jev's disagreement is only recorded.
    assert row.baseline_intent == intent.value
    assert row.answer == "attempt"
    assert row.item_id == item_id


async def test_a_guided_practice_answer_is_gated_in_shadow_inside_the_workflow_graph(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """The fourth route (spec §5): a guided-practice answer is graded inside the workflow graph
    itself (assessment_svc.answer_item -> _grade -> decide_grade), not through
    chat._resolve_check or the direct-submission endpoint. It starts its own read — the router
    resolves the flow's intent gate first (classify_paused_message), which is a separate read
    that leaves no fully_correct row of its own here since intent stays off."""
    _learner, subject = await _learner_and_subject_with_active_step(db_session, learner=api_learner)
    script = [
        FakeTurn(text=PRESENT),
        FakeTurn(text='{"intent": "attempt"}'),
        FakeTurn(text=RIGHT_GRADE),
        FakeTurn(text=RESPOND_2),
    ]
    client = fake_llm_client(script=script)
    app.dependency_overrides[get_llm_client] = lambda: client
    try:
        r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
        conversation_id = r.json()["id"]
        await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "let's practice", "mode": "workflow"},
        )

        with using(runtime(_jev(yes=0.4), fully_correct="shadow")):
            r = await api_client.post(
                f"{API}/conversations/{conversation_id}/messages",
                json={"content": "sunlight -> sugars"},
            )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    done = next(e for e in _parse_sse(r.text) if e["type"] == "done")
    assert done["detail"] == "mastered"  # SMART's 0.9 grade decided, not Jev's 0.4

    (row,) = [row for row in await _rows(db_session) if row.question == FULLY_CORRECT]
    assert row.baseline_score == pytest.approx(0.9)
    assert row.used is False


@pytest.fixture
def fake_grader():
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client(reply=RUBRIC_REPLY)
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def test_a_direct_submission_asks_only_the_grade_question(
    api_client: AsyncClient, db_session: AsyncSession, fake_grader: None
) -> None:
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "short", "stem": "Explain photosynthesis.", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    jev = _jev(yes=0.99)
    attempt = str(uuid.uuid4())

    with using(runtime(jev, intent="shadow", fully_correct="shadow")):
        r = await api_client.post(
            f"{API}/items/{item_id}/answer",
            json={"response": {"text": "Plants turn light into sugar."}, "attempt_id": attempt},
        )

    assert r.status_code == 200, r.text
    assert r.json()["score"] == 0.75  # SMART's grade
    ((_, asked),) = jev.requests
    assert list(asked) == [FULLY_CORRECT]
    (row,) = await _rows(db_session)
    assert row.baseline_score == pytest.approx(0.75)
    assert str(row.attempt_id) == attempt


async def test_a_retried_attempt_asks_jev_nothing(
    api_client: AsyncClient, db_session: AsyncSession, fake_grader: None
) -> None:
    """Review focus 1: a replayed attempt returns the recorded grade before grading runs."""
    (kc,) = await _seed_kcs(db_session)
    body = {"item_type": "short", "stem": "Explain osmosis.", "kcs": [{"kc_id": str(kc.id)}]}
    item_id = (await api_client.post(f"{API}/items", json=body)).json()["id"]
    submission = {
        "response": {"text": "Water moves across a membrane."},
        "attempt_id": str(uuid.uuid4()),
    }
    jev = _jev()

    with using(runtime(jev, fully_correct="shadow")):
        await api_client.post(f"{API}/items/{item_id}/answer", json=submission)
        await api_client.post(f"{API}/items/{item_id}/answer", json=submission)

    assert len(jev.requests) == 1
    assert len(await _rows(db_session)) == 1


async def test_everything_off_behaves_exactly_as_before(db_session: AsyncSession) -> None:
    learner, conversation, _ = await _open_check(db_session)
    llm, provider = _role_client(fast='{"intent": "attempt"}', smart=GRADE_REPLY)

    _, outcome = await chat_svc._resolve_check(
        db_session,
        llm,
        learner_id=learner.id,
        conversation=conversation,
        user_content="Velocity is speed with a direction.",
    )

    assert outcome is not None and outcome.result.score == pytest.approx(0.9)
    assert len(provider.systems) == 2  # the FAST gate and the SMART grader, as today
    assert await _rows(db_session) == []
    events = (
        await db_session.scalars(
            select(LearningEvent).where(LearningEvent.learner_id == learner.id)
        )
    ).all()
    assert [e.event_type for e in events] == ["observation"]
