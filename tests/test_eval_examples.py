"""Drawing, freezing and labelling evaluation examples (S59)."""

import uuid

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, Message
from app.models.evaluation import EvalExample, IntentRecord
from app.models.grading import GradingSnapshot
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import eval_examples as svc
from app.services.eval_areas import Frozen, grading_band


async def _learner(session: AsyncSession, *, eligible: bool = True) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", eval_eligible=eligible)
    session.add(learner)
    await session.flush()
    return learner


async def _graded(
    session: AsyncSession,
    learner: Learner,
    score: float,
    *,
    diagnosis: str | None = None,
    answer: str = "An answer.",
) -> LearningEvent:
    sha = uuid.uuid4().hex
    session.add(
        GradingSnapshot(
            learner_id=learner.id,
            sha256=sha,
            kind="item",
            content={"stem": "Explain osmosis.", "item_type": "short", "components": []},
        )
    )
    event = LearningEvent(
        learner_id=learner.id,
        event_type="observation",
        attempt_id=uuid.uuid4(),
        payload={
            "score": score,
            "item_score": score,
            "correct": score >= 0.8,
            "response": {"text": answer},
            "grading": {"grader": "rubric", "item": sha, "rubric": None, "model": "m-1"},
            "diagnosis": {"kind": diagnosis, "confidence": 0.7, "evidence": "x"}
            if diagnosis
            else None,
        },
    )
    session.add(event)
    await session.flush()
    return event


async def test_only_eligible_accounts_are_drawn(db_session: AsyncSession) -> None:
    yes, no = await _learner(db_session), await _learner(db_session, eligible=False)
    await _graded(db_session, yes, 0.9)
    await _graded(db_session, no, 0.9)
    assert await svc.draw(db_session, "grading") == 1
    example = await svc.next_unlabelled(db_session, "grading")
    assert example is not None and example.learner_id == yes.id


async def test_a_source_is_drawn_once(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.2)
    assert await svc.draw(db_session, "grading") == 1
    assert await svc.draw(db_session, "grading") == 0


async def test_drawing_past_the_end_returns_what_there_is(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    for score in (0.1, 0.5, 0.9):
        await _graded(db_session, learner, score)
    assert await svc.draw(db_session, "grading", limit=10) == 3


async def test_the_copy_is_frozen(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    event = await _graded(db_session, learner, 0.9, answer="Water crosses a membrane.")
    await svc.draw(db_session, "grading")
    await db_session.execute(
        update(LearningEvent)
        .where(LearningEvent.id == event.id)
        .values(payload={**event.payload, "response": {"text": "Edited later."}})
    )
    example = await svc.next_unlabelled(db_session, "grading")
    assert example is not None
    assert example.snapshot["answer"] == "Water crosses a membrane."


async def test_a_deleted_source_leaves_the_example_readable(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="Why?"))
    reply = Message(
        conversation_id=conversation.id,
        role="assistant",
        content="Because.",
        grounding_count=2,
        citations=[],
    )
    db_session.add(reply)
    await db_session.flush()
    assert await svc.draw(db_session, "source_support") == 1
    await db_session.execute(delete(Message).where(Message.id == reply.id))
    example = await svc.next_unlabelled(db_session, "source_support")
    assert example is not None
    assert example.snapshot["reply"] == "Because."
    assert example.snapshot["question"] == "Why?"
    assert example.decision == {"coverage": "retrieved_not_cited", "offered": 2}


async def test_source_support_is_judged_on_support_and_hides_the_rules_inputs(
    db_session: AsyncSession,
) -> None:
    # Showing how many passages were offered beside the cited ones would let the labeller read
    # the coverage label off the screen; the founder judges whether the reply is supported.
    learner = await _learner(db_session)
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="Why?"))
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="Because [1].",
            grounding_count=3,
            citations=[{"marker": "1", "chunk_id": None}],
        )
    )
    await db_session.flush()
    await svc.draw(db_session, "source_support")
    example = await svc.next_unlabelled(db_session, "source_support")
    assert example is not None
    assert "offered" not in example.snapshot
    assert example.decision == {"coverage": "cited", "offered": 3}
    saved = await svc.save_label(
        db_session, example.id, label={"support": "unsupported"}, note=None, by=learner.id
    )
    assert saved.label == {"support": "unsupported"}
    with pytest.raises(ValueError):
        await svc.save_label(
            db_session, example.id, label={"coverage": "cited"}, note=None, by=learner.id
        )


