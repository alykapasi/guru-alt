"""A grade says what produced it (S56)."""

import json
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import rubric_grading
from app.learning.grading import auto_grade, grade_flashcard
from app.learning.rubric_grading import GradedComponent, grade_open
from app.learning.turn_read import FULLY_CORRECT, ReadContext
from app.llm.decisions import FakeDecisionClient, YesNoAnswer
from app.llm.registry import fake_llm_client
from app.models.assessment import Item, ItemKC, ItemType, Rubric
from app.models.grading import GradingSnapshot
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc
from app.services import decisions, retention
from tests.decision_support import runtime, using

GRADE = json.dumps({"score": 0.8, "rationale": "ok"})


def test_auto_and_self_graders_name_themselves() -> None:
    mcq = auto_grade(ItemType.MCQ, {"correct": 1}, {"choice": 1})
    assert mcq.provenance is not None and mcq.provenance.grader == "auto"
    card = grade_flashcard({"rating": 3})
    assert card.provenance is not None and card.provenance.grader == "self"


async def test_the_rubric_grader_records_its_prompt_and_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "a"}, rubric=None
    )
    p = result.provenance
    assert p is not None and p.grader == "rubric"
    assert p.system_prompt == rubric_grading._SYSTEM_PROMPT
    assert p.template_version == rubric_grading.TEMPLATE_VERSION
    assert p.model == "fake:fake-1"


async def test_the_component_prompt_is_the_one_recorded_for_a_multi_component_item() -> None:
    comps = [GradedComponent(kc_id=uuid.uuid4(), name=n) for n in ("A", "B")]
    reply = json.dumps({"components": [{"n": 1, "score": 1}, {"n": 2, "score": 0}], "score": 0.5})
    result, _ = await grade_open(
        fake_llm_client(reply), stem="Q", response={"text": "a"}, rubric=None, components=comps
    )
    assert result.provenance is not None
    assert result.provenance.system_prompt == rubric_grading._COMPONENT_SYSTEM_PROMPT


async def test_an_empty_answer_names_the_grader_but_no_prompt_or_model() -> None:
    result, _ = await grade_open(
        fake_llm_client(GRADE), stem="Q", response={"text": "  "}, rubric=None
    )
    assert result.provenance is not None
    assert (result.provenance.grader, result.provenance.system_prompt, result.provenance.model) == (
        "rubric",
        None,
        None,
    )


async def test_a_system_override_is_what_is_sent_and_recorded() -> None:
    llm = fake_llm_client(GRADE)
    result, _ = await grade_open(
        llm, stem="Q", response={"text": "a"}, rubric=None, system="OLD PROMPT"
    )
    assert result.provenance is not None and result.provenance.system_prompt == "OLD PROMPT"


async def test_a_live_jev_pass_names_jev(db_session) -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)})

    async def smart():
        raise AssertionError("skipped")

    with using(runtime(fake, fully_correct="live")):
        result = await decisions.decide_grade(
            smart=smart,
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=ReadContext(learner_id=None, conversation_id=None, item_id=None),
            attempt_id=None,
        )
    assert result.provenance is not None and result.provenance.grader == "jev"


async def _setup(session: AsyncSession, *, item_type=ItemType.SHORT, rubric: dict | None = None):
    learner = Learner(handle=f"gp-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:6]}", name="Vectors")
    session.add(kc)
    await session.flush()
    rubric_row = None
    if rubric is not None:
        rubric_row = Rubric(owner_learner_id=learner.id, kc_id=kc.id, criteria=rubric)
        session.add(rubric_row)
        await session.flush()
    row = Item(
        owner_learner_id=learner.id,
        item_type=item_type,
        stem="Add two vectors.",
        answer_key={"correct": 1} if item_type == ItemType.MCQ else None,
        rubric_id=rubric_row.id if rubric_row else None,
    )
    session.add(row)
    await session.flush()
    session.add(ItemKC(item_id=row.id, kc_id=kc.id))
    await session.flush()
    item = await svc.get_item(session, row.id)
    assert item is not None
    return learner, kc, item


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.learner_id == learner_id)
            )
        ).all()
    )


