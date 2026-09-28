"""Re-grading measures a grader against past answers and changes nothing (S56)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import rubric_grading
from app.llm import LLMClient
from app.llm.meter import BudgetExceeded
from app.llm.providers import FakeProvider
from app.llm.registry import ModelSpec, fake_llm_client
from app.llm.types import ModelRole
from app.models.assessment import ItemType
from app.models.grading import GradingSnapshot
from app.models.learning import LearnerKCState, LearningEvent
from app.schemas.assessment import AnswerSubmit
from app.services import assessment as svc
from app.services import regrade
from tests.test_grading_provenance import _setup

NOW = datetime.now(UTC).replace(tzinfo=None)


def _grade(score: float) -> str:
    return json.dumps({"score": score, "rationale": "r"})


async def _graded(session: AsyncSession, score: float = 1.0, **setup):
    learner, kc, item = await _setup(session, **setup)
    await svc.answer_item(
        session,
        learner.id,
        item,
        AnswerSubmit(response={"text": "tip to tail"}),
        llm=fake_llm_client(_grade(score)),
    )
    return learner, kc, item


async def _plan(session: AsyncSession, learner_id: uuid.UUID) -> regrade.Plan:
    return await regrade.plan(
        session,
        since=NOW - timedelta(days=1),
        until=NOW + timedelta(days=1),
        learner_id=learner_id,
    )


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient(
        {"fake": provider}, {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    )


def _systems(provider: FakeProvider) -> list[str | None]:
    return [system for system, _messages in provider.prompts_sent]


async def _ability(session: AsyncSession, learner_id: uuid.UUID) -> float:
    state = await session.scalar(
        select(LearnerKCState).where(LearnerKCState.learner_id == learner_id)
    )
    assert state is not None
    await session.refresh(state)
    return state.ability


async def test_planning_finds_graded_attempts_and_calls_nothing(db_session) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await _plan(db_session, learner.id)
    assert len(found.candidates) == 1 and found.by_grader == {"rubric": 1}
    assert found.not_regradable == 0


async def test_old_events_and_missing_snapshots_are_not_regradable(db_session) -> None:
    learner, kc, _item = await _graded(db_session)
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            kc_id=kc.id,
            event_type="observation",
            attempt_id=uuid.uuid4(),
            payload={"score": 1.0, "response": {"text": "x"}},
        )
    )
    await db_session.flush()
    found = await _plan(db_session, learner.id)
    assert (len(found.candidates), found.not_regradable) == (1, 1)

    await db_session.execute(
        delete(GradingSnapshot).where(GradingSnapshot.learner_id == learner.id)
    )
    await db_session.flush()
    found = await _plan(db_session, learner.id)
    assert (len(found.candidates), found.not_regradable) == (0, 2)


async def test_a_disagreeing_grader_is_reported_and_nothing_changes(db_session) -> None:
    learner, _kc, _item = await _graded(db_session, score=1.0)
    before = await _ability(db_session, learner.id)
    found = await _plan(db_session, learner.id)

    report = await regrade.run(fake_llm_client(_grade(0.0)), found)

    assert (report.compared, report.failed) == (1, 0)
    assert report.correct_agreement == 0.0
    assert report.mean_abs_score_diff == pytest.approx(1.0)
    assert [(c.recorded, c.regraded) for c in report.largest] == [(1.0, 0.0)]
    assert await _ability(db_session, learner.id) == before
    [event] = (
        await db_session.scalars(
            select(LearningEvent).where(LearningEvent.learner_id == learner.id)
        )
    ).all()
    assert event.payload["score"] == 1.0


async def test_the_recorded_prompt_is_sent_when_asked(db_session, monkeypatch) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await _plan(db_session, learner.id)
    monkeypatch.setattr(rubric_grading, "_SYSTEM_PROMPT", "TODAY'S PROMPT")

    recorded = FakeProvider(reply=_grade(1.0))
    await regrade.run(_client(recorded), found, prompt="recorded")
    current = FakeProvider(reply=_grade(1.0))
    await regrade.run(_client(current), found, prompt="current")

    [sent] = _systems(recorded)
    snapshot = found.candidates[0].prompt
    assert snapshot is not None
    assert sent != "TODAY'S PROMPT" and sent == snapshot["system"]
    assert _systems(current) == ["TODAY'S PROMPT"]


async def test_the_rubric_reaches_only_its_own_component(db_session) -> None:
    """Review focus 2."""
    learner, kc, _item = await _graded(db_session, rubric={"points": ["tip to tail"]})
    found = await _plan(db_session, learner.id)
    [candidate] = found.candidates
    assert candidate.rubric is not None
    assert candidate.rubric["criteria"] == {"points": ["tip to tail"]}
    assert candidate.rubric["components"] == [
        {"kc_id": str(kc.id), "criteria": {"points": ["tip to tail"]}}
    ]


async def test_an_empty_answer_regrades_to_zero_without_a_call(db_session) -> None:
    """Review focus 3."""
    learner, _kc, item = await _setup(db_session)
    await svc.answer_item(
        db_session,
        learner.id,
        item,
        AnswerSubmit(response={"text": ""}),
        llm=fake_llm_client(_grade(1.0)),
    )
    found = await _plan(db_session, learner.id)
    provider = FakeProvider(reply=_grade(1.0))
    report = await regrade.run(_client(provider), found)
    assert provider.prompts_sent == [] and report.mean_abs_score_diff == 0.0


async def test_a_refused_regrade_is_counted_failed(db_session) -> None:
    """Review focus 5."""

    class Refusing(FakeProvider):
        async def complete(self, **kwargs):
            raise BudgetExceeded("deployment")

    learner, _kc, _item = await _graded(db_session)
    found = await _plan(db_session, learner.id)
    report = await regrade.run(_client(Refusing()), found)
    assert (report.compared, report.failed) == (0, 1)


async def test_an_auto_grade_is_regraded_free(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.MCQ)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"choice": 1}), llm=fake_llm_client()
    )
    found = await _plan(db_session, learner.id)
    provider = FakeProvider(reply="unused")
    report = await regrade.run(_client(provider), found)
    assert provider.prompts_sent == [] and report.correct_agreement == 1.0


async def test_a_self_rating_is_not_regradable(db_session) -> None:
    learner, _kc, item = await _setup(db_session, item_type=ItemType.FLASHCARD)
    await svc.answer_item(
        db_session, learner.id, item, AnswerSubmit(response={"rating": 3}), llm=fake_llm_client()
    )
    found = await _plan(db_session, learner.id)
    assert (len(found.candidates), found.not_regradable) == (0, 0)


async def test_the_report_holds_no_learner_text(db_session) -> None:
    learner, _kc, _item = await _graded(db_session)
    found = await _plan(db_session, learner.id)
    report = await regrade.run(fake_llm_client(_grade(0.0)), found)
    assert "tip to tail" not in regrade.render(found, report, None)
    assert "tip to tail" not in json.dumps(regrade.as_json(report), default=str)
