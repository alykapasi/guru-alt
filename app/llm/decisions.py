"""Typed decisions from a System One model — the one seam for them (S78).

Jev (TypeSafe) is not a language model. It reads a state and answers named questions about it:
a label with its probability distribution, or a yes/no probability. It writes no text. Guru uses
it as a fast first pass in front of the LLM — see ``app.services.decisions`` for how its answers
are used and ``docs/RUNBOOK.md`` §14 for operating it.

This module is the only one that imports ``typesafe_sdk``, the same rule ``LLMClient`` holds
for provider SDKs: everything past it speaks Guru's own question and answer types, so the vendor
can change without the services noticing.

**``read`` never raises.** Every way a request can go wrong — a timeout, a rejected key, a rate
limit, a 5xx, a body that does not parse, a label that is not one of the options — comes back
as a :class:`DecisionFailure` value, because every caller's answer to any of them is the same:
run today's path instead.

**No retries.** The SDK's default is two retries inside a 30-second budget. A decision exists
to be faster than the model call it stands in front of, so one attempt is made, bounded by the
caller's timeout, and a failure falls back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import httpx2
import structlog
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    Noul,
    RetryPolicy,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)
from typesafe_sdk import ChoiceAnswer as SdkChoiceAnswer
from typesafe_sdk import NoulAnswer as SdkNoulAnswer

from app.core.config import Settings

log = structlog.get_logger(__name__)

_NO_RETRIES = RetryPolicy(max_retries=0)


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one label. ``options`` maps each label to what it means."""

    instructions: str
    options: Mapping[str, str]


@dataclass(frozen=True)
class YesNoQuestion:
    """A yes/no judgment, optionally with what each outcome means."""

    instructions: str
    yes: str | None = None
    no: str | None = None


Question = ChoiceQuestion | YesNoQuestion


@dataclass(frozen=True)
class ChoiceAnswer:
    label: str
    probabilities: Mapping[str, float]
    confidence: float


@dataclass(frozen=True)
class YesNoAnswer:
    """Jev's yes/no answer is a probability and nothing else: it reports no confidence, and none
    is invented here."""

    probability: float


Answer = ChoiceAnswer | YesNoAnswer


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    SERVER = "server"
    INVALID = "invalid"


@dataclass(frozen=True)
class DecisionFailure:
    kind: FailureKind


@dataclass(frozen=True)
class DecisionResponse:
    """One request's answers, keyed by question name. A question the model answered out of
    bounds is a :class:`DecisionFailure` here while its neighbours still stand."""

    answers: Mapping[str, Answer | DecisionFailure]
    model: str
    input_tokens: int | None
    latency_ms: int


@runtime_checkable
class DecisionClient(Protocol):
    provider: str
    model: str

    async def read(
        self, state: Mapping[str, str], questions: Mapping[str, Question], *, timeout_s: float
    ) -> DecisionResponse | DecisionFailure: ...


