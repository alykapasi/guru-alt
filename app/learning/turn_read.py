"""One Jev read per learner turn (S81).

A System One model answers several independent questions about one state in one request. So a
turn that needs two decisions — what the reply does about an open question, and whether it is
fully correct — asks both at once, and each consumer takes its own answer from the shared
:class:`TurnRead` handle. The request starts the moment the read is made; nobody waits for it
until they need their answer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.agent.untrusted import as_untrusted
from app.learning.conversation_evidence import INTENT_OPTIONS, INTENT_TASK, INTENT_TIEBREAK
from app.llm.decisions import (
    Answer,
    ChoiceQuestion,
    DecisionClient,
    DecisionFailure,
    DecisionResponse,
    FailureKind,
    Question,
    YesNoQuestion,
)


@dataclass(frozen=True)
class ReadContext:
    """What a read is about, for its ``decision_calls`` rows. Ids only — never text."""

    learner_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    item_id: uuid.UUID | None


INTENT = "intent"
FULLY_CORRECT = "fully_correct"

QUESTIONS: dict[str, Question] = {
    INTENT: ChoiceQuestion(
        instructions=f"{INTENT_TASK} {INTENT_TIEBREAK}", options=dict(INTENT_OPTIONS)
    ),
    FULLY_CORRECT: YesNoQuestion(
        instructions=(
            "Does the learner's reply fully and correctly answer the question, meeting every "
            "rubric criterion given? A partial, vague or partly wrong answer is not fully correct."
        ),
        yes="The reply is complete and correct.",
        no="Something is missing, vague or wrong.",
    ),
}
"""The questions a turn can ask. ``fully_correct`` is only ever used to *skip* grading a correct
answer — never to fail one — so it is worded to say no to anything short of complete."""


def build_state(*, stem: str, message: str, rubric_criteria: dict | None) -> dict[str, str]:
    """What Jev reads. The reply is fenced exactly as the grader fences it (S31): it is written by
    the person being judged, and is the one part with a motive to say "answer yes"."""
    state = {"question": stem, "learner_reply": as_untrusted("LEARNER REPLY", message)}
    if rubric_criteria:
        state["rubric_criteria"] = json.dumps(rubric_criteria)
    return state


def start_read(
    client: DecisionClient,
    *,
    questions: Sequence[str],
    stem: str,
    message: str,
    rubric_criteria: dict | None,
    context: ReadContext,
    timeout_s: float,
) -> TurnRead:
    """Start one request asking ``questions`` about this turn, and return its handle."""
    return TurnRead(
        client,
        {name: QUESTIONS[name] for name in questions},
        build_state(stem=stem, message=message, rubric_criteria=rubric_criteria),
        timeout_s=timeout_s,
        context=context,
    )


class TurnRead:
    """A Jev request in flight, and the questions it asked."""

    def __init__(
        self,
        client: DecisionClient,
        questions: Mapping[str, Question],
        state: Mapping[str, str],
        *,
        timeout_s: float,
        context: ReadContext,
    ) -> None:
        self.client = client
        self.questions = dict(questions)
        self.context = context
        self.request_id = uuid.uuid4()
        self._task = asyncio.create_task(client.read(state, self.questions, timeout_s=timeout_s))

    def asks(self, name: str) -> bool:
        return name in self.questions

    async def answer(self, name: str, *, deadline_s: float | None) -> Answer | DecisionFailure:
        """This question's answer, waiting at most ``deadline_s`` (``None``: until the request
        ends, which its own timeout bounds). Giving up does not cancel the request — another
        question on it may still be waiting for the same answer."""
        try:
            result = await asyncio.wait_for(asyncio.shield(self._task), deadline_s)
        except TimeoutError:
            return DecisionFailure(FailureKind.TIMEOUT)
        if isinstance(result, DecisionFailure):
            return result
        return result.answers.get(name, DecisionFailure(FailureKind.INVALID))

    def completed(self) -> DecisionResponse | DecisionFailure | None:
        """The request's result if it has finished, else ``None``. Never waits."""
        if self._task.done() and not self._task.cancelled():
            return self._task.result()
        return None
