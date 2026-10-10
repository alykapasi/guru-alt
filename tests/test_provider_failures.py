"""A provider that is busy or down is told apart from a bug in Guru (S49)."""

import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ChatMessage, ChatRole, ModelRole
from app.llm.attribution import attributed
from app.llm.meter import (
    PROVIDER_MESSAGES,
    BudgetExceeded,
    CallRefused,
    ProviderUnavailable,
)
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.anthropic import refusal as anthropic_refusal
from app.llm.providers.fake import FakeProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.providers.openai_compat import refusal as openai_refusal
from app.llm.registry import LLMClient, ModelSpec
from app.models.chat import LLMCall
from app.models.learner import Learner

HELLO = [ChatMessage(role=ChatRole.USER, content="hello")]
REQUEST = httpx.Request("POST", "https://provider.test/v1")


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, request=REQUEST)


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


# --- the refusal types ------------------------------------------------------------------------


def test_a_spend_refusal_is_a_refused_call_with_its_old_shape() -> None:
    exc = BudgetExceeded("learner")
    assert isinstance(exc, CallRefused)
    assert (exc.code, exc.reason, exc.scope, exc.retry_after) == (
        "budget_exceeded",
        "budget",
        "learner",
        None,
    )


def test_a_provider_refusal_says_what_the_learner_reads() -> None:
    exc = ProviderUnavailable("busy", retry_after=7.0)
    assert isinstance(exc, CallRefused)
    assert (exc.code, exc.reason, exc.retry_after) == ("provider_busy", "provider", 7.0)
    assert str(exc) == exc.message == PROVIDER_MESSAGES["busy"]
    assert PROVIDER_MESSAGES == {
        "busy": "The tutor is busy right now — try again in a few seconds.",
        "down": "The tutor can't be reached right now — try again shortly.",
    }


# --- classification ---------------------------------------------------------------------------


def test_a_rate_limit_is_busy_with_the_providers_wait() -> None:
    exc = anthropic.RateLimitError(
        "slow down", response=_response(429, {"retry-after": "7"}), body=None
    )
    refused = anthropic_refusal(exc)
    assert refused is not None
    assert (refused.kind, refused.retry_after) == ("busy", 7.0)


def test_a_rate_limit_without_a_wait_uses_the_default() -> None:
    refused = openai_refusal(openai.RateLimitError("slow down", response=_response(429), body=None))
    assert refused is not None
    assert (refused.kind, refused.retry_after) == ("busy", 20.0)


def test_an_http_date_wait_is_read() -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    exc = openai.RateLimitError("slow", response=_response(429, {"retry-after": when}), body=None)
    refused = openai_refusal(exc)
    assert refused is not None and refused.retry_after is not None
    assert 25.0 <= refused.retry_after <= 31.0


@pytest.mark.parametrize("raw", ["soon", "inf", "Infinity", "1e400", "nan"])
def test_a_malformed_wait_falls_back_to_the_default(raw: str) -> None:
    """A wait that is not a finite number would crash the 503 and break the stream's JSON."""
    exc = openai.RateLimitError("slow", response=_response(429, {"retry-after": raw}), body=None)
    refused = openai_refusal(exc)
    assert refused is not None and refused.retry_after == 20.0


def test_a_very_long_wait_is_capped() -> None:
    """A daily quota's wait is not a countdown anyone should watch."""
    exc = openai.RateLimitError(
        "slow", response=_response(429, {"retry-after": "86400"}), body=None
    )
    refused = openai_refusal(exc)
    assert refused is not None and refused.retry_after == 300.0


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.InternalServerError("boom", response=_response(500), body=None),
        anthropic.OverloadedError("overloaded", response=_response(529), body=None),
        anthropic.APIConnectionError(request=REQUEST),
        anthropic.APITimeoutError(request=REQUEST),
    ],
    ids=["500", "529", "connection", "timeout"],
)
def test_an_anthropic_outage_is_down(exc: Exception) -> None:
    refused = anthropic_refusal(exc)
    assert refused is not None
    assert (refused.kind, refused.retry_after) == ("down", None)


@pytest.mark.parametrize(
    "exc",
    [
        openai.InternalServerError("boom", response=_response(503), body=None),
        openai.APIConnectionError(request=REQUEST),
        openai.APITimeoutError(request=REQUEST),
        openai.APIError("An error occurred during streaming", REQUEST, body={"message": "x"}),
    ],
    ids=["503", "connection", "timeout", "stream-error"],
)
def test_an_openai_compatible_outage_is_down(exc: Exception) -> None:
    refused = openai_refusal(exc)
    assert refused is not None and refused.kind == "down"


def test_an_openai_stream_error_coded_429_is_busy() -> None:
    exc = openai.APIError("rate limited", REQUEST, body={"code": 429, "message": "x"})
    refused = openai_refusal(exc)
    assert refused is not None and refused.kind == "busy"


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [("overloaded_error", "down"), ("api_error", "down"), ("rate_limit_error", "busy")],
)
def test_an_error_inside_an_anthropic_stream_is_read_by_its_type(
    error_type: str, kind: str
) -> None:
    """Mid-stream, the SDK raises from an SSE error event on a 200 response."""
    exc = anthropic.APIStatusError(
        "stream error",
        response=_response(200),
        body={"type": "error", "error": {"type": error_type, "message": "x"}},
    )
    refused = anthropic_refusal(exc)
    assert refused is not None and refused.kind == kind


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.BadRequestError("bad", response=_response(400), body=None),
        anthropic.AuthenticationError("bad key", response=_response(401), body=None),
        openai.PermissionDeniedError("no", response=_response(403), body=None),
        openai.NotFoundError("no model", response=_response(404), body=None),
        openai.APIResponseValidationError(response=_response(200), body=None),
        ValueError("ours"),
    ],
    ids=["400", "401", "403", "404", "validation", "not-sdk"],
)
def test_a_client_error_is_not_a_refusal(exc: Exception) -> None:
    assert anthropic_refusal(exc) is None
    assert openai_refusal(exc) is None


