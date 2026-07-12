"""Session runner: turning the plan's active step into a practice item."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation
from app.llm.registry import fake_llm_client
from app.models.assessment import ItemType
from app.models.chat import LLMCall
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.models.profile import ProfileDimension
from app.services import lesson_plan as lesson_plan_svc
from app.services import session_runner as svc

MCQ_REPLY = json.dumps({"stem": "What is X?", "choices": ["A", "B", "C", "D"], "correct": 2})
FLASHCARD_REPLY = json.dumps({"stem": "What is X?", "answer": "Y"})
FILL_BLANK_REPLY = json.dumps({"stem": "X is the ___.", "answer": "Y"})
SHORT_REPLY = json.dumps({"stem": "Explain X in your own words."})


async def _graph(session: AsyncSession) -> tuple[Learner, Subject, KC, KC]:
    """A subject with a root KC and a dependent KC (root -> dependent prerequisite)."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    root = KC(topic_id=topic.id, slug="a-root", name="A Root")
    dependent = KC(topic_id=topic.id, slug="b-dependent", name="B Dependent")
    session.add_all([root, dependent])
    await session.flush()
    session.add(KCEdge(kc_id=dependent.id, prereq_kc_id=root.id))
    await session.flush()
    return learner, subject, root, dependent


async def test_next_item_none_when_no_plan(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Chemistry")
    db_session.add_all([learner, subject])
    await db_session.flush()

    item = await svc.next_item(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id
    )
    assert item is None


async def test_next_item_none_when_plan_is_fully_done(db_session: AsyncSession) -> None:
    learner, subject, root, dependent = await _graph(db_session)
    for kc_id in (root.id, dependent.id):
        db_session.add(
            LearnerKCState(learner_id=learner.id, kc_id=kc_id, ability=1.5, uncertainty=0.3)
        )
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    item = await svc.next_item(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id
    )
    assert item is None


async def test_next_item_reuses_an_existing_bank_item(db_session: AsyncSession) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    existing, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), root
    )
    assert existing is not None

    item = await svc.next_item(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert item.id == existing.id

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 0


async def test_next_item_generates_when_bank_is_empty(db_session: AsyncSession) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    item = await svc.next_item(
        db_session, fake_llm_client(MCQ_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert [link.kc_id for link in item.kc_links] == [root.id]

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "fast"


async def test_next_item_serves_a_due_review_step_as_a_flashcard(db_session: AsyncSession) -> None:
    """Review steps with no profile-driven type preference default to a flashcard — the
    session runner's spaced-repetition surfacing default."""
    learner, subject, root, _dependent = await _graph(db_session)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=root.id,
            ability=0.8,
            uncertainty=0.4,
            due_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    item = await svc.next_item(
        db_session, fake_llm_client(FLASHCARD_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert item.item_type == ItemType.FLASHCARD
    assert [link.kc_id for link in item.kc_links] == [root.id]


async def test_next_item_honors_a_profile_preferred_item_type(db_session: AsyncSession) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="format_effectiveness",
            value={"mcq": {"mean_score": 0.2}, "fill_blank": {"mean_score": 0.9}},
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    item = await svc.next_item(
        db_session, fake_llm_client(FILL_BLANK_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert item.item_type == ItemType.FILL_BLANK
    assert [link.kc_id for link in item.kc_links] == [root.id]


async def test_next_item_preferred_type_generation_wins_over_any_type_reuse(
    db_session: AsyncSession,
) -> None:
    """A bank item of a *different* type shouldn't be served instead of generating the
    profile-preferred type — preferred-type generation is tried before the any-type fallback."""
    learner, subject, root, _dependent = await _graph(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="format_effectiveness",
            value={"flashcard": {"mean_score": 0.9}},
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    existing_mcq, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), root
    )
    assert existing_mcq is not None

    item = await svc.next_item(
        db_session, fake_llm_client(FLASHCARD_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert item.item_type == ItemType.FLASHCARD
    assert item.id != existing_mcq.id


async def test_next_item_preferred_type_generation_failure_falls_back_to_any_type(
    db_session: AsyncSession,
) -> None:
    learner, subject, root, _dependent = await _graph(db_session)
    db_session.add(
        ProfileDimension(
            learner_id=learner.id,
            key="format_effectiveness",
            value={"flashcard": {"mean_score": 0.9}},
            uncertainty=0.3,
            kind="trait",
            source="behavioral",
        )
    )
    await db_session.flush()
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    existing_mcq, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), root
    )
    assert existing_mcq is not None

    # Flashcard generation fails to parse — falls through to reusing the existing MCQ rather
    # than returning None.
    item = await svc.next_item(
        db_session, fake_llm_client("not json"), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert item.id == existing_mcq.id


async def test_next_item_none_on_malformed_generation_reply(db_session: AsyncSession) -> None:
    learner, subject, _root, _dependent = await _graph(db_session)
    await lesson_plan_svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )

    item = await svc.next_item(
        db_session, fake_llm_client("not json"), learner_id=learner.id, subject_id=subject.id
    )
    assert item is None


# --- short_answer_item_for_kc (the guided-practice workflow's item resolver) --------------


async def test_short_answer_item_for_kc_reuses_a_seeded_bank_item(db_session: AsyncSession) -> None:
    _learner, _subject, root, _dependent = await _graph(db_session)
    existing, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), root
    )
    assert existing is not None

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client(), learner_id=uuid.uuid4(), kc=root
    )
    assert item is not None
    assert item.id == existing.id

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 0


async def test_short_answer_item_for_kc_generates_when_bank_is_empty(
    db_session: AsyncSession,
) -> None:
    learner, _subject, root, _dependent = await _graph(db_session)

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, kc=root
    )
    assert item is not None
    assert item.item_type == ItemType.SHORT
    assert [link.kc_id for link in item.kc_links] == [root.id]

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "fast"


async def test_short_answer_item_for_kc_no_mcq_fallback_on_generation_failure(
    db_session: AsyncSession,
) -> None:
    """Unlike item_for_kc, a failed SHORT generation must not fall back to a different-type
    bank item — the workflow's grading only makes sense against a SHORT item."""
    learner, _subject, root, _dependent = await _graph(db_session)
    existing_mcq, _ = await item_generation.generate_mcq_item(
        db_session, fake_llm_client(MCQ_REPLY), root
    )
    assert existing_mcq is not None

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client("not json"), learner_id=learner.id, kc=root
    )
    assert item is None
