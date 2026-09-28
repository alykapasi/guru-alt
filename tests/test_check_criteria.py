"""A conversational check gets a standard and a level before it is graded (S56)."""

import json
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import declared_check, difficulty, item_generation, mastery
from app.learning.tracer import Estimate
from app.llm.meter import BudgetExceeded
from app.llm.providers import FakeProvider
from app.llm.providers.fake import FakeTurn
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole
from app.models.chat import Conversation, ConversationPhase
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import chat as chat_svc

ATTEMPT = '{"intent": "attempt"}'
DEFERRAL = '{"intent": "deferral"}'
GRADE = '{"score": 0.9, "rationale": "ok"}'
ANSWER = "Because it has a direction as well as a size."
CRITERIA = json.dumps({"criteria": ["names both parts", "gives a reason"], "level": "challenging"})


def _client(*replies: str) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(reply="", script=[FakeTurn(text=r) for r in replies])
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    return LLMClient({"fake": provider}, specs), provider


@pytest.mark.parametrize("level", [name for name, _gloss in difficulty.LEVELS])
def test_each_level_has_a_midpoint_inside_its_own_band(level: str) -> None:
    assert difficulty.band(difficulty.midpoint(level)) == level


def test_the_midpoints_are_one_logit_apart() -> None:
    assert [difficulty.midpoint(n) for n, _ in difficulty.LEVELS] == [-2.0, -1.0, 0.0, 1.0, 2.0]


def test_an_unknown_level_has_no_midpoint() -> None:
    with pytest.raises(ValueError):
        difficulty.midpoint("impossible")


async def test_criteria_and_level_are_read_from_the_reply() -> None:
    llm, provider = _client(CRITERIA)
    criteria, level, _ = await item_generation.write_criteria(
        llm, stem="Why is momentum a vector?", component_name="Momentum"
    )
    assert criteria == ["names both parts", "gives a reason"]
    assert level == "challenging"
    [(system, messages)] = provider.prompts_sent
    assert system == item_generation.CRITERIA_SYSTEM_PROMPT
    assert "Why is momentum a vector?" in str(messages[0].content)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("not json", ([], None)),
        (json.dumps({"criteria": ["a"], "level": "impossible"}), (["a"], None)),
        (json.dumps({"criteria": [" ", "b"], "level": " Moderate "}), (["b"], "moderate")),
        (json.dumps({"level": "moderate"}), ([], "moderate")),
    ],
)
async def test_a_partial_reply_keeps_what_it_can(reply: str, expected: tuple) -> None:
    llm, _ = _client(reply)
    criteria, level, _ = await item_generation.write_criteria(llm, stem="Q", component_name="K")
    assert (criteria, level) == expected


# --- a check gets its criteria on its first attempt ------------------------------------------


async def _declared(session: AsyncSession):
    learner = Learner(handle=f"cc-{uuid.uuid4().hex[:8]}")
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    session.add_all([learner, subject])
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug="momentum", name="Momentum", description="mass times velocity")
    session.add(kc)
    await session.flush()
    item = await chat_svc._materialise_declared_check(
        session,
        learner_id=learner.id,
        subject_id=subject.id,
        declared=declared_check.DeclaredCheck(
            component="Momentum", question="Why is momentum a vector?"
        ),
    )
    assert item is not None and item.rubric_id is None
    conversation = Conversation(
        learner_id=learner.id,
        subject_id=subject.id,
        phase=ConversationPhase.AWAITING_ANSWER,
        active_item_id=item.id,
    )
    session.add(conversation)
    await session.commit()
    return learner, kc, conversation, item


async def _answer(session, llm, learner, conversation, text=ANSWER, attempt_id=None):
    return await chat_svc._resolve_check(
        session,
        llm,
        learner_id=learner.id,
        conversation=conversation,
        user_content=text,
        attempt_id=attempt_id,
    )


def _criteria_calls(provider: FakeProvider) -> list:
    return [m for s, m in provider.prompts_sent if s == item_generation.CRITERIA_SYSTEM_PROMPT]


async def _events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    return list(
        (
            await session.scalars(
                select(LearningEvent).where(LearningEvent.learner_id == learner_id)
            )
        ).all()
    )


def _specs() -> dict:
    return {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}


async def test_the_first_attempt_writes_criteria_and_a_rated_level(db_session) -> None:
    learner, kc, conversation, item = await _declared(db_session)
    llm, provider = _client(ATTEMPT, CRITERIA, GRADE)

    still_open, outcome = await _answer(db_session, llm, learner, conversation)

    assert still_open is None and outcome is not None
    assert item.rubric is not None and item.rubric.kc_id == kc.id
    assert item.rubric.criteria == {"criteria": ["names both parts", "gives a reason"]}
    assert item.rubric.owner_learner_id == learner.id
    assert item.difficulty == 1.0
    [event] = await _events(db_session, learner.id)
    assert event.payload["difficulty"] == 1.0
    assert event.payload["grading"]["rubric"] is not None
    assert len(_criteria_calls(provider)) == 1


