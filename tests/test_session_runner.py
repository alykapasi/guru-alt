"""Session runner: turning the plan's active step into a practice item."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation, mastery
from app.learning.mastery import Observation
from app.llm.registry import fake_llm_client
from app.models.assessment import RUBRIC_GRADABLE, ItemType
from app.models.chat import LLMCall
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
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
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, subject_id=subject.id
    )
    assert item is not None
    assert [link.kc_id for link in item.kc_links] == [root.id]
    # The type is asserted, not just that something came back: the MCQ-shaped reply this test
    # used to send also parses as a SHORT item (only ``stem`` is required), so without this
    # line the test passed identically whichever type the fallback generated.
    assert item.item_type == ItemType.SHORT

    calls = (await db_session.scalars(select(LLMCall))).all()
    assert len(calls) == 1
    assert calls[0].role == "fast"


def test_the_generated_default_is_a_type_that_can_be_diagnosed() -> None:
    """An MCQ carries no failure kind and no per-component split, and four passes of work now
    read both. A default nothing asked for should produce the type that can fill them."""
    assert svc.DEFAULT_GENERATED_TYPE in RUBRIC_GRADABLE


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
            key="score_by_format",
            value={
                "mcq": {"mean_score": 0.2, "mean_difficulty": 0.5},
                "fill_blank": {"mean_score": 0.9, "mean_difficulty": 0.5},
            },
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
            key="score_by_format",
            value={
                "flashcard": {"mean_score": 0.9, "mean_difficulty": 0.5},
                "mcq": {"mean_score": 0.5, "mean_difficulty": 0.5},
            },
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
            key="score_by_format",
            value={
                "flashcard": {"mean_score": 0.9, "mean_difficulty": 0.5},
                "mcq": {"mean_score": 0.5, "mean_difficulty": 0.5},
            },
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


# --- a review that keeps failing stops being self-rated (S09/S10) ------------------------


async def _review_due(session: AsyncSession, learner: Learner, kc: KC) -> None:
    """Put ``kc`` on the review queue, due now.

    Updates the state row rather than inserting one: recording an observation has already
    created it, and a second insert collides on the learner/KC unique constraint.
    """
    state = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id
        )
    )
    if state is None:
        state = LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=0.0)
        session.add(state)
    state.due_at = datetime.now(UTC) - timedelta(days=1)
    await session.flush()


async def _fail_review(session: AsyncSession, learner: Learner, kc: KC, score: float) -> None:
    await mastery.record_observation(
        session, Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=score)
    )
    await session.flush()


async def test_an_ordinary_due_review_is_still_a_flashcard(db_session: AsyncSession) -> None:
    learner, _subject, root, _dependent = await _graph(db_session)
    chosen = await svc.review_item_type(db_session, learner_id=learner.id, kc_id=root.id)
    assert chosen is ItemType.FLASHCARD


async def test_a_review_that_keeps_failing_is_served_as_an_open_question(
    db_session: AsyncSession,
) -> None:
    """A run of low self-ratings drives the estimate down and records nothing about why. The
    format changes at exactly the point the reason starts mattering more than the speed."""
    learner, _subject, root, _dependent = await _graph(db_session)
    await _fail_review(db_session, learner, root, 0.1)
    await _fail_review(db_session, learner, root, 0.2)

    chosen = await svc.review_item_type(db_session, learner_id=learner.id, kc_id=root.id)
    assert chosen is ItemType.SHORT


async def test_one_bad_review_is_not_enough_to_change_the_format(
    db_session: AsyncSession,
) -> None:
    """The same "not just a bad day" rule the detour trigger uses — one miss is noise."""
    learner, _subject, root, _dependent = await _graph(db_session)
    await _fail_review(db_session, learner, root, 0.1)

    chosen = await svc.review_item_type(db_session, learner_id=learner.id, kc_id=root.id)
    assert chosen is ItemType.FLASHCARD


async def test_a_recovered_component_goes_back_to_being_a_flashcard(
    db_session: AsyncSession,
) -> None:
    """Escalation follows the *current* run, not a lifetime tally: a learner who has since
    answered well is not still stuck, and should not keep paying for rubric grading."""
    learner, _subject, root, _dependent = await _graph(db_session)
    await _fail_review(db_session, learner, root, 0.1)
    await _fail_review(db_session, learner, root, 0.2)
    await _fail_review(db_session, learner, root, 1.0)

    chosen = await svc.review_item_type(db_session, learner_id=learner.id, kc_id=root.id)
    assert chosen is ItemType.FLASHCARD


async def test_the_due_review_queue_serves_the_escalated_format(
    db_session: AsyncSession,
) -> None:
    """End to end through the endpoint's own service, not just the predicate."""
    learner, _subject, root, _dependent = await _graph(db_session)
    await _fail_review(db_session, learner, root, 0.1)
    await _fail_review(db_session, learner, root, 0.2)
    # After the observations, not before: recording one reschedules the component, so a due
    # date set first is overwritten and the review never appears on the queue at all.
    await _review_due(db_session, learner, root)

    pairs = await svc.due_review_items(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, item_limit=5
    )
    resolved = [item for _review, item in pairs if item is not None]
    assert len(resolved) == 1
    assert resolved[0].item_type == ItemType.SHORT


# --- a revisit is a different question, even when the bank runs out (S14) --------------------


async def test_a_component_with_one_item_does_not_repeat_it(db_session: AsyncSession) -> None:
    """Exposure ordering makes a revisit different only while there is a spare question to be
    different. With one item it had nothing to choose between and re-asked the question the
    learner had just been told the answer to — which measures memory of that exchange, and
    still moved the estimate up."""
    learner, _subject, root, _dependent = await _graph(db_session)
    seen, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), root
    )
    assert seen is not None
    await _fail_review(db_session, learner, root, 1.0)
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=root.id,
            event_type="observation",
            payload={"item_id": str(seen.id), "score": 1.0},
        )
    )
    await db_session.flush()

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, kc=root
    )
    assert item is not None
    assert item.id != seen.id


async def test_an_unseen_bank_item_is_still_reused_for_free(db_session: AsyncSession) -> None:
    """Generation is triggered by exhaustion, not by every request — a spare question is a
    free win and paying to invent another would be waste."""
    learner, _subject, root, _dependent = await _graph(db_session)
    unseen, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), root
    )
    assert unseen is not None
    calls_before = len((await db_session.scalars(select(LLMCall))).all())

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client(SHORT_REPLY), learner_id=learner.id, kc=root
    )
    assert item is not None and item.id == unseen.id
    assert len((await db_session.scalars(select(LLMCall))).all()) == calls_before


async def test_a_repeated_question_beats_no_question_at_all(db_session: AsyncSession) -> None:
    """The last resort, and the reason it exists: a model that will not produce a parseable
    item must not end the session. Worse evidence is still evidence; nothing is not."""
    learner, _subject, root, _dependent = await _graph(db_session)
    seen, _ = await item_generation.generate_short_item(
        db_session, fake_llm_client(SHORT_REPLY), root
    )
    assert seen is not None
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=root.id,
            event_type="observation",
            payload={"item_id": str(seen.id), "score": 1.0},
        )
    )
    await db_session.flush()

    item = await svc.short_answer_item_for_kc(
        db_session, fake_llm_client("not json"), learner_id=learner.id, kc=root
    )
    assert item is not None and item.id == seen.id
