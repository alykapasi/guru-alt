"""How a Jev answer is used — the per-question off / shadow / live switch (S81, S83).

Two consumers ask: the answer-intent gate (:func:`decide_intent`, in front of the FAST
classifier) and the rubric grader (:func:`decide_grade`, in front of the SMART grader).

- **off** — nothing is asked; today's path runs exactly as before.
- **shadow** — Jev is asked, today's path decides, and both are recorded side by side. The
  record is written in the background: nobody waits on it and it never touches the turn's
  transaction.
- **live** — a confident answer decides and today's model call is skipped. "Confident" is the
  question's threshold (Choice confidence for ``intent``, P(yes) for ``fully_correct``);
  anything less, a failure, or no answer by the live deadline, and today's path runs as in
  shadow mode. Jev never fails an answer: only a confident *pass* skips the grader.

Every question switches on its own, so a question that disagrees too often can be turned off
without touching the other. Operating it: ``docs/RUNBOOK.md`` §14.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any

from app.core.config import DecisionMode, Settings, get_settings
from app.learning import conversation_evidence, turn_read
from app.learning.conversation_evidence import TurnIntent
from app.learning.grading import GradeResult
from app.learning.turn_read import FULLY_CORRECT, INTENT, ReadContext, TurnRead
from app.llm import LLMClient
from app.llm.decisions import (
    Answer,
    ChoiceAnswer,
    DecisionClient,
    DecisionFailure,
    YesNoAnswer,
    build_decision_client,
)
from app.services.decision_log import record_decision
from app.services.llm_log import log_llm_call


@dataclass(frozen=True)
class DecisionPolicy:
    intent_mode: DecisionMode
    fully_correct_mode: DecisionMode
    intent_threshold: float
    fully_correct_threshold: float
    live_deadline_s: float
    shadow_timeout_s: float
    # Shadow rows are written in the background in production. Tests write them inline: a
    # background write would share the test's database connection with the code under test.
    background: bool = True

    def mode(self, name: str) -> DecisionMode:
        return {INTENT: self.intent_mode, FULLY_CORRECT: self.fully_correct_mode}[name]

    def threshold(self, name: str) -> float:
        return {INTENT: self.intent_threshold, FULLY_CORRECT: self.fully_correct_threshold}[name]

    @classmethod
    def off(cls) -> DecisionPolicy:
        return cls("off", "off", 0.9, 0.9, 0.8, 5.0)

    @classmethod
    def from_settings(cls, settings: Settings) -> DecisionPolicy:
        return cls(
            intent_mode=settings.decision_intent_mode,
            fully_correct_mode=settings.decision_fully_correct_mode,
            intent_threshold=settings.decision_intent_threshold,
            fully_correct_threshold=settings.decision_fully_correct_threshold,
            live_deadline_s=settings.decision_live_deadline_ms / 1000,
            shadow_timeout_s=settings.decision_shadow_timeout_s,
        )


@dataclass(frozen=True)
class DecisionRuntime:
    client: DecisionClient | None
    policy: DecisionPolicy


_runtime: DecisionRuntime | None = None
_pending: set[asyncio.Task[None]] = set()


def get_runtime() -> DecisionRuntime:
    """The process-wide runtime, built from settings on first use.

    Building it validates the settings, which is why startup calls this: a question switched on
    with no key fails the boot, not a learner's turn.
    """
    global _runtime
    if _runtime is None:
        settings = get_settings()
        _runtime = DecisionRuntime(
            client=build_decision_client(settings), policy=DecisionPolicy.from_settings(settings)
        )
    return _runtime


def set_runtime(runtime: DecisionRuntime | None) -> DecisionRuntime | None:
    """Swap the runtime (tests). Returns the previous one."""
    global _runtime
    previous = _runtime
    _runtime = runtime
    return previous


def start_turn_read(
    *,
    questions: Sequence[str],
    stem: str,
    message: str,
    rubric_criteria: dict | None,
    context: ReadContext,
) -> TurnRead | None:
    """Start one request for whichever of ``questions`` are switched on, or ``None``.

    Nothing is asked about an empty reply: the FAST gate answers one for free, and so does this.
    The request's own timeout is the shadow timeout even when a question is live — a live
    question stops *waiting* at its deadline, but the request carries on for any shadow
    question sharing it.
    """
    runtime = get_runtime()
    wanted = [name for name in questions if runtime.policy.mode(name) != "off"]
    if runtime.client is None or not wanted or not message.strip():
        return None
    return turn_read.start_read(
        runtime.client,
        questions=wanted,
        stem=stem,
        message=message,
        rubric_criteria=rubric_criteria,
        context=context,
        timeout_s=runtime.policy.shadow_timeout_s,
    )


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for every background shadow write (shutdown, and tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


async def _settle(
    read: TurnRead,
    name: str,
    *,
    mode: DecisionMode,
    used: bool,
    answer: Answer | DecisionFailure | None = None,
    baseline_intent: str | None = None,
    baseline_score: float | None = None,
    attempt_id: uuid.UUID | None = None,
) -> None:
    """Record this question's row. With no ``answer`` in hand (shadow), wait for it — in the
    background when the policy says so, so the turn never waits on Jev."""

    async def write() -> None:
        outcome = answer if answer is not None else await read.answer(name, deadline_s=None)
        await record_decision(
            read=read,
            question=name,
            mode=mode,
            outcome=outcome,
            used=used,
            baseline_intent=baseline_intent,
            baseline_score=baseline_score,
            attempt_id=attempt_id,
        )

    if answer is None and get_runtime().policy.background:
        _spawn(write())
    else:
        await write()


async def _fast_intent(
    llm: LLMClient, *, question: str, message: str, context: ReadContext
) -> TurnIntent:
    """Today's gate: the FAST classifier, with its call logged."""
    intent, usage = await conversation_evidence.classify_intent(
        llm, question=question, message=message
    )
    if usage.input_tokens or usage.output_tokens:
        await log_llm_call(
            learner_id=context.learner_id,
            conversation_id=context.conversation_id,
            role=conversation_evidence.CHECK_ROLE.value,
            spec=llm.spec(conversation_evidence.CHECK_ROLE),
            usage=usage,
        )
    return intent


