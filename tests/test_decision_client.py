"""The decision client: the one seam between Guru and a System One model (S78).

Everything here runs offline. The TypeSafe client is driven through the SDK's own transport
hook with a canned HTTP response, so the mapping from the vendor's wire shape to Guru's types is
tested without a key or a network.
"""

import asyncio
import json
import pathlib
import time

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.llm.decisions import (
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionFailure,
    DecisionResponse,
    DecisionsMisconfigured,
    FailureKind,
    FakeDecisionClient,
    TypeSafeDecisionClient,
    YesNoAnswer,
    YesNoQuestion,
    build_decision_client,
)

QUESTIONS = {
    "intent": ChoiceQuestion(
        instructions="What does the reply do?",
        options={"attempt": "tries", "deferral": "asks", "withdrawal": "leaves"},
    ),
    "fully_correct": YesNoQuestion(instructions="Is it fully correct?"),
}
STATE = {"question": "What is velocity?", "learner_reply": "speed with a direction"}


def _client(handler) -> TypeSafeDecisionClient:
    return TypeSafeDecisionClient(
        api_key="test-key", model="jev-1.13.0", transport=httpx2.MockTransport(handler)
    )


def _ok_body(**answers: object) -> dict:
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 42}, "answers": answers}


async def test_answers_come_back_as_guru_types() -> None:
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_ok_body(
                intent={
                    "type": "choice",
                    "choice": "attempt",
                    "confidence": 0.93,
                    "probabilities": {"attempt": 0.93, "deferral": 0.05, "withdrawal": 0.02},
                },
                fully_correct={"type": "noul", "noul": 0.81},
            ),
        )

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=1.0)

    assert isinstance(result, DecisionResponse)
    assert result.answers["intent"] == ChoiceAnswer(
        label="attempt",
        probabilities={"attempt": 0.93, "deferral": 0.05, "withdrawal": 0.02},
        confidence=0.93,
    )
    assert result.answers["fully_correct"] == YesNoAnswer(probability=0.81)
    assert result.model == "jev-1.13.0"
    assert result.input_tokens == 42
    (body,) = seen
    assert body["state"] == STATE
    assert body["model"] == "jev-1.13.0"
    assert body["questions"]["intent"]["type"] == "choice"
    assert set(body["questions"]["intent"]["criteria"]) == {"attempt", "deferral", "withdrawal"}
    assert body["questions"]["fully_correct"]["type"] == "noul"


async def test_a_label_outside_the_options_is_invalid_not_trusted() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json=_ok_body(
                intent={
                    "type": "choice",
                    "choice": "maybe",
                    "confidence": 0.99,
                    "probabilities": {"maybe": 0.99},
                },
                fully_correct={"type": "noul", "noul": 0.5},
            ),
        )

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=1.0)

    assert isinstance(result, DecisionResponse)
    assert result.answers["intent"] == DecisionFailure(FailureKind.INVALID)
    assert result.answers["fully_correct"] == YesNoAnswer(probability=0.5)


async def test_a_missing_answer_is_invalid() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_ok_body(fully_correct={"type": "noul", "noul": 0.2}))

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=1.0)

    assert isinstance(result, DecisionResponse)
    assert result.answers["intent"] == DecisionFailure(FailureKind.INVALID)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, FailureKind.AUTH),
        (403, FailureKind.AUTH),
        (429, FailureKind.RATE_LIMITED),
        (422, FailureKind.INVALID),
        (500, FailureKind.SERVER),
        (529, FailureKind.SERVER),
    ],
)
async def test_every_http_failure_is_a_value_not_an_exception(
    status: int, kind: FailureKind
) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(status, json={"error": "nope"})

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=1.0)

    assert result == DecisionFailure(kind)
    assert calls == 1  # no retries: a learner is waiting, and the fallback is today's path


async def test_a_connection_timeout_is_a_timeout() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=request)

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=0.1)

    assert result == DecisionFailure(FailureKind.TIMEOUT)


async def test_a_trickling_server_still_hits_the_overall_bound() -> None:
    """httpx's per-operation timeout does not cap the request as a whole (Review focus 2): a
    handler that just sleeps past `timeout_s` never trips httpx's own machinery, since the mock
    transport never touches a socket. Only `asyncio.timeout` around the whole call catches it."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0.3)
        return httpx2.Response(200, json=_ok_body(fully_correct={"type": "noul", "noul": 0.5}))

    started = time.perf_counter()
    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=0.05)
    elapsed = time.perf_counter() - started

    assert result == DecisionFailure(FailureKind.TIMEOUT)
    assert elapsed < 0.2


async def test_an_unreadable_body_is_invalid() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"not json")

    result = await _client(handler).read(STATE, QUESTIONS, timeout_s=1.0)

    assert result == DecisionFailure(FailureKind.INVALID)


async def test_the_fake_answers_what_it_was_scripted_and_records_the_request() -> None:
    fake = FakeDecisionClient({"fully_correct": YesNoAnswer(probability=0.7)})

    result = await fake.read(STATE, QUESTIONS, timeout_s=1.0)

    assert isinstance(result, DecisionResponse)
    assert result.answers["fully_correct"] == YesNoAnswer(probability=0.7)
    assert result.answers["intent"] == DecisionFailure(FailureKind.INVALID)  # not scripted
    assert fake.requests == [(STATE, QUESTIONS)]


async def test_the_fake_times_out_when_slower_than_the_timeout() -> None:
    fake = FakeDecisionClient({}, delay_s=0.5)

    assert await fake.read(STATE, QUESTIONS, timeout_s=0.01) == DecisionFailure(FailureKind.TIMEOUT)


def test_everything_off_needs_no_key_and_builds_nothing() -> None:
    assert build_decision_client(Settings(typesafe_api_key=SecretStr(""))) is None


@pytest.mark.parametrize("field", ["decision_intent_mode", "decision_fully_correct_mode"])
@pytest.mark.parametrize("mode", ["shadow", "live"])
def test_a_mode_that_is_on_without_a_key_refuses_to_start(field: str, mode: str) -> None:
    settings = Settings(typesafe_api_key=SecretStr("  "), **{field: mode})  # ty: ignore[invalid-argument-type]

    with pytest.raises(DecisionsMisconfigured) as raised:
        build_decision_client(settings)

    assert f"GURU_{field.upper()}" in str(raised.value)
    assert "GURU_TYPESAFE_API_KEY" in str(raised.value)


def test_a_mode_that_is_on_with_a_key_builds_the_typesafe_client() -> None:
    settings = Settings(typesafe_api_key=SecretStr("k"), decision_intent_mode="shadow")

    client = build_decision_client(settings)

    assert isinstance(client, TypeSafeDecisionClient)
    assert client.model == "jev-1.13.0"
    assert client.provider == "typesafe"


def test_only_the_decision_client_imports_the_vendor_sdk() -> None:
    """Services reach Jev through `DecisionClient`, never the SDK (CLAUDE.md, LLM rules)."""
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    importers = sorted(
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if "typesafe_sdk" in path.read_text(encoding="utf-8")
    )
    assert importers == ["llm/decisions.py"]