class TypeSafeDecisionClient:
    """:class:`DecisionClient` over TypeSafe's ``system_one`` endpoint."""

    provider = "typesafe"

    def __init__(
        self, *, api_key: str, model: str, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        # The SDK's DEBUG output includes request bodies — the learner's own words. Pinned here,
        # where the client is made, so no logging configuration elsewhere can turn it back on
        # by accident.
        logging.getLogger("typesafe_sdk").setLevel(logging.WARNING)
        self.model = model
        self._client = AsyncTypeSafeClient(
            api_key=api_key, model=model, retry=_NO_RETRIES, transport=transport
        )

    async def read(
        self, state: Mapping[str, str], questions: Mapping[str, Question], *, timeout_s: float
    ) -> DecisionResponse | DecisionFailure:
        started = time.perf_counter()
        try:
            # httpx's `timeout` bounds each operation (connect/read/write/pool) separately, not
            # the request as a whole — a server trickling data can outlast it. This is the hard
            # overall bound. `TimeoutError` from its expiry maps to FailureKind.TIMEOUT below,
            # same as the SDK's own timeout error.
            async with asyncio.timeout(timeout_s):
                response = await self._client.system_one(
                    dict(state),
                    {name: _to_sdk(question) for name, question in questions.items()},
                    timeout=timeout_s,
                    retry=_NO_RETRIES,
                )
            # Inside the same try as the request: a malformed answer must fall back too, not
            # raise past this method.
            return DecisionResponse(
                answers={
                    name: _from_sdk(question, response.answers.get(name))
                    for name, question in questions.items()
                },
                model=response.model,
                input_tokens=response.usage.input_tokens,
                latency_ms=round((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:  # every failure has the same answer: fall back
            failure = _failure_of(exc)
            log.warning(
                "decision.request_failed", kind=failure.kind.value, error=type(exc).__name__
            )
            return failure


def _failure_of(exc: Exception) -> DecisionFailure:
    """The failure kind for an exception out of the SDK. Order matters: the SDK's timeout is a
    connection error, and its validation error is an API error."""
    if isinstance(exc, TypeSafeAPITimeoutError | TimeoutError):
        return DecisionFailure(FailureKind.TIMEOUT)
    if isinstance(exc, TypeSafeRateLimitError):
        return DecisionFailure(FailureKind.RATE_LIMITED)
    if isinstance(exc, TypeSafeAuthenticationError | TypeSafePermissionDeniedError):
        return DecisionFailure(FailureKind.AUTH)
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return DecisionFailure(FailureKind.INVALID)
    if isinstance(exc, TypeSafeAPIError):
        return DecisionFailure(FailureKind.SERVER if exc.status >= 500 else FailureKind.INVALID)
    if isinstance(exc, TypeSafeAPIConnectionError):
        return DecisionFailure(FailureKind.SERVER)
    if isinstance(exc, TypeSafeError):
        return DecisionFailure(FailureKind.INVALID)
    # Anything else (a body that is not JSON surfaces as a decode error) is a reply we cannot
    # use, which is what `invalid` means.
    return DecisionFailure(FailureKind.INVALID)


def _to_sdk(question: Question) -> Choice | Noul:
    if isinstance(question, ChoiceQuestion):
        return Choice(instructions=question.instructions, criteria=dict(question.options))
    if question.yes is None and question.no is None:
        return Noul(instructions=question.instructions)
    return Noul(
        instructions=question.instructions, criteria={"true": question.yes, "false": question.no}
    )


def _from_sdk(question: Question, answer: object) -> Answer | DecisionFailure:
    if isinstance(question, ChoiceQuestion):
        if isinstance(answer, SdkChoiceAnswer) and answer.choice in question.options:
            return ChoiceAnswer(
                label=answer.choice,
                probabilities=dict(answer.probabilities),
                confidence=answer.confidence,
            )
        return DecisionFailure(FailureKind.INVALID)
    if isinstance(answer, SdkNoulAnswer) and 0.0 <= answer.noul <= 1.0:
        return YesNoAnswer(probability=answer.noul)
    return DecisionFailure(FailureKind.INVALID)


@dataclass
class FakeDecisionClient:
    """Scripted answers for tests. A question with no scripted answer comes back ``invalid``."""

    answers: Mapping[str, Answer | DecisionFailure] | None = None
    failure: DecisionFailure | None = None
    delay_s: float = 0.0
    model: str = "fake-decider"
    provider: str = "fake"
    requests: list[tuple[dict[str, str], dict[str, Question]]] = field(default_factory=list)

    async def read(
        self, state: Mapping[str, str], questions: Mapping[str, Question], *, timeout_s: float
    ) -> DecisionResponse | DecisionFailure:
        self.requests.append((dict(state), dict(questions)))
        if self.delay_s > timeout_s:
            await asyncio.sleep(timeout_s)
            return DecisionFailure(FailureKind.TIMEOUT)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            return self.failure
        scripted = self.answers or {}
        return DecisionResponse(
            answers={
                name: scripted.get(name, DecisionFailure(FailureKind.INVALID)) for name in questions
            },
            model=self.model,
            input_tokens=100,
            latency_ms=round(self.delay_s * 1000),
        )


class DecisionsMisconfigured(RuntimeError):
    """Raised at startup when a decision is switched on with nothing to call."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("refusing to start: " + "; ".join(problems))


def _modes(settings: Settings) -> dict[str, str]:
    return {
        "GURU_DECISION_INTENT_MODE": settings.decision_intent_mode,
        "GURU_DECISION_FULLY_CORRECT_MODE": settings.decision_fully_correct_mode,
    }


def decision_problems(settings: Settings) -> list[str]:
    """Every reason these settings cannot run, as operator-readable sentences."""
    on = [name for name, mode in _modes(settings).items() if mode != "off"]
    if on and not settings.typesafe_api_key.get_secret_value().strip():
        return [f"{', '.join(on)} is on but GURU_TYPESAFE_API_KEY is empty"]
    return []


def build_decision_client(settings: Settings) -> DecisionClient | None:
    """The client the settings call for: none when every question is off.

    Raises :class:`DecisionsMisconfigured` rather than letting a switched-on question discover
    a missing key on a learner's turn.
    """
    problems = decision_problems(settings)
    if problems:
        raise DecisionsMisconfigured(problems)
    if all(mode == "off" for mode in _modes(settings).values()):
        return None
    return TypeSafeDecisionClient(
        api_key=settings.typesafe_api_key.get_secret_value(), model=settings.decision_model
    )
