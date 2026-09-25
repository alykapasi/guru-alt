"""How a Jev answer is used — off and shadow (S81). Live is added in Task 6.

The property that matters most is asserted per consumer: **in shadow mode nothing the learner
sees can change**, whether Jev agrees, disagrees, times out or fails.
"""

import asyncio
import time
from collections.abc import Sequence

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning.conversation_evidence import TurnIntent
from app.learning.grading import GradeResult
from app.learning.turn_read import FULLY_CORRECT, INTENT, ReadContext
from app.llm.decisions import (
    ChoiceAnswer,
    DecisionFailure,
    FailureKind,
    FakeDecisionClient,
    YesNoAnswer,
)
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef, Usage
from app.models.decision import DecisionCall
from app.services import decisions
from tests.decision_support import runtime, using

NOBODY = ReadContext(learner_id=None, conversation_id=None, item_id=None)
SMART_GRADE = GradeResult(
    score=0.4, correct=False, detail={"rationale": "half", "method": "rubric"}
)


class _CountingProvider(FakeProvider):
    """A FAST gate that answers `reply` and counts how often it was asked."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply=reply)
        self.calls = 0

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            content=self._reply, model=model, usage=Usage(input_tokens=1, output_tokens=1)
        )


def _fast(intent: str) -> tuple[LLMClient, _CountingProvider]:
    provider = _CountingProvider(f'{{"intent": "{intent}"}}')
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "m") for r in ModelRole}), provider


def _intent(label: str, confidence: float) -> ChoiceAnswer:
    return ChoiceAnswer(label=label, probabilities={label: confidence}, confidence=confidence)


class _Smart:
    """Stands in for the SMART grader and counts calls to it."""

    def __init__(self, result: GradeResult = SMART_GRADE) -> None:
        self.result = result
        self.calls = 0

    async def __call__(self) -> GradeResult:
        self.calls += 1
        return self.result


JEV_BEHAVIOURS = {
    "agrees": FakeDecisionClient({INTENT: _intent("deferral", 0.99)}),
    "disagrees": FakeDecisionClient({INTENT: _intent("attempt", 0.99)}),
    "times out": FakeDecisionClient(failure=DecisionFailure(FailureKind.TIMEOUT)),
    "fails": FakeDecisionClient(failure=DecisionFailure(FailureKind.SERVER)),
}


# --- off -----------------------------------------------------------------------------------


async def test_off_makes_no_request_and_the_fast_gate_decides() -> None:
    fake = FakeDecisionClient({INTENT: _intent("attempt", 0.99)})
    llm, fast = _fast("deferral")

    with using(runtime(fake)):
        intent = await decisions.decide_intent(llm, question="q", message="m", context=NOBODY)

    assert intent is TurnIntent.DEFERRAL
    assert fake.requests == []
    assert fast.calls == 1


async def test_off_grading_makes_no_request_and_smart_decides() -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)})
    smart = _Smart()

    with using(runtime(fake)):
        result = await decisions.decide_grade(
            smart=smart,
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=NOBODY,
            attempt_id=None,
        )

    assert result == SMART_GRADE
    assert fake.requests == []
    assert smart.calls == 1


# --- shadow never changes the outcome --------------------------------------------------------


@pytest.mark.parametrize("behaviour", list(JEV_BEHAVIOURS))
async def test_shadow_intent_never_changes_what_the_gate_decides(behaviour: str) -> None:
    fake = JEV_BEHAVIOURS[behaviour]
    fake.requests.clear()
    llm, fast = _fast("deferral")

    with using(runtime(fake, intent="shadow")):
        intent = await decisions.decide_intent(llm, question="q", message="m", context=NOBODY)

    assert intent is TurnIntent.DEFERRAL
    assert fast.calls == 1
    assert len(fake.requests) == 1


@pytest.mark.parametrize(
    "fake",
    [
        FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)}),
        FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.01)}),
        FakeDecisionClient(failure=DecisionFailure(FailureKind.TIMEOUT)),
        FakeDecisionClient(failure=DecisionFailure(FailureKind.AUTH)),
    ],
    ids=["confident yes", "confident no", "times out", "fails"],
)
async def test_shadow_grading_never_changes_the_grade(fake: FakeDecisionClient) -> None:
    smart = _Smart()

    with using(runtime(fake, fully_correct="shadow")):
        result = await decisions.decide_grade(
            smart=smart,
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=NOBODY,
            attempt_id=None,
        )

    assert result == SMART_GRADE
    assert smart.calls == 1


async def test_shadow_records_jev_beside_the_fast_decision(db_session: AsyncSession) -> None:
    llm, _ = _fast("deferral")

    with using(runtime(FakeDecisionClient({INTENT: _intent("attempt", 0.97)}), intent="shadow")):
        await decisions.decide_intent(llm, question="q", message="m", context=NOBODY)

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert (row.question, row.mode, row.answer, row.baseline_intent, row.used) == (
        "intent",
        "shadow",
        "attempt",
        "deferral",
        False,
    )


async def test_shadow_records_jev_beside_the_smart_score(db_session: AsyncSession) -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.95)})

    with using(runtime(fake, fully_correct="shadow")):
        await decisions.decide_grade(
            smart=_Smart(),
            stem="q",
            answer="a",
            rubric_criteria=None,
            context=NOBODY,
            attempt_id=None,
        )

    row = (await db_session.scalars(select(DecisionCall))).one()
    assert row.question == "fully_correct"
    assert row.probabilities == {"yes": pytest.approx(0.95)}
    assert row.baseline_score == pytest.approx(0.4)


# --- shared reads and edge inputs ------------------------------------------------------------


async def test_a_shared_read_is_one_request_for_both_questions() -> None:
    fake = FakeDecisionClient(
        {INTENT: _intent("attempt", 0.9), FULLY_CORRECT: YesNoAnswer(probability=0.9)}
    )
    llm, _ = _fast("attempt")

    with using(runtime(fake, intent="shadow", fully_correct="shadow")):
        read = decisions.start_turn_read(
            questions=[INTENT, FULLY_CORRECT],
            stem="q",
            message="m",
            rubric_criteria=None,
            context=NOBODY,
        )
        await decisions.decide_intent(llm, question="q", message="m", context=NOBODY, read=read)
        await decisions.decide_grade(
            smart=_Smart(),
            stem="q",
            answer="m",
            rubric_criteria=None,
            context=NOBODY,
            attempt_id=None,
            read=read,
        )

    assert len(fake.requests) == 1


async def test_a_read_leaves_out_questions_that_are_off() -> None:
    fake = FakeDecisionClient({})

    with using(runtime(fake, intent="shadow")):
        read = decisions.start_turn_read(
            questions=[INTENT, FULLY_CORRECT],
            stem="q",
            message="m",
            rubric_criteria=None,
            context=NOBODY,
        )

    assert read is not None and read.asks(INTENT) and not read.asks(FULLY_CORRECT)


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
async def test_an_empty_reply_asks_jev_nothing(message: str) -> None:
    """Review focus 3: the FAST gate answers an empty reply for free; so must this."""
    fake = FakeDecisionClient({INTENT: _intent("attempt", 0.99)})
    llm, fast = _fast("attempt")

    with using(runtime(fake, intent="shadow")):
        intent = await decisions.decide_intent(llm, question="q", message=message, context=NOBODY)

    assert intent is TurnIntent.DEFERRAL
    assert fake.requests == []
    assert fast.calls == 0


async def test_an_empty_answer_is_graded_without_asking_jev() -> None:
    fake = FakeDecisionClient({FULLY_CORRECT: YesNoAnswer(probability=0.99)})

    with using(runtime(fake, fully_correct="shadow")):
        await decisions.decide_grade(
            smart=_Smart(),
            stem="q",
            answer="  ",
            rubric_criteria=None,
            context=NOBODY,
            attempt_id=None,
        )

    assert fake.requests == []


# --- the shadow write never delays the turn ---------------------------------------------------


async def test_a_slow_shadow_request_does_not_delay_the_gate() -> None:
    fake = FakeDecisionClient({INTENT: _intent("attempt", 0.9)}, delay_s=0.5)
    llm, _ = _fast("deferral")

    with using(runtime(fake, intent="shadow", background=True)):
        started = time.perf_counter()
        intent = await decisions.decide_intent(llm, question="q", message="m", context=NOBODY)
        elapsed = time.perf_counter() - started
        await decisions.drain()

    assert intent is TurnIntent.DEFERRAL
    assert elapsed < 0.3


async def test_drain_waits_for_every_pending_shadow_write() -> None:
    fake = FakeDecisionClient({INTENT: _intent("attempt", 0.9)}, delay_s=0.05)
    llm, _ = _fast("deferral")

    with using(runtime(fake, intent="shadow", background=True)):
        await decisions.decide_intent(llm, question="q", message="m", context=NOBODY)
        await decisions.drain()

    assert decisions._pending == set()
    await asyncio.sleep(0)  # nothing left scheduled to run