async def decide_intent(
    llm: LLMClient,
    *,
    question: str,
    message: str,
    context: ReadContext,
    read: TurnRead | None = None,
) -> TurnIntent:
    """What ``message`` does about ``question``. Never raises, like the gate it wraps.

    ``read`` is a turn read already asking ``intent`` (the conversational check shares one with
    grading); without one, a read is started here if the question is on.
    """
    policy = get_runtime().policy
    mode = policy.mode(INTENT)
    if read is None or not read.asks(INTENT):
        read = start_turn_read(
            questions=[INTENT],
            stem=question,
            message=message,
            rubric_criteria=None,
            context=context,
        )
    answer: Answer | DecisionFailure | None = None
    if mode == "live" and read is not None:
        answer = await read.answer(INTENT, deadline_s=policy.live_deadline_s)
        if isinstance(answer, ChoiceAnswer) and answer.confidence >= policy.intent_threshold:
            await _settle(read, INTENT, mode=mode, used=True, answer=answer)
            return TurnIntent(answer.label)
    intent = await _fast_intent(llm, question=question, message=message, context=context)
    if read is not None:
        await _settle(
            read, INTENT, mode=mode, used=False, answer=answer, baseline_intent=intent.value
        )
    return intent


async def decide_grade(
    *,
    smart: Callable[[], Awaitable[GradeResult]],
    stem: str,
    answer: str,
    rubric_criteria: dict | None,
    context: ReadContext,
    attempt_id: uuid.UUID | None,
    read: TurnRead | None = None,
) -> GradeResult:
    """Grade an open answer: ``smart`` is today's SMART grader, already wrapped with its logging.

    Jev can only ever *skip* ``smart`` for an answer it is confident is fully correct (live
    mode). It never produces a failing grade — a wrong answer needs the grader's rationale and
    diagnosis, which Jev cannot write.
    """
    policy = get_runtime().policy
    mode = policy.mode(FULLY_CORRECT)
    if not answer.strip():
        return await smart()
    if read is None or not read.asks(FULLY_CORRECT):
        read = start_turn_read(
            questions=[FULLY_CORRECT],
            stem=stem,
            message=answer,
            rubric_criteria=rubric_criteria,
            context=context,
        )
    verdict: Answer | DecisionFailure | None = None
    if mode == "live" and read is not None:
        verdict = await read.answer(FULLY_CORRECT, deadline_s=policy.live_deadline_s)
        if (
            isinstance(verdict, YesNoAnswer)
            and verdict.probability >= policy.fully_correct_threshold
        ):
            await _settle(
                read, FULLY_CORRECT, mode=mode, used=True, answer=verdict, attempt_id=attempt_id
            )
            # No rationale, no diagnosis and no per-component scores: a correct answer has no
            # failure to diagnose, and every component takes the aggregate.
            return GradeResult(score=1.0, correct=True, detail={"method": "decision"})
    try:
        result = await smart()
    except Exception:
        # A read already in flight must still be recorded — losing the row would be worse than
        # the failure itself, which propagates unchanged either way.
        if read is not None:
            await _settle(
                read, FULLY_CORRECT, mode=mode, used=False, answer=verdict, attempt_id=attempt_id
            )
        raise
    if read is not None:
        await _settle(
            read,
            FULLY_CORRECT,
            mode=mode,
            used=False,
            answer=verdict,
            baseline_score=result.score,
            attempt_id=attempt_id,
        )
    return result