async def _answer_mcq(session: AsyncSession, learner: Learner, item: Item, **submit) -> None:
    await svc.answer_item(
        session,
        learner.id,
        item,
        AnswerSubmit(response={"choice": 1}, **submit),
        llm=fake_llm_client(),
    )


async def test_a_rubric_grade_records_its_snapshots_on_the_event(db_session) -> None:
    learner, _kc, item = await _setup(db_session, rubric={"points": ["tip to tail"]})
    await svc.answer_item(
        db_session,
        learner.id,
        item,
        AnswerSubmit(response={"text": "tip to tail"}),
        llm=fake_llm_client(GRADE),
    )
    [event] = await _events(db_session, learner.id)
    grading = event.payload["grading"]
    assert event.payload["schema_version"] == 5
    assert grading["grader"] == "rubric" and grading["model"] == "fake:fake-1"
    kinds = {
        s.sha256: s.kind
        for s in await db_session.scalars(
            select(GradingSnapshot).where(GradingSnapshot.learner_id == learner.id)
        )
    }
    assert kinds[grading["item"]] == "item"
    assert kinds[grading["rubric"]] == "rubric"
    assert kinds[grading["prompt"]] == "prompt"


async def test_an_auto_grade_has_an_item_snapshot_and_nothing_else(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await _answer_mcq(db_session, learner, item)
    [event] = await _events(db_session, learner.id)
    g = event.payload["grading"]
    assert g["grader"] == "auto" and g["item"]
    assert (g["rubric"], g["prompt"], g["model"]) == (None, None, None)


async def test_the_same_item_graded_twice_is_one_snapshot(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    for _ in range(2):
        await _answer_mcq(db_session, learner, item)
    count = await db_session.scalar(
        select(func.count())
        .select_from(GradingSnapshot)
        .where(GradingSnapshot.learner_id == learner.id)
    )
    assert count == 1


async def test_a_retried_attempt_writes_nothing_twice(db_session) -> None:
    """Review focus 1."""
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    attempt = uuid.uuid4()
    for _ in range(2):
        await _answer_mcq(db_session, learner, item, attempt_id=attempt)
    [event] = await _events(db_session, learner.id)
    assert event.payload["grading"]["grader"] == "auto"
    count = await db_session.scalar(
        select(func.count())
        .select_from(GradingSnapshot)
        .where(GradingSnapshot.learner_id == learner.id)
    )
    assert count == 1


async def test_deleting_the_item_keeps_the_history(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await _answer_mcq(db_session, learner, item)
    await db_session.execute(delete(Item).where(Item.id == item.id))
    await db_session.flush()
    [event] = await _events(db_session, learner.id)
    snapshot = await db_session.get(GradingSnapshot, (learner.id, event.payload["grading"]["item"]))
    assert snapshot is not None and snapshot.content["stem"] == "Add two vectors."


async def test_snapshots_are_exported_and_erased_with_the_account(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await _answer_mcq(db_session, learner, item)
    exported = await retention.export_learner(db_session, learner.id)
    assert len(exported["grading_snapshots"]) == 1

    learner_id = learner.id
    await db_session.execute(delete(Learner).where(Learner.id == learner_id))
    await db_session.flush()
    remaining = await db_session.scalars(
        select(GradingSnapshot).where(GradingSnapshot.learner_id == learner_id)
    )
    assert remaining.all() == []


async def test_an_administrators_answer_carries_provenance_too(db_session) -> None:
    """Review focus 4."""
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    admin = Learner(handle=f"adm-{uuid.uuid4().hex[:8]}", is_admin=True)
    db_session.add(admin)
    await db_session.flush()
    db_session.info["admin_actor_id"] = str(admin.id)
    try:
        await _answer_mcq(db_session, learner, item)
    finally:
        db_session.info.pop("admin_actor_id", None)
    [event] = await _events(db_session, learner.id)
    assert event.event_type == "admin_observation"
    assert event.payload["grading"]["grader"] == "auto"
