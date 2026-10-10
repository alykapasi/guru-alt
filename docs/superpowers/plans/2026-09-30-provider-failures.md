# Provider Rate Limits and Outages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A provider that is rate-limiting or down gives every route, stream and worker one predictable answer the learner can read, with safe retry, instead of "generation failed", a bare 500 or a logged failure.

**Architecture:** The provider adapters (the only SDK importers) translate an SDK error that survived the SDK's own retries into `ProviderUnavailable(kind="busy"|"down", retry_after)`. It and `BudgetExceeded` share a new base, `CallRefused`, and every place that already turns a spend refusal into a reason — the flows' re-raise, `refusal_ends_turn`, worker deferral, ingestion, notes — catches the base. An app handler answers 503 with `Retry-After`; streamed turns carry `code`/`retry_after` on their error frame; the chat's Retry waits out a busy provider.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, `anthropic` 0.113 and `openai` 2.44 SDKs, pytest; React + TypeScript + Vitest.

**Spec:** `docs/superpowers/specs/2026-09-30-provider-failures-design.md`

## Global Constraints

- Branch `feat/workstream-2` (open PR #44). Never reset, amend, rebase, squash or force-push.
- Stage only the task's files by name, then run `git status`.
- Commit subjects end `[S49]`; every commit message ends with the exact line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never read or print `.env` or any secret; never set `GURU_JEV_SMOKE`; no paid model runs.
- Busy message, verbatim: `The tutor is busy right now — try again in a few seconds.`
- Down message, verbatim: `The tutor can't be reached right now — try again shortly.`
- Codes: `provider_busy`, `provider_down` (and `budget_exceeded` for spend refusals, unchanged).
- Classification after the SDK's retries: 429 or error type `rate_limit_error` → busy, `retry_after` = the `retry-after` header (seconds or HTTP-date), else `provider_retry_after_seconds` (20.0); 5xx, 529, error type `overloaded_error`/`api_error`, connection error, SDK timeout → down, `retry_after` None; anything else (400, 401, 403, 404, response-validation errors) is re-raised unchanged.
- Non-streamed: **503** `{"detail": {"code", "message", "retry_after"}}` plus `Retry-After: <ceil seconds>` when `retry_after` is set. `BudgetExceeded` keeps **429** and its existing shape.
- A reply the provider broke off is not saved; the turn closes `failed` with error `provider_busy`/`provider_down`.
- Gates: `uv run poe check`, `uv run poe format-check`; after changing a schema-facing type, `uv run poe api-types`, stage `frontend/src/api/schema.d.ts`, then `uv run poe api-contract`. Frontend (in `frontend/`): `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint`, and `npx prettier --write` on changed files.
- Beartype runs at test time: annotate floats as `float` and pass `5.0`, never `5`.

## Review Focus

1. A bad API key (401/403) must not be reported to the learner as "busy" — it stays a loud 500. Pinned in Task 1 (`test_a_client_error_is_not_a_refusal`).
2. An Anthropic error that arrives *inside* a stream has HTTP status 200 and only an error `type` (`overloaded_error`, `rate_limit_error`); it must still be classified. Pinned in Task 1 (`test_an_error_inside_an_anthropic_stream_is_read_by_its_type`).
3. A provider that fails after some tokens reached the learner must save no partial reply, and the same `client_turn_id` must then answer. Pinned in Task 3 (`test_a_busy_provider_ends_the_turn_saying_so`, `test_a_turn_the_provider_refused_can_be_retried`).
4. Making `BudgetExceeded` a subclass must not change the spend refusal: still 429 `budget_exceeded` (existing `tests/test_spend_guard.py`), and workers still log `budget.deferred` (existing `test_background_work_refused_is_deferred_quietly`).
5. A `retry-after` header given as an HTTP-date, or as garbage, must give a sensible wait rather than a crash. Pinned in Task 1 (`test_an_http_date_wait_is_read`, `test_a_malformed_wait_falls_back_to_the_default`).

---

### Task 1: Refusal types, classification in the adapters, accounting

**Files:**
- Modify: `app/llm/meter.py` (refusal classes; `fail` records the code)
- Create: `app/llm/providers/failure.py`
- Modify: `app/llm/providers/anthropic.py`, `app/llm/providers/openai_compat.py`, `app/llm/providers/fake.py`
- Modify: `app/core/config.py` (setting), `tests/eval/reliability/knobs.py` (S18 inventory)
- Test: `tests/test_provider_failures.py` (create)

**Interfaces:**
- Produces (in `app.llm.meter`):
  - `class CallRefused(Exception)`: attributes `message: str`, `code: str`, `reason: Literal["budget", "provider"]`, `retry_after: float | None` (class default `None`).
  - `class BudgetExceeded(CallRefused)`: unchanged constructor `BudgetExceeded(scope)`, `code = "budget_exceeded"`, `reason = "budget"`, keeps `.scope` and `.message`.
  - `PROVIDER_MESSAGES: dict[str, str]` keyed `"busy"`/`"down"`.
  - `class ProviderUnavailable(CallRefused)`: `ProviderUnavailable(kind: Literal["busy","down"], *, retry_after: float | None = None)`; `.kind`, `.code == f"provider_{kind}"`, `reason = "provider"`, `str(exc) == exc.message`.
- Produces: `FakeProvider(reply=..., *, script=None, refuse: ProviderUnavailable | None = None, refuse_calls: frozenset[str] = frozenset({"complete", "stream", "embed"}), refuse_after_words: int = 0)`.
- Produces: `Settings.provider_retry_after_seconds: float = 20.0` (`GURU_PROVIDER_RETRY_AFTER_SECONDS`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_failures.py`:

```python
"""A provider that is busy or down is told apart from a bug in Guru (S49)."""

import uuid
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

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


def test_a_malformed_wait_falls_back_to_the_default() -> None:
    exc = openai.RateLimitError("slow", response=_response(429, {"retry-after": "soon"}), body=None)
    refused = openai_refusal(exc)
    assert refused is not None and refused.retry_after == 20.0


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
def test_an_error_inside_an_anthropic_stream_is_read_by_its_type(error_type: str, kind: str) -> None:
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
        name="openrouter", base_url="https://provider.test/v1", api_key="k", timeout=1.0, max_retries=0
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_provider_failures.py -q -p no:cacheprovider`
Expected: collection ERROR — `ImportError: cannot import name 'PROVIDER_MESSAGES' from 'app.llm.meter'`.

- [ ] **Step 3: Add the refusal types to `app/llm/meter.py`**

Replace the existing `class BudgetExceeded` block (lines ~37–44) with:

```python
class CallRefused(Exception):
    """A model call that did not happen, for a reason the learner can be told (S47, S49).

    Not a bug in Guru: a spend limit, or the provider refusing or out of reach. Every place
    that turns a refusal into a reason — the 429/503 handlers, ``refusal_ends_turn``, the
    workers' deferral — catches this base, so a new kind of refusal is handled everywhere at
    once.
    """

    code: str
    reason: Literal["budget", "provider"]
    retry_after: float | None = None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BudgetExceeded(CallRefused):
    """A paid call refused before it was made (S47)."""

    code = "budget_exceeded"
    reason = "budget"

    def __init__(self, scope: Scope) -> None:
        super().__init__(MESSAGES[scope])
        self.scope: Scope = scope


ProviderKind = Literal["busy", "down"]
PROVIDER_MESSAGES: dict[str, str] = {
    "busy": "The tutor is busy right now — try again in a few seconds.",
    "down": "The tutor can't be reached right now — try again shortly.",
}


class ProviderUnavailable(CallRefused):
    """The provider rate-limited the call or could not be reached, after the SDK's own
    retries (S49). Raised only by the provider adapters — the only SDK importers — from
    ``app.llm.providers.failure``; ``retry_after`` is the wait the provider asked for (busy)
    or ``None`` (down)."""

    reason = "provider"

    def __init__(self, kind: ProviderKind, *, retry_after: float | None = None) -> None:
        super().__init__(PROVIDER_MESSAGES[kind])
        self.kind: ProviderKind = kind
        self.code = f"provider_{kind}"
        self.retry_after = retry_after
```

In `async def fail(...)`, record the code for a provider refusal instead of the class name (the class name of every provider refusal would be the same, and the code is what an operator needs):

```python
async def fail(reservation: Reservation, exc: BaseException) -> None:
    usage = Usage(input_tokens=reservation.input_tokens, output_tokens=0)
    # A provider refusal records which (S49); anything else its class name, never its message.
    kind = exc.code if isinstance(exc, ProviderUnavailable) else type(exc).__name__
    log.info("llm.call", model=reservation.model, status="failed", error_kind=kind)
    await _update(
        reservation,
        status="failed",
        error_kind=kind,
        estimated=True,
        input_tokens=usage.input_tokens,
        output_tokens=0,
        cost_usd=price_usd(reservation.provider, reservation.model, usage),
    )
```

Update the module docstring's last sentence to: ``BudgetExceeded`` is the one exception raised here on purpose; ``ProviderUnavailable`` is defined here beside it and raised by the adapters.

- [ ] **Step 4: Add the setting and the knob**

In `app/core/config.py`, directly after `llm_max_retries: int = 2`:

```python
    # How long a learner is asked to wait when a provider rate-limits a call and names no wait
    # of its own (S49). Uncalibrated, listed in the S18 inventory.
    provider_retry_after_seconds: float = Field(default=20.0, gt=0)
```

In `tests/eval/reliability/knobs.py`, add after the `deadline.request_seconds` `Knob(...)`:

```python
    Knob(
        id="provider.retry_after_seconds",
        where="app.core.config.Settings.provider_retry_after_seconds",
        value=20.0,
        governs="how long a learner is asked to wait when a provider rate-limits without saying (S49)",
        settled_by="the retry-after values the chosen providers actually send, from alpha logs",
    ),
```

and in `live()` after `"deadline.request_seconds": ...`:

```python
        "provider.retry_after_seconds": float(s.provider_retry_after_seconds),
```

- [ ] **Step 5: Create `app/llm/providers/failure.py`**

```python
"""What a provider's error means to a learner (S49).

Each adapter reduces its own SDK's exception to a status, an error type, headers or "could
not connect"; this decides what those mean, the same way for every provider. It imports no
SDK. Only errors that survived the SDK's own retries ever get here.
"""

import contextlib
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from app.core.config import get_settings
from app.llm.meter import ProviderUnavailable

# Error types a provider names in a body — Anthropic's, inside a stream, arrive on a 200.
BUSY_TYPES = frozenset({"rate_limit_error"})
DOWN_TYPES = frozenset({"overloaded_error", "api_error"})


def retry_after_seconds(headers: Mapping[str, str] | None) -> float:
    """The provider's ``retry-after`` in seconds (a number or an HTTP-date), else the default."""
    default = get_settings().provider_retry_after_seconds
    raw = (headers or {}).get("retry-after")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def classify(
    *,
    status: int | None = None,
    error_type: str | None = None,
    headers: Mapping[str, str] | None = None,
    unreachable: bool = False,
) -> ProviderUnavailable | None:
    """Busy, down, or ``None`` — not a refusal (a bad request, a bad key: ours to fix)."""
    if unreachable:
        return ProviderUnavailable("down")
    if status == 429 or error_type in BUSY_TYPES:
        return ProviderUnavailable("busy", retry_after=retry_after_seconds(headers))
    if (status is not None and status >= 500) or error_type in DOWN_TYPES:
        return ProviderUnavailable("down")
    return None


@contextlib.contextmanager
def translated(refusal: Callable[[Exception], ProviderUnavailable | None]) -> Iterator[None]:
    """Re-raise an SDK error that is a refusal as ``ProviderUnavailable``; anything else as-is."""
    try:
        yield
    except Exception as exc:
        unavailable = refusal(exc)
        if unavailable is None:
            raise
        raise unavailable from exc
```

- [ ] **Step 6: Translate in the Anthropic adapter**

In `app/llm/providers/anthropic.py`, change the SDK import to
`from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic`, add
`from app.llm.meter import ProviderUnavailable` and
`from app.llm.providers.failure import classify, translated`, and add after `_warn_if_truncated`:

```python
def refusal(exc: Exception) -> ProviderUnavailable | None:
    """What an Anthropic SDK error means to a learner (S49); ``None`` when it is not a refusal.

    An error inside a stream arrives on the stream's 200 response, so its ``type`` decides."""
    if isinstance(exc, APIConnectionError):  # APITimeoutError included
        return classify(unreachable=True)
    if isinstance(exc, APIStatusError):
        return classify(status=exc.status_code, error_type=exc.type, headers=exc.response.headers)
    return None
```

Wrap the SDK calls. In `complete`:

```python
        with translated(refusal):
            msg = await self._client.messages.create(
                model=model, max_tokens=max_tokens, messages=cast(Any, convo), **extra
            )
```

In `stream`, put the whole `async with self._client.messages.stream(...) as stream:` block (through the final `yield ChatChunk(...)`) inside `with translated(refusal):`, re-indented one level.

- [ ] **Step 7: Translate in the OpenAI-compatible adapter**

In `app/llm/providers/openai_compat.py`, change the SDK import to
`from openai import APIConnectionError, APIError, APIStatusError, AsyncOpenAI`, add
`from app.llm.meter import ProviderUnavailable` and
`from app.llm.providers.failure import classify, translated`, and add after `_warn_if_truncated`:

```python
def refusal(exc: Exception) -> ProviderUnavailable | None:
    """What an OpenAI-compatible SDK error means to a learner (S49); ``None`` when it is not.

    An error event inside a stream (OpenRouter reports mid-generation failures this way) is a
    bare ``APIError`` with no HTTP status of its own — only a code in its body, when any."""
    if isinstance(exc, APIConnectionError):  # APITimeoutError included
        return classify(unreachable=True)
    if isinstance(exc, APIStatusError):
        return classify(status=exc.status_code, headers=exc.response.headers)
    if type(exc) is APIError:
        code = str(exc.code) if exc.code is not None else ""
        return classify(status=int(code) if code.isdigit() else 500)
    return None
```

Wrap: in `complete`, the `resp = await self._client.chat.completions.create(...)` call in `with translated(refusal):`; in `stream`, everything from `stream = await self._client.chat.completions.create(` through the end of the `async with stream:` block in one `with translated(refusal):` (the tool-call finalisation after it stays outside); in `embed`, the `resp = await self._client.embeddings.create(...)` call in `with translated(refusal):`.

- [ ] **Step 8: Let the fake refuse**

In `app/llm/providers/fake.py`, import `from app.llm.meter import ProviderUnavailable`, extend `__init__`:

```python
    def __init__(
        self,
        reply: str = "Hello from the fake tutor.",
        *,
        script: Sequence[FakeTurn] | None = None,
        refuse: ProviderUnavailable | None = None,
        refuse_calls: frozenset[str] = frozenset({"complete", "stream", "embed"}),
        refuse_after_words: int = 0,
    ) -> None:
```

storing `self._refuse = refuse`, `self._refuse_calls = refuse_calls`, `self._refuse_after_words = refuse_after_words` (comment: `# Test-only: behave like a provider that is busy or down (S49), on the calls named — a stream after refuse_after_words words.`), and add:

```python
    def _refusal(self, call: str) -> ProviderUnavailable | None:
        return self._refuse if call in self._refuse_calls else None
```

`complete` begins `if (refusal := self._refusal("complete")) is not None: raise refusal`; `embed` the same with `"embed"`. `stream` becomes:

```python
        turn = self._next_turn()
        refusal = self._refusal("stream")
        for i, word in enumerate(turn.text.split()):
            if refusal is not None and i == self._refuse_after_words:
                raise refusal
            yield ChatChunk(text=word if i == 0 else f" {word}")
        if refusal is not None:
            raise refusal
        yield ChatChunk(usage=self._usage(messages, turn), tool_calls=turn.tool_calls)
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provider_failures.py tests/test_meter.py tests/test_spend_guard.py tests/test_llm.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/llm/meter.py app/llm/providers/failure.py app/llm/providers/anthropic.py app/llm/providers/openai_compat.py app/llm/providers/fake.py app/core/config.py tests/eval/reliability/knobs.py tests/test_provider_failures.py
git commit -m "feat(llm): a provider that is busy or down is told apart from a bug [S49]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: Every refusal site catches `CallRefused`

**Files:**
- Modify: `app/services/turn_common.py` (`refusal_ends_turn`)
- Modify: `app/services/chat.py` (lines ~343 criteria, ~626 tutor), `app/services/agentic.py` (~128), `app/services/workflow.py` (~298), `app/services/onboarding.py` (~100)
- Modify: `app/memory/supersession.py` (~74), `app/learning/link_judge.py` (~86), `app/services/profile.py` (~215), `app/services/notes.py` (~439, ~526), `app/rag/pipeline.py` (~99)
- Modify: `app/workers/tasks.py` (~81, ~101, ~112)
- Test: `tests/test_provider_refusals.py` (create), `tests/test_notes_service.py` (add one test)

**Interfaces:**
- Consumes: `CallRefused`, `ProviderUnavailable`, `PROVIDER_MESSAGES` from `app.llm.meter` (Task 1); `FakeProvider(refuse=..., refuse_calls=..., refuse_after_words=...)` (Task 1).
- Produces: worker log lines `"{exc.reason}.deferred task=<name>"` — `budget.deferred …` unchanged for spend refusals, `provider.deferred …` for provider refusals.

Site rulings (each `except BudgetExceeded` below becomes `except CallRefused`, keeping its body): the four flows' `raise` (the turn ends with its reason); the criteria write's `return item, None` (the question stays open); supersession's and link-judge's `raise` (the task defers rather than recording "coexists"/"undecided"); profile's rollback-and-raise (the worker defers, watermark unmoved); notes render's `refused = True` (a fallback is shown but not cached, so an outage does not stand in for a real render for good); notes distill's `raise` (the learner is told why); the pipeline's re-raise (the source's error is the readable message, not "embedding failed after …"). Ruling against the spec's "the source fails": `ingestion._is_terminal` already treats a provider error — and a spend refusal — as transient, so the source goes back to `pending` and is retried up to `ingest_max_attempts`, then shows `failed` with the message; that is the existing, deliberate policy and is kept. Where `BudgetExceeded` is no longer referenced in a module, replace its import with `CallRefused`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_provider_refusals.py`:

```python
"""A provider refusal takes the path a spend refusal already takes (S49)."""

import contextlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ModelRole
from app.llm.meter import PROVIDER_MESSAGES, ProviderUnavailable
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.models.chat import Conversation, Message
from app.models.learner import Learner


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"r-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def test_a_reply_the_provider_drops_says_why(db_session: AsyncSession) -> None:
    """Generation's own error handler must not turn a refusal into "generation failed"."""
    from app.services import chat as chat_svc

    learner_id = await _learner(db_session)
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.commit()
    llm = _client(
        FakeProvider(
            refuse=ProviderUnavailable("down"),
            refuse_calls=frozenset({"stream"}),
            refuse_after_words=1,
        )
    )

    events = [
        e
        async for e in chat_svc.run_tutor_turn(
            db_session,
            llm,
            learner_id=learner_id,
            conversation=conversation,
            history=[],
            user_content="hello",
            max_tokens=200,
            source_ids=[],
        )
    ]

    assert [e.detail for e in events if e.type == "error"] == [PROVIDER_MESSAGES["down"]]


async def test_background_work_the_provider_refused_is_deferred(
    db_session: AsyncSession, monkeypatch
) -> None:
    from app.workers import tasks

    learner_id = await _learner(db_session)

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        tasks,
        "build_llm_client",
        lambda _s: _client(FakeProvider(refuse=ProviderUnavailable("busy", retry_after=5.0))),
    )
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.flush()
    # A message, so the interests estimator makes a model call.
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()
    logged: list[str] = []
    monkeypatch.setattr(tasks.logger, "info", lambda msg, *a: logged.append(msg % a))

    await tasks._profile_refresh_task(str(learner_id))  # must not raise

    assert logged == ["provider.deferred task=profile_refresh"]


async def test_an_ingestion_the_provider_refused_carries_the_message(
    db_session: AsyncSession,
) -> None:
    from app.models.source import SourceKind
    from app.services import ingestion
    from app.storage import InMemoryBlobStore

    learner_id = await _learner(db_session)
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner_id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=b"Photosynthesis turns light into chemical energy in the chloroplast.",
    )
    await db_session.commit()
    llm = _client(
        FakeProvider(refuse=ProviderUnavailable("down"), refuse_calls=frozenset({"embed"}))
    )

    result = await ingestion.ingest_source(db_session, store, llm, source.id)

    assert result is not None
    assert result.error == PROVIDER_MESSAGES["down"]
```

Append to `tests/test_notes_service.py`, after `test_a_refused_render_is_shown_but_not_kept` (import `ProviderUnavailable` beside `BudgetExceeded` at the top: `from app.llm.meter import BudgetExceeded, ProviderUnavailable`):

```python
class _DroppedRender(_BrokenRender):
    """Distils normally, then the provider is down for the render (S49)."""

    async def complete(self, **kwargs) -> ChatResponse:
        self._calls += 1
        if self._calls > 1:
            raise ProviderUnavailable("down")
        return await FakeProvider.complete(self, **kwargs)


async def test_a_render_the_provider_dropped_is_shown_but_not_kept(
    db_session: AsyncSession,
) -> None:
    """Cached, the fallback would stand in for a real render after the provider is back."""
    from app.models.note import NoteRender

    learner, topic, kc = await _seed(db_session)
    await _add_observation(db_session, learner, kc)

    view = await notes_svc.refresh_note(
        db_session, _client(_DroppedRender(ATOMS)), learner.id, topic
    )

    assert "Vectors add tip-to-tail." in (view.content_md or "")
    assert (await db_session.scalars(select(NoteRender))).all() == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_provider_refusals.py tests/test_notes_service.py -k "provider or dropped" -q -p no:cacheprovider`
Expected: 4 failed — the tutor reply says `generation failed`; the profile task raises `ProviderUnavailable` instead of logging; the source error starts `embedding failed after`; the dropped render is cached.

- [ ] **Step 3: `refusal_ends_turn` catches the base**

In `app/services/turn_common.py`, import `CallRefused` instead of `BudgetExceeded`, and in `guarded`:

```python
        except CallRefused as exc:
            yield TurnEvent(type="error", detail=exc.message)
```

Update its docstring's first line to: "A paid call refused — by the spend guard (S47) or by a provider that is busy or down (S49) — ends the turn with the reason."

- [ ] **Step 4: Switch every site listed in the rulings**

Mechanically, at each listed line: `except BudgetExceeded:` → `except CallRefused:`, body unchanged, and fix the module's import (`from app.llm.meter import CallRefused`, keeping `BudgetExceeded` only where the module still names it). In `app/rag/pipeline.py` the check is `if isinstance(failure, CallRefused): raise failure` with its comment amended to "Nor is a refusal (S47, S49): its message is the learner's reason, and the source shows it."

In `app/workers/tasks.py`, each of the three handlers becomes (shown for write-back; profile refresh and concept links follow the same form with their own task names):

```python
            except CallRefused as exc:
                # Deferred, not failed (S47, S49): the claim stays, so the sweep retries it later.
                logger.info("%s.deferred task=memory_write_back", exc.reason)
                return
```

Verify no `except BudgetExceeded` remains outside tests:

Run: `grep -rn "except BudgetExceeded" app`
Expected: no output.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provider_refusals.py tests/test_notes_service.py tests/test_spend_guard.py tests/test_check_criteria.py tests/test_chat_budget.py tests/test_memory_supersession.py -q -p no:cacheprovider`
Expected: all pass (the spend-guard tests still see `budget.deferred …` and the budget message).

- [ ] **Step 6: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/services/turn_common.py app/services/chat.py app/services/agentic.py app/services/workflow.py app/services/onboarding.py app/memory/supersession.py app/learning/link_judge.py app/services/profile.py app/services/notes.py app/rag/pipeline.py app/workers/tasks.py tests/test_provider_refusals.py tests/test_notes_service.py
git commit -m "feat(llm): a provider refusal takes the path a spend refusal takes [S49]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: What the learner is told — 503, and the coded turn error

**Files:**
- Modify: `app/main.py` (handler)
- Modify: `app/services/turn_common.py` (`TurnEvent.code`, `TurnEvent.retry_after`, `error_frame`)
- Modify: `app/api/v1/chat.py` (error branch, ~line 720), `app/api/v1/onboarding.py` (error branch, ~line 122)
- Test: `tests/test_provider_refusals.py` (add), `tests/test_onboarding_api.py` (add one test)

**Interfaces:**
- Consumes: `CallRefused`, `ProviderUnavailable` (Task 1); `refusal_ends_turn` catching `CallRefused` (Task 2).
- Produces: `TurnEvent(..., code: str | None = None, retry_after: float | None = None)`; `error_frame(ev: TurnEvent) -> dict[str, Any]` in `app.services.turn_common` — `{"type": "error", "detail": ev.detail}` plus `"code"` and `"retry_after"` when `ev.code` is set. SSE error frames may now carry `code` and `retry_after` (Task 4 reads them).

Ruling carried by this task: the chat route records a failed turn's error as `ev.code or ev.detail`, so a spend-refused turn now records `budget_exceeded` rather than the sentence (a code is what `turns.error` holds for `deadline` too); the learner-facing detail is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_provider_refusals.py` (add imports at the top: `import json`, `from collections.abc import AsyncIterator, Iterator`, `import pytest`, `from httpx import AsyncClient`, `from sqlalchemy import select`, `from app.api.deps import get_llm_client`, `from app.main import app`, `from app.models.chat import Turn, TurnStatus`, `from app.llm.registry import fake_llm_client`, `from app.services.turn_common import TurnEvent, refusal_ends_turn`):

```python
API = "/api/v1"


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("A whole reply.")
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(body: str) -> list[dict]:
    return [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]


async def _conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    cid = (await api_client.post(f"{API}/conversations", json={"title": "Calc"})).json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(cid))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return cid


def _busy_flow():
    """Streams a word, then the provider says it is busy."""

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Half")
        raise ProviderUnavailable("busy", retry_after=7.0)

    return refusal_ends_turn(flow)


async def test_a_busy_provider_ends_the_turn_saying_so(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _busy_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {
        "type": "error",
        "detail": PROVIDER_MESSAGES["busy"],
        "code": "provider_busy",
        "retry_after": 7.0,
    }
    turn = (
        await db_session.scalars(select(Turn).where(Turn.conversation_id == uuid.UUID(cid)))
    ).one()
    assert (turn.status, turn.error) == (TurnStatus.FAILED, "provider_busy")
    replies = await db_session.scalars(
        select(Message).where(
            Message.conversation_id == uuid.UUID(cid), Message.role == "assistant"
        )
    )
    assert replies.all() == []  # the half-written reply is not kept


async def test_a_turn_the_provider_refused_can_be_retried(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    key = str(uuid.uuid4())
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _busy_flow())
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )
    monkeypatch.undo()

    r = await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )

    assert _sse(r.text)[-1]["type"] == "done"


async def test_a_route_over_a_busy_provider_answers_503_with_the_wait(
    api_client: AsyncClient, api_learner: Learner, db_session: AsyncSession
) -> None:
    conversation = Conversation(learner_id=api_learner.id)
    db_session.add(conversation)
    await db_session.flush()
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()
    app.dependency_overrides[get_llm_client] = lambda: _client(
        FakeProvider(refuse=ProviderUnavailable("busy", retry_after=7.5))
    )
    try:
        r = await api_client.post(f"{API}/profile/refresh")
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert r.status_code == 503
    assert r.headers["retry-after"] == "8"
    assert r.json() == {
        "detail": {
            "code": "provider_busy",
            "message": PROVIDER_MESSAGES["busy"],
            "retry_after": 7.5,
        }
    }
```

Append to `tests/test_onboarding_api.py`:

```python
async def test_a_goal_turn_the_provider_refused_says_so(
    api_client: AsyncClient, fake_llm_goal: None, monkeypatch
) -> None:
    """The refinement stream carries the refusal's code, like chat (S49)."""
    from app.llm.meter import PROVIDER_MESSAGES, ProviderUnavailable
    from app.services.turn_common import refusal_ends_turn

    @refusal_ends_turn
    async def down(**kwargs):
        raise ProviderUnavailable("down")
        yield  # an async generator

    monkeypatch.setattr("app.services.onboarding.run_goal_refinement_turn", down)
    session_id = (await api_client.post(f"{API}/onboarding/goal-sessions")).json()["session_id"]

    response = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={"session_id": session_id, "content": "x", "satisfied": False, "mode": "start"},
    )

    assert _parse_sse(response.text)[-1] == {
        "type": "error",
        "detail": PROVIDER_MESSAGES["down"],
        "code": "provider_down",
        "retry_after": None,
    }
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_provider_refusals.py tests/test_onboarding_api.py -k "busy or refused or 503" -q -p no:cacheprovider`
Expected: failures — the SSE frames lack `code`/`retry_after`, the turn's error is the sentence, and the route answers 500.

- [ ] **Step 3: The 503 handler**

In `app/main.py`, import `math` and `ProviderUnavailable` (`from app.llm.meter import BudgetExceeded, ProviderUnavailable`), and add after `_budget_exceeded`:

```python
@app.exception_handler(ProviderUnavailable)
async def _provider_unavailable(_request: Request, exc: ProviderUnavailable) -> JSONResponse:
    """A provider busy or out of reach after its own retries (S49) — one shape for every route."""
    headers = (
        {"Retry-After": str(math.ceil(exc.retry_after))} if exc.retry_after is not None else None
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": {"code": exc.code, "message": exc.message, "retry_after": exc.retry_after}
        },
        headers=headers,
    )
```

- [ ] **Step 4: The coded turn error**

In `app/services/turn_common.py`, add two fields to `TurnEvent` after `check_result`:

```python
    # Set on an "error" a refusal produced (S47, S49): what refused, and — for a busy
    # provider — how long to wait before trying again.
    code: str | None = None
    retry_after: float | None = None
```

In `refusal_ends_turn`, yield
`TurnEvent(type="error", detail=exc.message, code=exc.code, retry_after=exc.retry_after)`,
and add below `refusal_ends_turn`:

```python
def error_frame(ev: TurnEvent) -> dict[str, Any]:
    """The SSE frame for an ``error`` event; a refusal's code and wait ride along (S49)."""
    frame: dict[str, Any] = {"type": "error", "detail": ev.detail}
    if ev.code is not None:
        frame |= {"code": ev.code, "retry_after": ev.retry_after}
    return frame
```

(import `Any` from `typing` if the module lacks it).

In `app/api/v1/chat.py`, the error branch of `event_stream` becomes:

```python
                elif ev.type == "error":
                    outcome, error = TurnStatus.FAILED, ev.code or ev.detail
                    yield _sse(error_frame(ev))
```

and in `app/api/v1/onboarding.py`:

```python
                    elif event.type == "error":
                        yield _sse(error_frame(event))
```

importing `error_frame` from `app.services.turn_common` in both.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_provider_refusals.py tests/test_onboarding_api.py tests/test_spend_guard.py tests/test_turn_stop.py tests/test_chat.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
uv run poe format && uv run poe lint && uv run poe type-check
git add app/main.py app/services/turn_common.py app/api/v1/chat.py app/api/v1/onboarding.py tests/test_provider_refusals.py tests/test_onboarding_api.py
git commit -m "feat(api): a busy or unreachable provider answers 503 and says so mid-turn [S49]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: Chat waits out a busy provider before offering Retry

**Files:**
- Modify: `frontend/src/api/sse.ts` (error event type)
- Modify: `frontend/src/hooks/useChatConversation.ts` (`retryAfter`)
- Create: `frontend/src/components/chat/TurnError.tsx`, `frontend/src/components/chat/TurnError.test.tsx`
- Modify: `frontend/src/pages/Chat.tsx`, `frontend/src/pages/Session.tsx` (use `TurnError`)
- Modify: `frontend/src/hooks/useChatConversation.test.tsx` (add a test)

**Interfaces:**
- Consumes: SSE error frames `{type: "error", detail, code?, retry_after?}` (Task 3).
- Produces: `useChatConversation(...).retryAfter: number | null` — seconds to wait after a `provider_busy` error, else `null`; `TurnError({ error, canRetry, retryAfter, onRetry })`.

- [ ] **Step 1: Write the failing tests**

Create `frontend/src/components/chat/TurnError.test.tsx`:

```tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { TurnError } from "./TurnError";

describe("TurnError", () => {
  afterEach(() => vi.useRealTimers());

  it("waits out a busy provider before offering to try again", () => {
    vi.useFakeTimers();
    render(<TurnError error="The tutor is busy" canRetry retryAfter={3} onRetry={vi.fn()} />);

    expect(screen.getByRole("button", { name: "Try again in 3s" })).toBeDisabled();
    for (let i = 0; i < 3; i++) act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("offers to try again at once when there is nothing to wait for", () => {
    const onRetry = vi.fn();
    render(<TurnError error="Unreachable" canRetry retryAfter={null} onRetry={onRetry} />);

    screen.getByRole("button", { name: "Try again" }).click();
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
```

In `frontend/src/hooks/useChatConversation.test.tsx`, make the mocked stream also end on an error (`if (ev.type === "stopped" || ev.type === "error") return;`) and add inside the `describe`:

```tsx
  it("remembers how long a busy provider asked to wait", async () => {
    const { result } = renderHook(() => useChatConversation("c-1"), { wrapper });

    let sending: Promise<void> = Promise.resolve();
    act(() => {
      sending = result.current.send("Hi", { mode: "chat" });
    });
    await nextFrame({ type: "turn", turn_id: "t-2" });
    await nextFrame({
      type: "error",
      detail: "The tutor is busy",
      code: "provider_busy",
      retry_after: 7,
    });
    await act(() => sending);

    expect(result.current.error).toBe("The tutor is busy");
    expect(result.current.retryAfter).toBe(7);
  });
```

- [ ] **Step 2: Run them to verify they fail**

Run (in `frontend/`): `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/chat/TurnError.test.tsx src/hooks/useChatConversation.test.tsx`
Expected: FAIL — `TurnError` cannot be resolved; `retryAfter` is `undefined`.

- [ ] **Step 3: Implement**

`frontend/src/api/sse.ts`: the error member of `TurnEvent` becomes
`| { type: "error"; detail: string; code?: string; retry_after?: number | null }`.

`frontend/src/hooks/useChatConversation.ts`:
- state `const [retryAfter, setRetryAfter] = useState<number | null>(null);` beside `error`, with the comment `// Seconds a busy provider asked for before the retry (S49); null when nothing to wait for.`
- in `run`, beside `setError(null)`: `setRetryAfter(null);`
- the error branch:

```ts
          } else if (ev.type === "error") {
            setError(ev.detail);
            setFailed(turn);
            setRetryAfter(ev.code === "provider_busy" ? (ev.retry_after ?? null) : null);
```

- return `retryAfter` beside `error`.

Create `frontend/src/components/chat/TurnError.tsx`:

```tsx
import { useEffect, useState } from "react";

/** A turn's error line and its Try again (S51). After a busy provider (S49) the button waits
 * out the time the provider asked for, counting down, rather than sending a retry that would
 * only be refused again. Mounted per error, so each error starts its own countdown. */
export function TurnError({
  error,
  canRetry,
  retryAfter,
  onRetry,
}: {
  error: string;
  canRetry: boolean;
  retryAfter: number | null;
  onRetry: () => void;
}) {
  const [remaining, setRemaining] = useState(() => Math.ceil(retryAfter ?? 0));
  useEffect(() => {
    if (remaining <= 0) return;
    const timer = setTimeout(() => setRemaining((s) => s - 1), 1000);
    return () => clearTimeout(timer);
  }, [remaining]);

  return (
    <div className="text-caption text-error mx-auto flex w-full max-w-3xl items-center gap-3 px-6 pb-2">
      <p>{error}</p>
      {canRetry && (
        <button
          type="button"
          className="btn btn-ghost btn-xs"
          disabled={remaining > 0}
          onClick={onRetry}
        >
          {remaining > 0 ? `Try again in ${remaining}s` : "Try again"}
        </button>
      )}
    </div>
  );
}
```

In `frontend/src/pages/Chat.tsx` and `frontend/src/pages/Session.tsx`, destructure `retryAfter` from the hook and replace each `{error && (<div …>…Try again…</div>)}` block with:

```tsx
        {error && (
          <TurnError
            error={error}
            canRetry={canRetry}
            retryAfter={retryAfter}
            onRetry={() => void retry()}
          />
        )}
```

importing `TurnError` from `../components/chat/TurnError`.

- [ ] **Step 4: Run the checks**

Run (in `frontend/`): `npx prettier --write src/api/sse.ts src/hooks/useChatConversation.ts src/hooks/useChatConversation.test.tsx src/components/chat/TurnError.tsx src/components/chat/TurnError.test.tsx src/pages/Chat.tsx src/pages/Session.tsx && npm run build && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run lint`
Expected: build succeeds, all vitest files pass, lint clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/sse.ts frontend/src/hooks/useChatConversation.ts frontend/src/hooks/useChatConversation.test.tsx frontend/src/components/chat/TurnError.tsx frontend/src/components/chat/TurnError.test.tsx frontend/src/pages/Chat.tsx frontend/src/pages/Session.tsx
git commit -m "feat(chat): after a busy provider, Try again waits out the time it asked for [S49]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Record it

**Files:**
- Modify: `docs/guru-suggestions-tracker.md`, `docs/RUNBOOK.md` (§18), `CLAUDE.md`

- [ ] **Step 1: Tracker**
  - Remove the `S49` row from the Workstream 5 live table; add to Completed, after `S48`:
    `| S49 | Provider failure experience | Registry validated at startup, explicit transport limits (earlier). A provider that is busy or down after the SDK's retries is a refusal: 503 with Retry-After on routes, a coded error ending a turn (no partial reply kept, same-id retry regenerates), background work deferred, spend rows record provider_busy/provider_down; chat's Try again waits out a busy provider. | S18 (the default wait) | [Failure classification](../app/llm/providers/failure.py), [design](superpowers/specs/2026-09-30-provider-failures-design.md), [RUNBOOK §18](RUNBOOK.md#18-spend-limits-s47-s48) |`
  - At a glance: Live v0 count 18 → 17 and drop `S49` from its IDs; Done 47 → 48; "Next up" becomes `**Next up: workstream 5** — S37, then S17, S62, S53.`
  - S18's Remaining: after "the turn and request deadlines" add ", the provider retry wait".

- [ ] **Step 2: RUNBOOK §18** — add after the "**Deadlines and Stop.**" bullet:

```markdown
- **Provider failures (S49).** After the SDK's own retries (`GURU_LLM_MAX_RETRIES`), a 429 is
  `provider_busy` and a 5xx, 529 overload, connection error or timeout is `provider_down`
  (`app/llm/providers/failure.py`); anything else — a bad key, a bad request — stays an error
  and a 500. A route answers 503 `{"detail": {"code", "message", "retry_after"}}` with
  `Retry-After` when busy (the provider's own wait, else `GURU_PROVIDER_RETRY_AFTER_SECONDS`,
  20, uncalibrated — S18); a turn ends with that error, keeps no partial reply, and is `failed`
  with the code, so the same `client_turn_id` regenerates; background work logs
  `provider.deferred task=…` and the sweep retries; an ingestion stays retryable with the
  message as its error. Spend rows record `error_kind` `provider_busy`/`provider_down`, so
  outages count apart from bugs.
```

- [ ] **Step 3: CLAUDE.md** — in the "Every paid call is recorded and admitted by the client" bullet, after "any other request answers 504 past its own." insert: "A provider that is busy or down is a refusal too (`CallRefused`): 503, or a coded turn error, never 'generation failed' (S49)."

- [ ] **Step 4: Commit**

```bash
git add docs/guru-suggestions-tracker.md docs/RUNBOOK.md CLAUDE.md
git commit -m "docs: record provider rate-limit and outage responses [S49]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

- [ ] **Step 5: Whole gate**

Run: `uv run poe check && uv run poe format-check`
Expected: all pass.