# --- the adapters translate ---------------------------------------------------------------------


async def test_an_anthropic_completion_refused_raises_provider_unavailable(monkeypatch) -> None:
    provider = AnthropicProvider(api_key="k", timeout=1.0, max_retries=0)
    original = anthropic.RateLimitError("slow", response=_response(429), body=None)

    async def create(**kwargs):
        raise original

    monkeypatch.setattr(provider._client.messages, "create", create)
    with pytest.raises(ProviderUnavailable) as caught:
        await provider.complete(model="m", messages=HELLO)
    assert caught.value.kind == "busy" and caught.value.__cause__ is original


async def test_an_anthropic_stream_refused_raises_provider_unavailable(monkeypatch) -> None:
    provider = AnthropicProvider(api_key="k", timeout=1.0, max_retries=0)

    class Refusing:
        async def __aenter__(self):
            raise anthropic.OverloadedError("overloaded", response=_response(529), body=None)

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(provider._client.messages, "stream", lambda **kwargs: Refusing())
    with pytest.raises(ProviderUnavailable) as caught:
        async for _ in provider.stream(model="m", messages=HELLO):
            pass
    assert caught.value.kind == "down"


async def test_an_anthropic_stream_that_fails_part_way_is_translated(monkeypatch) -> None:
    """The error arrives after text has already been yielded, on the stream's 200."""
    provider = AnthropicProvider(api_key="k", timeout=1.0, max_retries=0)

    async def text_stream():
        yield "Hello"
        raise anthropic.APIStatusError(
            "stream error",
            response=_response(200),
            body={"type": "error", "error": {"type": "overloaded_error", "message": "x"}},
        )

    class Stream:
        async def __aenter__(self):
            return SimpleNamespace(text_stream=text_stream())

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(provider._client.messages, "stream", lambda **kwargs: Stream())
    seen: list[str] = []
    with pytest.raises(ProviderUnavailable) as caught:
        async for chunk in provider.stream(model="m", messages=HELLO):
            seen.append(chunk.text)
    assert seen == ["Hello"] and caught.value.kind == "down"


async def test_an_openai_stream_that_fails_part_way_is_translated(monkeypatch) -> None:
    """OpenRouter reports a mid-generation failure as an error event: a bare APIError."""
    provider = OpenAICompatProvider(
        name="openrouter",
        base_url="https://provider.test/v1",
        api_key="k",
        timeout=1.0,
        max_retries=0,
    )
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="Hello", tool_calls=None), finish_reason=None
            )
        ],
        usage=None,
    )

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def __aiter__(self):
            yield chunk
            raise openai.APIError("upstream failed", REQUEST, body={"message": "x"})

    async def create(**kwargs):
        return Stream()

    monkeypatch.setattr(provider._client.chat.completions, "create", create)
    seen: list[str] = []
    with pytest.raises(ProviderUnavailable) as caught:
        async for out in provider.stream(model="m", messages=HELLO):
            seen.append(out.text)
    assert seen == ["Hello"] and caught.value.kind == "down"


async def test_an_anthropic_client_error_passes_through(monkeypatch) -> None:
    provider = AnthropicProvider(api_key="k", timeout=1.0, max_retries=0)

    async def create(**kwargs):
        raise anthropic.AuthenticationError("bad key", response=_response(401), body=None)

    monkeypatch.setattr(provider._client.messages, "create", create)
    with pytest.raises(anthropic.AuthenticationError):
        await provider.complete(model="m", messages=HELLO)


@pytest.mark.parametrize("call", ["complete", "stream", "embed"])
async def test_an_openai_compatible_call_refused_raises_provider_unavailable(
    monkeypatch, call: str
) -> None:
    provider = OpenAICompatProvider(
        name="openrouter",
        base_url="https://provider.test/v1",
        api_key="k",
        timeout=1.0,
        max_retries=0,
    )

    async def refuse(**kwargs):
        raise openai.InternalServerError("boom", response=_response(502), body=None)

    monkeypatch.setattr(provider._client.chat.completions, "create", refuse)
    monkeypatch.setattr(provider._client.embeddings, "create", refuse)
    with pytest.raises(ProviderUnavailable) as caught:
        if call == "complete":
            await provider.complete(model="m", messages=HELLO)
        elif call == "stream":
            async for _ in provider.stream(model="m", messages=HELLO):
                pass
        else:
            await provider.embed(model="m", texts=["x"])
    assert caught.value.kind == "down"


# --- the fake, and what the meter records --------------------------------------------------------


async def test_the_fake_can_refuse_part_way_through_a_stream() -> None:
    provider = FakeProvider(
        "one two three", refuse=ProviderUnavailable("down"), refuse_after_words=1
    )
    seen: list[str] = []
    with pytest.raises(ProviderUnavailable):
        async for chunk in provider.stream(model="m", messages=HELLO):
            seen.append(chunk.text)
    assert seen == ["one"]


async def test_a_refused_call_is_recorded_with_its_code(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.commit()
    client = _client(FakeProvider(refuse=ProviderUnavailable("busy", retry_after=5.0)))

    with attributed(learner_id=learner.id, feature="chat_turn"), pytest.raises(ProviderUnavailable):
        await client.complete(ModelRole.FAST, HELLO)

    row = await db_session.scalar(
        select(LLMCall)
        .where(LLMCall.learner_id == learner.id)
        .execution_options(populate_existing=True)
    )
    assert row is not None
    assert (row.status, row.error_kind) == ("failed", "provider_busy")