async def test_revoking_keeps_existing_examples_and_stops_new_ones(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.9)
    await svc.draw(db_session, "grading")
    learner.eval_eligible = False
    await _graded(db_session, learner, 0.2)
    await db_session.flush()
    assert await svc.draw(db_session, "grading") == 0
    assert await svc.next_unlabelled(db_session, "grading") is not None


async def test_grading_draws_spread_across_bands(db_session: AsyncSession) -> None:
    # Eight fails and one pass: an unstratified draw of two almost never includes the pass.
    learner = await _learner(db_session)
    for _ in range(8):
        await _graded(db_session, learner, 0.1)
    await _graded(db_session, learner, 0.95)
    assert await svc.draw(db_session, "grading", limit=2) == 2
    bands = sorted(
        e.decision["band"] for e in (await db_session.scalars(svc_select_all("grading"))).all()
    )
    assert bands == ["fail", "pass"]


def test_stratifying_takes_one_from_each_band_in_turn() -> None:
    def frozen(band: str) -> Frozen:
        return Frozen(uuid.uuid4(), "attempt", uuid.uuid4(), {}, {"band": band}, band)

    picked = svc._stratified([frozen("fail")] * 3 + [frozen("pass")] + [frozen("partial")], 3)
    assert sorted(p.stratum or "" for p in picked) == ["fail", "partial", "pass"]


def svc_select_all(area: str):
    from sqlalchemy import select

    return select(EvalExample).where(EvalExample.area == area)


async def test_diagnosis_draws_only_failed_components(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.2, diagnosis="conceptual")
    await _graded(db_session, learner, 0.9, diagnosis="none")
    assert await svc.draw(db_session, "diagnosis") == 1
    example = await svc.next_unlabelled(db_session, "diagnosis")
    assert example is not None and example.decision["kind"] == "conceptual"


async def test_intent_draws_recorded_decisions(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    db_session.add(IntentRecord(learner_id=learner.id, stem="Q?", message="Hm", intent="deferral"))
    await db_session.flush()
    assert await svc.draw(db_session, "intent") == 1
    example = await svc.next_unlabelled(db_session, "intent")
    assert example is not None
    assert example.snapshot == {"stem": "Q?", "message": "Hm"}
    assert example.decision == {"intent": "deferral"}


async def test_a_label_outside_the_vocabulary_is_refused(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.9)
    await svc.draw(db_session, "grading")
    example = await svc.next_unlabelled(db_session, "grading")
    assert example is not None
    with pytest.raises(ValueError):
        await svc.save_label(
            db_session, example.id, label={"band": "great"}, note=None, by=learner.id
        )
    with pytest.raises(ValueError):
        await svc.save_label(
            db_session, example.id, label={"kind": "pass"}, note=None, by=learner.id
        )


async def test_a_label_is_saved_with_who_and_when(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.9)
    await svc.draw(db_session, "grading")
    example = await svc.next_unlabelled(db_session, "grading")
    assert example is not None
    saved = await svc.save_label(
        db_session, example.id, label={"band": "partial"}, note=" close ", by=learner.id
    )
    assert saved.label == {"band": "partial"}
    assert saved.labelled_by_id == learner.id and saved.labelled_at is not None
    assert await svc.next_unlabelled(db_session, "grading") is None
    assert [e.id for e in await svc.labelled(db_session, "grading")] == [example.id]


async def test_skipped_examples_are_passed_over(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _graded(db_session, learner, 0.1)
    await _graded(db_session, learner, 0.9)
    await svc.draw(db_session, "grading")
    first = await svc.next_unlabelled(db_session, "grading")
    assert first is not None
    second = await svc.next_unlabelled(db_session, "grading", skip=[first.id])
    assert second is not None and second.id != first.id


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0.0, "fail"),
        (0.39, "fail"),
        (0.4, "partial"),
        (0.79, "partial"),
        (0.8, "pass"),
        (1.0, "pass"),
    ],
)
def test_grading_band_edges(score: float, band: str) -> None:
    assert grading_band(score) == band


async def test_counts(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    for score in (0.1, 0.9, 0.5):
        await _graded(db_session, learner, score)
    await svc.draw(db_session, "grading", limit=2)
    counts = await svc.counts(db_session)
    assert counts["grading"]["unlabelled"] == 2
    assert counts["grading"]["labelled"] == 0
    assert counts["grading"]["available"] >= 1
    assert set(counts) == {"grading", "diagnosis", "intent", "source_support"}


async def test_an_unknown_area_is_refused(db_session: AsyncSession) -> None:
    with pytest.raises(svc.UnknownArea):
        await svc.draw(db_session, "vibes")
