"""One Jev read per learner turn (S81).

A System One model answers several independent questions about one state in one request. So a
turn that needs two decisions — what the reply does about an open question, and whether it is
fully correct — asks both at once, and each consumer takes its own answer from the shared
:class:`TurnRead` handle. The request starts the moment the read is made; nobody waits for it
until they need their answer.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from app.llm.decisions import (
    Answer,
    DecisionClient,
    DecisionFailure,
    DecisionResponse,
    FailureKind,
    Question,
)


@dataclass(frozen=True)
class ReadContext:
    """What a read is about, for its ``decision_calls`` rows. Ids only — never text."""

    learner_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    item_id: uuid.UUID | None


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