async def test_the_criteria_call_never_sees_the_answer(db_session) -> None:
    learner, _kc, conversation, _item = await _declared(db_session)
    llm, provider = _client(ATTEMPT, CRITERIA, GRADE)
    await _answer(db_session, llm, learner, conversation)
    [messages] = _criteria_calls(provider)
    assert all(ANSWER not in str(m.content) for m in messages)


async def test_a_retried_attempt_reuses_the_criteria(db_session) -> None:
    """Review focus 2: a retried turn with the same attempt id."""
    learner, _kc, conversation, item = await _declared(db_session)
    attempt = uuid.uuid4()
    await _answer(
        db_session, _client(ATTEMPT, CRITERIA, GRADE)[0], learner, conversation, attempt_id=attempt
    )
    rubric_id = item.rubric_id

    llm, provider = _client(ATTEMPT, GRADE)
    _open, outcome = await _answer(db_session, llm, learner, conversation, attempt_id=attempt)

    assert outcome is not None and outcome.result.score == 0.9
    assert _criteria_calls(provider) == [] and item.rubric_id == rubric_id
    assert len(await _events(db_session, learner.id)) == 1


async def test_a_deferral_writes_no_criteria(db_session) -> None:
    learner, _kc, conversation, item = await _declared(db_session)
    llm, provider = _client(DEFERRAL)
    still_open, outcome = await _answer(db_session, llm, learner, conversation, text="hint?")
    assert still_open is not None and outcome is None
    assert _criteria_calls(provider) == [] and item.rubric_id is None


async def test_an_unusable_reply_grades_the_old_way(db_session) -> None:
    learner, _kc, conversation, item = await _declared(db_session)
    llm, _provider = _client(ATTEMPT, "not json", GRADE)
    _open, outcome = await _answer(db_session, llm, learner, conversation)
    assert outcome is not None
    assert item.rubric_id is None and item.difficulty == 0.0


async def test_an_unknown_level_keeps_the_criteria_only(db_session) -> None:
    """Review focus 4."""
    learner, _kc, conversation, item = await _declared(db_session)
    reply = json.dumps({"criteria": ["a"], "level": "impossible"})
    _open, outcome = await _answer(
        db_session, _client(ATTEMPT, reply, GRADE)[0], learner, conversation
    )
    assert outcome is not None and item.rubric is not None and item.difficulty == 0.0


async def test_a_check_with_criteria_is_left_alone(db_session) -> None:
    """Review focus 5."""
    learner, _kc, conversation, item = await _declared(db_session)
    await _answer(db_session, _client(ATTEMPT, CRITERIA, GRADE)[0], learner, conversation)
    before = (item.rubric_id, item.difficulty)
    llm, provider = _client(ATTEMPT, GRADE)
    await _answer(db_session, llm, learner, conversation)
    assert _criteria_calls(provider) == [] and (item.rubric_id, item.difficulty) == before


class _Refusing(FakeProvider):
    async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
        if system == item_generation.CRITERIA_SYSTEM_PROMPT:
            raise BudgetExceeded("learner")
        return await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )


class _Failing(FakeProvider):
    async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
        if system == item_generation.CRITERIA_SYSTEM_PROMPT:
            raise RuntimeError("provider down")
        return await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )


async def test_a_refused_criteria_call_records_nothing(db_session) -> None:
    """Review focus 3."""
    learner, _kc, conversation, item = await _declared(db_session)
    llm = LLMClient({"fake": _Refusing(reply=ATTEMPT)}, _specs())
    still_open, outcome = await _answer(db_session, llm, learner, conversation)
    assert still_open is not None and outcome is None
    assert item.rubric_id is None and await _events(db_session, learner.id) == []


async def test_a_failed_criteria_call_still_grades(db_session) -> None:
    learner, _kc, conversation, item = await _declared(db_session)
    provider = _Failing(reply="", script=[FakeTurn(text=ATTEMPT), FakeTurn(text=GRADE)])
    _open, outcome = await _answer(
        db_session, LLMClient({"fake": provider}, _specs()), learner, conversation
    )
    assert outcome is not None and item.rubric_id is None


# --- aiming the tutor ------------------------------------------------------------------------


def test_the_instruction_names_the_level() -> None:
    note = declared_check.instruction("moderate (two or three steps)")
    assert note.startswith(declared_check.INSTRUCTION)
    assert note.endswith("Pitch any check at this level: moderate (two or three steps).")


@pytest.mark.parametrize(("ability", "level"), [(-3.0, "introductory"), (3.0, "demanding")])
async def test_the_level_follows_the_learners_subject_estimate(
    db_session, monkeypatch, ability: float, level: str
) -> None:
    async def rollup(*_args, **_kwargs) -> Estimate:
        return Estimate(ability=ability, uncertainty=0.5)

    monkeypatch.setattr(mastery, "rollup_subject", rollup)
    note = await chat_svc._declared_check_note(
        db_session, learner_id=uuid.uuid4(), subject_id=uuid.uuid4()
    )
    assert f"Pitch any check at this level: {level} (" in note
