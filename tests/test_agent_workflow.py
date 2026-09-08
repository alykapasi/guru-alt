"""LangGraph guided-practice workflow substrate: present/interrupt/resume/grade/respond.

DB-backed (unlike test_agent_refinement.py's pure-graph tests) — the grade node calls the
real app.services.assessment.answer_item, so a real Item/db_session is required, mirroring
test_agent_agentic.py's DB e2e section.
"""

import uuid

import pytest
from langgraph.types import Command
from sqlalchemy import Float, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.workflow import WorkflowState, build_workflow_graph, workflow_config
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage
from app.models.assessment import Item, ItemType
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import ItemCreate, ItemKCRef
from app.services import assessment as assessment_svc

PRESENT = "Here's a worked example. Now try: explain photosynthesis."
RESPOND_1 = "Not quite — here's a hint, try again."
RESPOND_2 = "Great job, you've got it!"


async def _learner_and_item(session: AsyncSession) -> tuple[Learner, Item]:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Bio")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="photosynthesis", name="Photosynthesis")
    session.add(kc)
    await session.flush()
    item = await assessment_svc.create_item(
        session,
        ItemCreate(
            item_type=ItemType.SHORT,
            stem="Explain photosynthesis in your own words.",
            kcs=[ItemKCRef(kc_id=kc.id)],
        ),
    )
    return learner, item


def _state(item: Item, max_rounds: int = 3) -> WorkflowState:
    return {
        "messages": [ChatMessage(role=ChatRole.USER, content="let's practice")],
        "system": "You are Guru, running guided practice.",
        "max_tokens": 256,
        "item_id": str(item.id),
        "response_text": "",
        "last_message": "",
        "score": 0.0,
        "correct": False,
        "usage": Usage(),
        "rounds": 0,
        "max_rounds": max_rounds,
    }


async def test_first_invoke_pauses_at_await_response(db_session: AsyncSession) -> None:
    learner, item = await _learner_and_item(db_session)
    graph = build_workflow_graph(fake_llm_client(PRESENT), db_session, learner_id=learner.id)
    config = workflow_config("t-pause")

    await graph.ainvoke(_state(item), config)

    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("await_response",)
    assert snapshot.interrupts[0].value == {"prompt": PRESENT, "round": 1}


async def test_correct_first_try_ends_after_one_round(db_session: AsyncSession) -> None:
    learner, item = await _learner_and_item(db_session)

    script = [
        FakeTurn(text=PRESENT),
        FakeTurn(text='{"score": 1.0, "rationale": "nice"}'),
        FakeTurn(text=RESPOND_2),
    ]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-correct")

    await graph.ainvoke(_state(item), config)
    result = await graph.ainvoke(Command(resume={"response_text": "sunlight -> sugars"}), config)

    assert result["correct"] is True
    assert result["score"] == 1.0
    assert result["rounds"] == 1
    assert result["last_message"] == RESPOND_2
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()


async def test_wrong_then_correct_loops_once(db_session: AsyncSession) -> None:
    learner, item = await _learner_and_item(db_session)

    script = [
        FakeTurn(text=PRESENT),
        FakeTurn(text='{"score": 0.2, "rationale": "missing detail"}'),
        FakeTurn(text=RESPOND_1),
        FakeTurn(text='{"score": 0.9, "rationale": "much better"}'),
        FakeTurn(text=RESPOND_2),
    ]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-loop")

    await graph.ainvoke(_state(item), config)
    await graph.ainvoke(Command(resume={"response_text": "plants eat dirt"}), config)

    snapshot = await graph.aget_state(config)
    assert snapshot.next == ("await_response",)
    assert snapshot.interrupts[0].value == {"prompt": RESPOND_1, "round": 2}
    assert snapshot.values["rounds"] == 1

    result = await graph.ainvoke(Command(resume={"response_text": "sunlight -> sugars"}), config)
    assert result["correct"] is True
    assert result["rounds"] == 2
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()


async def test_max_rounds_ends_the_graph_even_when_still_wrong(db_session: AsyncSession) -> None:
    learner, item = await _learner_and_item(db_session)

    always_wrong = FakeTurn(text='{"score": 0.1, "rationale": "no"}')
    script = [FakeTurn(text=PRESENT), always_wrong, FakeTurn(text=RESPOND_1)]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-cap")

    await graph.ainvoke(_state(item, max_rounds=1), config)
    result = await graph.ainvoke(Command(resume={"response_text": "wrong answer"}), config)

    assert result["correct"] is False
    assert result["rounds"] == 1  # the cap stopped it, not correctness
    snapshot = await graph.aget_state(config)
    assert snapshot.next == ()


async def test_a_scaffolded_second_round_is_weaker_evidence_than_the_first(
    db_session: AsyncSession,
) -> None:
    """S13: the loop hints and re-asks the *same* question, so round 2 is not independent.

    Both rounds still leave their own evidence row — only how much each one counts changes.
    """
    learner, item = await _learner_and_item(db_session)

    script = [
        FakeTurn(text=PRESENT),
        FakeTurn(text='{"score": 0.2, "rationale": "missing detail"}'),
        FakeTurn(text=RESPOND_1),
        FakeTurn(text='{"score": 0.9, "rationale": "much better"}'),
        FakeTurn(text=RESPOND_2),
    ]
    graph = build_workflow_graph(fake_llm_client(script=script), db_session, learner_id=learner.id)
    config = workflow_config("t-assist")

    await graph.ainvoke(_state(item), config)
    await graph.ainvoke(Command(resume={"response_text": "plants eat dirt"}), config)
    await graph.ainvoke(Command(resume={"response_text": "sunlight -> sugars"}), config)

    events = (
        await db_session.scalars(
            select(LearningEvent)
            .where(LearningEvent.learner_id == learner.id)
            .order_by(LearningEvent.payload["score"].astext.cast(Float))
        )
    ).all()
    assert len(events) == 2
    first, second = events  # ordered by score: the wrong attempt, then the corrected one
    assert first.payload["hints_used"] == 0 and first.payload["credit"] == 1.0
    # A hint was given and the same question was re-asked — both dilute the demonstration.
    assert second.payload["hints_used"] == 1
    assert second.payload["prior_attempts"] == 1
    assert second.payload["credit"] == pytest.approx(1 / 3)
