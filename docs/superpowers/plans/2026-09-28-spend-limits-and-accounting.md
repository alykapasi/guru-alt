# Spend Limits and Call Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `LLMClient` records every model call itself — pending, ok, failed or partial, with feature, request, prompt hash and version — and refuses any call that would break the learner's daily caps (exact under concurrency) or the deployment ceiling (background work paused at 90%).

**Architecture:** An attribution context (`app/llm/attribution.py`) carries learner, conversation, feature, request and background through `contextvars`. A meter (`app/llm/meter.py`) wraps each provider call: it asks the spend guard (`app/services/spend_guard.py`) for admission, writes a `pending` row on the independent accounting session, and settles it afterwards. The 29 hand-written `log_llm_call` sites become `@metered(...)` decorators or `attributed(...)` blocks.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, PostgreSQL advisory locks, Alembic, pytest; React + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-28-spend-limits-and-accounting-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`; API changes also `uv run poe api-types`, stage `frontend/src/api/schema.d.ts`, then `uv run poe api-contract`.
- Frontend changes also green on `cd frontend && npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint`.
- New migration → `uv run python -m tests.testdb`. Alembic revision ids ≤ 32 characters.
- `llm_calls.created_at` is naive UTC; compare with `datetime.now(UTC).replace(tzinfo=None)`.
- Accounting never fails the work: a failed accounting read or write is logged (`llm.call_not_recorded`) and the call proceeds. `BudgetExceeded` is the only exception the meter raises on its own.
- `error_kind` is the exception's class name only — never its message.
- Learner-facing messages, verbatim: learner scope "You've reached today's usage limit. It resets over the next 24 hours."; deployment scope "Guru has reached its usage limit for now — try again later."
- Background thresholds: 90% of each learner cap and of `spend_budget_usd`. `spend_guard_cache_seconds = 30`. Stale pending alert after 15 minutes.
- One tracker id per commit subject: `[S47]` or `[S48]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls: tests use `fake_llm_client` / `FakeProvider`.

## Review Focus

1. Two requests for one learner in parallel near the cap: the advisory lock serialises admission, so the second sees the first's `pending` reservation (Task 3 test uses an open stream to hold a reservation; true two-connection concurrency is for the reviewer to reason about).
2. A stream the consumer abandons mid-way (disconnect or `aclose()`): the row ends `partial`, never stays `pending` (Task 2 test).
3. A refusal in the middle of a streaming turn: the learner sees the budget message in the error event, not "generation failed" (Task 5 test).
4. A background job refused at 90%: logged as deferred, its claim kept, no exception noise (Task 5 test).
5. A paid call made during an API request with no feature: the attribution guard fails the test that made it (Task 4 fixture).

---

### Task 1: Schema and the attribution context [S48]

**Files:**
- Create: `db/migrations/versions/0071_call_accounting.py`, `app/llm/attribution.py`, `tests/test_attribution.py`
- Modify: `app/models/chat.py` (`LLMCall`), `app/core/config.py` (`app_version`, `spend_guard_cache_seconds`), `tests/test_migrations_with_data.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True) class Attribution:
      learner_id: uuid.UUID | None = None; conversation_id: uuid.UUID | None = None
      feature: str | None = None; request_id: str | None = None; background: bool = False
  def current() -> Attribution
  @contextmanager def attributed(**fields) -> Iterator[Attribution]
  def bind(**fields) -> None  # set for the rest of the current task (request dependencies)
  def metered(feature: str, *, learner: str | None = None, conversation: str | None = None, background: bool | None = None)  # decorator for async functions and async generators
  ```
  `LLMCall` gains `feature: str` (default `"legacy"` in the database, `"unattributed"` from the meter when none), `request_id: str | None`, `status: str` (`pending|ok|failed|partial`), `error_kind: str | None`, `estimated: bool`, `prompt_hash: str | None`, `app_version: str | None`.

- [ ] **Step 1: Failing tests** — `tests/test_attribution.py`:

```python
"""Who a model call is for and what it is for travels with the work, not through 29 call sites."""

import uuid

from app.llm.attribution import attributed, current, metered


def test_nesting_overrides_only_what_it_names() -> None:
    learner = uuid.uuid4()
    with attributed(learner_id=learner, feature="chat_turn", request_id="r1"):
        with attributed(feature="practice_grading"):
            inner = current()
        outer = current()
    assert (inner.learner_id, inner.feature, inner.request_id) == (
        learner,
        "practice_grading",
        "r1",
    )
    assert outer.feature == "chat_turn"
    assert current().feature is None


async def test_metered_reads_ids_from_the_arguments() -> None:
    learner, conversation_id = uuid.uuid4(), uuid.uuid4()

    class Conversation:
        id = conversation_id

    @metered("chat_turn", learner="learner_id", conversation="conversation.id")
    async def turn(*, learner_id: uuid.UUID, conversation: object) -> tuple:
        seen = current()
        return seen.feature, seen.learner_id, seen.conversation_id

    assert await turn(learner_id=learner, conversation=Conversation()) == (
        "chat_turn",
        learner,
        conversation_id,
    )
    assert current().feature is None


async def test_metered_wraps_an_async_generator() -> None:
    @metered("goal_refinement")
    async def events():
        yield current().feature
        yield current().feature

    assert [e async for e in events()] == ["goal_refinement", "goal_refinement"]
    assert current().feature is None
```

Append to `tests/test_migrations_with_data.py` (match the 0070 test's helpers):

```python
async def test_existing_calls_become_settled_legacy_rows() -> None:
    """0071 (S48): rows written before attribution existed are settled and unattributed."""
    async with database_at("0070_refresh_scheduling") as connect:
        conn = await connect()
        try:
            call_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO llm_calls (id, role, provider, model, input_tokens, output_tokens) "
                "VALUES ($1, 'fast', 'fake', 'fake-1', 3, 4)",
                call_id,
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0071_call_accounting")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT feature, status, estimated FROM llm_calls WHERE id = $1", call_id
            )
            assert (row["feature"], row["status"], row["estimated"]) == ("legacy", "ok", False)
        finally:
            await conn.close()
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_attribution.py tests/test_migrations_with_data.py -q -k "attribution or nesting or metered or legacy"` → FAIL (module missing; no revision).

- [ ] **Step 3: Implement.**

`app/llm/attribution.py`:

```python
"""Who a model call is for, and what it is for (S48).

Every call used to be recorded by hand at the call site, 29 of them, each remembering to pass
the learner and forgetting everything else — which feature paid, which request. The client now
records every call itself and reads the answer from here: entry points set the learner and the
request, the service doing the work sets the feature, and a nested ``attributed`` overrides only
what it names.

Restored by saving and re-setting the previous value rather than with a ``ContextVar`` token:
an async generator can be resumed from a context other than the one that set its token, and a
token reset there raises. One consequence is accepted: while a metered generator is suspended
at a ``yield``, its caller sees the generator's attribution. The caller is the route streaming
the events, which is the same work.
"""

import functools
import inspect
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class Attribution:
    learner_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    feature: str | None = None
    request_id: str | None = None
    background: bool = False


_current: ContextVar[Attribution] = ContextVar("llm_attribution", default=Attribution())


def current() -> Attribution:
    return _current.get()


@contextmanager
def attributed(**fields: Any) -> Iterator[Attribution]:
    """Override the named fields for the duration of the block."""
    previous = _current.get()
    value = replace(previous, **fields)
    _current.set(value)
    try:
        yield value
    finally:
        _current.set(previous)


def bind(**fields: Any) -> None:
    """Set fields for the rest of the current task, with no restore.

    For a request dependency: it returns before the endpoint runs, so a ``with`` block would
    already have ended. Each request runs in its own task with its own copy of the context,
    so nothing set here outlives the request.
    """
    _current.set(replace(_current.get(), **fields))


def _pick(bound: dict[str, Any], path: str | None) -> Any:
    if path is None:
        return None
    name, _, attribute = path.partition(".")
    value = bound.get(name)
    return getattr(value, attribute) if attribute and value is not None else value


def metered(
    feature: str,
    *,
    learner: str | None = None,
    conversation: str | None = None,
    background: bool | None = None,
) -> Callable:
    """Attribute every model call made inside the decorated function.

    ``learner`` / ``conversation`` name an argument, optionally with one attribute
    (``"conversation.id"``, ``"source.learner_id"``); omitted, they are inherited.
    """

    def decorate(fn: Callable) -> Callable:
        signature = inspect.signature(fn)

        def fields(args: tuple, kwargs: dict) -> dict[str, Any]:
            bound = signature.bind_partial(*args, **kwargs).arguments
            out: dict[str, Any] = {"feature": feature}
            if learner is not None:
                out["learner_id"] = _pick(bound, learner)
            if conversation is not None:
                out["conversation_id"] = _pick(bound, conversation)
            if background is not None:
                out["background"] = background
            return out

        if inspect.isasyncgenfunction(fn):

            @functools.wraps(fn)
            async def generator(*args: Any, **kwargs: Any):
                with attributed(**fields(args, kwargs)):
                    async for item in fn(*args, **kwargs):
                        yield item

            return generator

        @functools.wraps(fn)
        async def coroutine(*args: Any, **kwargs: Any) -> Any:
            with attributed(**fields(args, kwargs)):
                return await fn(*args, **kwargs)

        return coroutine

    return decorate
```

Migration `0071_call_accounting.py` (`down_revision = "0070_refresh_scheduling"`):

```python
"""Complete call accounting (S48): what each call was for, and how it ended.

A row is now written *before* the call (``pending``, carrying a reserved estimate the spend
guard counts) and settled after (``ok``, ``failed``, ``partial``). Existing rows were only ever
written after success, so they become ``ok``, attributed to no feature (``legacy``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0071_call_accounting"
down_revision: str | Sequence[str] | None = "0070_refresh_scheduling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_calls", sa.Column("feature", sa.Text(), nullable=False, server_default="legacy")
    )
    op.add_column("llm_calls", sa.Column("request_id", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls", sa.Column("status", sa.Text(), nullable=False, server_default="ok")
    )
    op.add_column("llm_calls", sa.Column("error_kind", sa.Text(), nullable=True))
    op.add_column(
        "llm_calls",
        sa.Column("estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("llm_calls", sa.Column("prompt_hash", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("app_version", sa.Text(), nullable=True))
    op.create_index("ix_llm_calls_learner_created", "llm_calls", ["learner_id", "created_at"])
    op.create_index("ix_llm_calls_status", "llm_calls", ["status"])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_status", table_name="llm_calls")
    op.drop_index("ix_llm_calls_learner_created", table_name="llm_calls")
    for column in (
        "app_version",
        "prompt_hash",
        "estimated",
        "error_kind",
        "status",
        "request_id",
        "feature",
    ):
        op.drop_column("llm_calls", column)
```

`LLMCall` (after `first_token_ms`), with a short comment each:

```python
    # What paid for it (S48): set by the service doing the work, via app.llm.attribution.
    feature: Mapped[str] = mapped_column(server_default="legacy", default="unattributed")
    request_id: Mapped[str | None] = mapped_column(default=None)
    # pending (reserved, not yet answered) | ok | failed | partial (stream closed early).
    status: Mapped[str] = mapped_column(server_default="ok", default="ok")
    error_kind: Mapped[str | None] = mapped_column(default=None)
    # True while the tokens and cost are the reservation's estimate rather than the provider's.
    estimated: Mapped[bool] = mapped_column(server_default=false(), default=False)
    prompt_hash: Mapped[str | None] = mapped_column(default=None)
    app_version: Mapped[str | None] = mapped_column(default=None)
```

Import `false` from `sqlalchemy`. (No index on `feature`: reports group a 24-hour window that the `created_at` index already narrows.)

Settings, beside `spend_budget_usd`:

```python
    # Recorded on every model call (S48) so a change in cost or quality can be tied to a
    # release. Set by the deployment; "dev" otherwise.
    app_version: str = "dev"
    # How stale the deployment's spend total may be when the guard reads it (S47): summing the
    # whole window on every call would cost more than the calls it protects.
    spend_guard_cache_seconds: int = 30
```

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; the new tests PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0071_call_accounting.py app/llm/attribution.py app/models/chat.py app/core/config.py tests/test_attribution.py tests/test_migrations_with_data.py
git status
git commit -m "feat(llm): an attribution context and accounting columns [S48]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The client records every call itself [S48]

**Files:**
- Create: `app/llm/meter.py`, `tests/test_meter.py`
- Modify: `app/llm/registry.py` (`LLMClient.complete/stream/embed`), `app/services/llm_log.py` (becomes the accounting-session home only), `tests/test_llm_log.py`

**Interfaces:**
- Consumes: `attribution.current()` (Task 1).
- Produces:
  ```python
  class BudgetExceeded(Exception):  # app/llm/meter.py
      scope: Literal["learner", "deployment"]; message: str
  @dataclass(frozen=True) class Estimate: input_tokens: int; output_tokens: int; cost_usd: float | None
  @dataclass(frozen=True) class Reservation: row_id: uuid.UUID | None; provider: str; model: str; input_tokens: int
  async def open_call(role: str, provider: str, model: str, *, system: str | None, input_chars: int, max_output: int) -> Reservation
  async def settle(reservation: Reservation, usage: Usage) -> None
  async def fail(reservation: Reservation, exc: BaseException) -> None
  async def partial(reservation: Reservation, delivered_chars: int) -> None
  ADMIT: Callable[..., Awaitable[None]] | None  # set by Task 3; None admits everything
  ```
  `app.services.llm_log` keeps `set_accounting_session_factory` and `accounting_session` (conftest and `decision_log` import them) and loses `log_llm_call` in Task 4.

- [ ] **Step 1: Failing tests** — `tests/test_meter.py`:

```python
"""The client records every model call itself: pending, then ok, failed or partial (S48)."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole
from app.llm.attribution import attributed
from app.llm.providers.fake import FakeProvider
from app.llm.registry import ModelSpec, fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner

HELLO = [ChatMessage(role=ChatRole.USER, content="hello there")]


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"m-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def _rows(session: AsyncSession, learner_id: uuid.UUID) -> list[LLMCall]:
    return list(
        (
            await session.scalars(
                select(LLMCall)
                .where(LLMCall.learner_id == learner_id)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def _client(provider: FakeProvider) -> LLMClient:
    return LLMClient({"fake": provider}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


async def test_a_completion_is_recorded_with_what_paid_for_it(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn", request_id="req-1"):
        await fake_llm_client("hi").complete(ModelRole.FAST, HELLO, system="Be brief.")

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.feature, row.request_id, row.estimated) == (
        "ok",
        "chat_turn",
        "req-1",
        False,
    )
    assert row.prompt_hash is not None and len(row.prompt_hash) == 16
    assert row.app_version == "dev"


async def test_an_unattributed_call_says_so(db_session: AsyncSession) -> None:
    await fake_llm_client("hi").complete(ModelRole.FAST, HELLO)
    row = await db_session.scalar(
        select(LLMCall).order_by(LLMCall.created_at.desc()).limit(1)
    )
    assert row is not None and row.feature == "unattributed"


async def test_a_failed_call_is_recorded_without_its_message(db_session: AsyncSession) -> None:
    class Down(FakeProvider):
        async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
            raise TimeoutError("learner said something private")

    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn"), pytest.raises(TimeoutError):
        await _client(Down()).complete(ModelRole.FAST, HELLO)

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.error_kind, row.estimated, row.output_tokens) == (
        "failed",
        "TimeoutError",
        True,
        0,
    )
    assert row.input_tokens > 0


async def test_a_stream_closed_early_is_partial(db_session: AsyncSession) -> None:
    """Review focus 2: an abandoned stream must not stay pending."""
    learner_id = await _learner(db_session)
    llm = fake_llm_client("one two three four five six")
    with attributed(learner_id=learner_id, feature="chat_turn"):
        stream = llm.stream(ModelRole.SMART, HELLO)
        seen = [await anext(stream), await anext(stream)]
        await stream.aclose()

    assert len(seen) == 2
    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.estimated) == ("partial", True)
    assert row.output_tokens > 0


async def test_a_finished_stream_is_ok(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="chat_turn"):
        async for _chunk in fake_llm_client("one two").stream(ModelRole.SMART, HELLO):
            pass

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.estimated) == ("ok", False)
    assert row.first_token_ms is not None


async def test_an_embedding_is_recorded(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    with attributed(learner_id=learner_id, feature="ingestion"):
        await fake_llm_client().embed(ModelRole.EMBED, ["a", "b"])

    [row] = await _rows(db_session, learner_id)
    assert (row.status, row.feature, row.output_tokens) == ("ok", "ingestion", 0)


async def test_broken_accounting_does_not_fail_the_call() -> None:
    from contextlib import asynccontextmanager

    from app.services.llm_log import set_accounting_session_factory

    @asynccontextmanager
    async def broken():
        raise RuntimeError("accounting database down")
        yield  # pragma: no cover

    previous = set_accounting_session_factory(broken)
    try:
        reply = await fake_llm_client("still answered").complete(ModelRole.FAST, HELLO)
    finally:
        set_accounting_session_factory(previous)
    assert reply.content == "still answered"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_meter.py -q` → FAIL (no rows are written by the client).

- [ ] **Step 3: Implement.**

`app/llm/meter.py`:

```python
"""Every model call is recorded by the client, before and after (S48), and admitted first (S47).

Before: the spend guard (``ADMIT``, installed by ``app.services.spend_guard``) may refuse the
call, and a ``pending`` row is written carrying a reserved estimate — so a call that never
comes back still counts, and concurrent calls see each other. After: the row is settled as
``ok`` with the provider's numbers, ``failed`` with the exception's class name (its message may
contain learner text), or ``partial`` for a stream closed before its last chunk.

Accounting is written on its own session and never fails the work: a failed write is logged
and the call proceeds. ``BudgetExceeded`` is the one exception raised here on purpose.
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import update

from app.core.config import get_settings
from app.llm.attribution import Attribution, current
from app.llm.pricing import price_usd
from app.llm.types import Usage

log = structlog.get_logger(__name__)

Scope = Literal["learner", "deployment"]
MESSAGES: dict[str, str] = {
    "learner": "You've reached today's usage limit. It resets over the next 24 hours.",
    "deployment": "Guru has reached its usage limit for now — try again later.",
}


class BudgetExceeded(Exception):
    """A paid call refused before it was made (S47)."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(MESSAGES[scope])
        self.scope: Scope = scope
        self.message = MESSAGES[scope]


@dataclass(frozen=True)
class Estimate:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


# (session, attribution, estimate) -> None, raising BudgetExceeded. Installed by
# app.services.spend_guard at import; None admits everything (a process that never imported
# the guard, e.g. a bare script).
Admit = Callable[..., Awaitable[None]]
ADMIT: Admit | None = None


@dataclass(frozen=True)
class Reservation:
    row_id: uuid.UUID | None
    provider: str
    model: str
    input_tokens: int


def _hash(system: str | None) -> str | None:
    return hashlib.sha256(system.encode()).hexdigest()[:16] if system else None


def _estimate(provider: str, model: str, *, input_chars: int, max_output: int) -> Estimate:
    input_tokens = max(1, input_chars // 4)
    usage = Usage(input_tokens=input_tokens, output_tokens=max_output)
    return Estimate(input_tokens, max_output, price_usd(provider, model, usage))


async def open_call(
    role: str,
    provider: str,
    model: str,
    *,
    system: str | None,
    input_chars: int,
    max_output: int,
) -> Reservation:
    """Admit the call and write its pending row. Raises ``BudgetExceeded`` when refused."""
    from app.models.chat import LLMCall
    from app.services.llm_log import accounting_session

    who: Attribution = current()
    estimate = _estimate(provider, model, input_chars=input_chars, max_output=max_output)
    row_id = uuid.uuid4()
    try:
        async with accounting_session() as session:
            if ADMIT is not None:
                await ADMIT(session, who, estimate)
            session.add(
                LLMCall(
                    id=row_id,
                    learner_id=who.learner_id,
                    conversation_id=who.conversation_id,
                    role=role,
                    provider=provider,
                    model=model,
                    input_tokens=estimate.input_tokens,
                    output_tokens=estimate.output_tokens,
                    cost_usd=estimate.cost_usd,
                    feature=who.feature or "unattributed",
                    request_id=who.request_id,
                    status="pending",
                    estimated=True,
                    prompt_hash=_hash(system),
                    app_version=get_settings().app_version,
                )
            )
            await session.commit()
    except BudgetExceeded:
        raise
    except Exception as exc:  # bookkeeping must not become an outage
        log.error("llm.call_not_recorded", role=role, model=model, error=type(exc).__name__)
        return Reservation(None, provider, model, estimate.input_tokens)
    return Reservation(row_id, provider, model, estimate.input_tokens)


async def _update(reservation: Reservation, **values: object) -> None:
    from app.models.chat import LLMCall
    from app.services.llm_log import accounting_session

    if reservation.row_id is None:
        return
    try:
        async with accounting_session() as session:
            await session.execute(
                update(LLMCall).where(LLMCall.id == reservation.row_id).values(**values)
            )
            await session.commit()
    except Exception as exc:
        log.error("llm.call_not_recorded", model=reservation.model, error=type(exc).__name__)


async def settle(reservation: Reservation, usage: Usage) -> None:
    cost = price_usd(reservation.provider, reservation.model, usage)
    log.info(
        "llm.call",
        model=reservation.model,
        status="ok",
        feature=current().feature,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        latency_ms=usage.latency_ms,
        first_token_ms=usage.first_token_ms,
    )
    await _update(
        reservation,
        status="ok",
        estimated=False,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
        latency_ms=usage.latency_ms,
        first_token_ms=usage.first_token_ms,
    )


async def fail(reservation: Reservation, exc: BaseException) -> None:
    usage = Usage(input_tokens=reservation.input_tokens, output_tokens=0)
    log.info("llm.call", model=reservation.model, status="failed", error_kind=type(exc).__name__)
    await _update(
        reservation,
        status="failed",
        error_kind=type(exc).__name__,
        estimated=True,
        input_tokens=usage.input_tokens,
        output_tokens=0,
        cost_usd=price_usd(reservation.provider, reservation.model, usage),
    )


async def partial(reservation: Reservation, delivered_chars: int) -> None:
    usage = Usage(input_tokens=reservation.input_tokens, output_tokens=max(0, delivered_chars // 4))
    log.info("llm.call", model=reservation.model, status="partial")
    await _update(
        reservation,
        status="partial",
        estimated=True,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=price_usd(reservation.provider, reservation.model, usage),
    )
```

(`Reservation` carries `provider`/`model` rather than a `ModelSpec` so `app.llm.meter` never imports `registry`, which imports it.)

`LLMClient` in `app/llm/registry.py` — add a helper and route the three methods through the meter:

```python
def _chars(system: str | None, messages: Sequence[ChatMessage]) -> int:
    return len(system or "") + sum(len(text_of(m.content)) for m in messages)
```

(`text_of` lives beside `ChatMessage` in `app.llm.types`; import it.)

```python
    async def complete(self, role, messages, *, system=None, max_tokens=1024, tools=None):
        spec = self._roles[role]
        provider = self._providers[spec.provider]
        reservation = await meter.open_call(
            role.value, spec.provider, spec.model,
            system=system, input_chars=_chars(system, messages), max_output=max_tokens,
        )
        started = time.perf_counter()
        try:
            response = await provider.complete(
                model=spec.model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
            )
        except BaseException as exc:
            await meter.fail(reservation, exc)
            raise
        response = _timed(response, started)
        await meter.settle(reservation, response.usage)
        return response
```

`embed` the same way with `input_chars=sum(len(t) for t in texts)`, `max_output=0`, `system=None`.

`stream` keeps its timing and adds the meter; settle on the terminal usage chunk (before yielding it, so a consumer that stops right after still leaves an `ok` row), `fail` on an exception, `partial` in `finally` when neither happened:

```python
        spec = self._roles[role]
        provider = self._providers[spec.provider]
        reservation = await meter.open_call(
            role.value, spec.provider, spec.model,
            system=system, input_chars=_chars(system, messages), max_output=max_tokens,
        )
        started = time.perf_counter()
        first_token_ms: int | None = None
        delivered = 0
        closed = False
        try:
            async for chunk in provider.stream(
                model=spec.model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
            ):
                if first_token_ms is None and chunk.text:
                    first_token_ms = int((time.perf_counter() - started) * 1000)
                delivered += len(chunk.text or "")
                if chunk.usage is None:
                    yield chunk
                    continue
                usage = chunk.usage.model_copy(update={"first_token_ms": first_token_ms})
                await meter.settle(reservation, usage)
                closed = True
                yield chunk.model_copy(update={"usage": usage})
        except Exception as exc:
            if not closed:
                await meter.fail(reservation, exc)
                closed = True
            raise
        finally:
            if not closed:
                await meter.partial(reservation, delivered)
```

(Keep the existing long docstring on `stream`; add one paragraph on recording. Check `ModelRole` is a `StrEnum` — use `str(role)` if `.value` differs from what `log_llm_call` callers passed; today they pass `role.value`/`str(role)`, which are the same for a `StrEnum`.)

`tests/test_llm_log.py`: its tests exercise `log_llm_call`, which Task 4 deletes. Move each still-meaningful assertion to `tests/test_meter.py` in this task — independent-connection survival of a rollback, unpriced → `cost_usd` NULL, priced cost, latency and first-token timing on the row and in the `llm.call` log line — driving the client (`fake_llm_client(...).complete/stream/embed` under `attributed(...)`) instead of `log_llm_call`, then delete `tests/test_llm_log.py` in Task 4 together with `log_llm_call`. Until Task 4, both paths write rows; that is expected for one task.

- [ ] **Step 4: Run** — `uv run pytest tests/test_meter.py tests/test_llm_log.py -q` → PASS; `uv run poe check && uv run poe format-check` → green. Existing tests that count `LLMCall` rows may now see two rows per call (the client's and the site's `log_llm_call`): if any such test fails, note it and fix it in Task 4 when the site loses its `log_llm_call` — do not change the assertion here. If more than a handful fail, instead make `log_llm_call` a no-op in this task (ledger the ruling) so each commit stays green.

- [ ] **Step 5: Commit**

```bash
git add app/llm/meter.py app/llm/registry.py app/services/llm_log.py tests/test_meter.py tests/test_llm_log.py
git status
git commit -m "feat(llm): the client records every call, including failed and partial ones [S48]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The spend guard [S47]

**Files:**
- Create: `app/services/spend_guard.py`, `tests/test_spend_guard.py`
- Delete: `app/services/budget.py`
- Modify: `app/api/v1/chat.py` (preflight), `tests/test_chat_budget.py`

**Interfaces:**
- Consumes: `meter.ADMIT`, `meter.Estimate`, `meter.BudgetExceeded`, `attribution.Attribution`.
- Produces:
  ```python
  @dataclass(frozen=True) class Spend: cost_usd: float; tokens: int
  async def spend_since(session, learner_id, *, window: timedelta = WINDOW) -> Spend  # counts pending at estimate
  async def check(session, learner_id: uuid.UUID | None, *, background: bool = False) -> None  # raises BudgetExceeded; no reservation
  async def admit(session, who: Attribution, estimate: Estimate) -> None  # installed as meter.ADMIT
  async def background_paused() -> bool  # deployment past 90%
  def reset_cache() -> None
  ```

- [ ] **Step 1: Failing tests** — `tests/test_spend_guard.py`:

```python
"""Caps hold on every paid call, for concurrent calls too, and background work yields first (S47)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.llm import ChatMessage, ChatRole, ModelRole
from app.llm.attribution import attributed
from app.llm.meter import BudgetExceeded
from app.llm.registry import fake_llm_client
from app.models.chat import LLMCall
from app.models.learner import Learner
from app.services import spend_guard

HELLO = [ChatMessage(role=ChatRole.USER, content="hello")]


@pytest.fixture(autouse=True)
def fresh_cache():
    spend_guard.reset_cache()
    yield
    spend_guard.reset_cache()


def _settings(monkeypatch, **kwargs) -> None:
    monkeypatch.setattr(spend_guard, "get_settings", lambda: Settings(**kwargs))


async def _learner(session: AsyncSession) -> uuid.UUID:
    learner = Learner(handle=f"g-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.commit()
    return learner.id


async def _spent(session: AsyncSession, learner_id: uuid.UUID | None, *, tokens: int, cost=None):
    session.add(
        LLMCall(
            learner_id=learner_id,
            role="smart",
            provider="fake",
            model="fake-1",
            input_tokens=tokens,
            output_tokens=0,
            cost_usd=cost,
            feature="chat_turn",
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    await session.commit()


async def test_a_learner_over_the_cap_is_refused_before_any_provider_call(
    db_session: AsyncSession, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=1000)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=1000)
    llm = fake_llm_client("unreached")

    with attributed(learner_id=learner_id, feature="chat_turn"), pytest.raises(BudgetExceeded) as exc:
        await llm.complete(ModelRole.FAST, HELLO)

    assert exc.value.scope == "learner"
    assert llm._providers["fake"].prompts_sent == []  # type: ignore[attr-defined]


async def test_a_reservation_counts_before_its_call_returns(
    db_session: AsyncSession, monkeypatch
) -> None:
    """Review focus 1: an open stream holds a reservation the next call sees."""
    _settings(monkeypatch, learner_daily_token_limit=3000)
    learner_id = await _learner(db_session)
    llm = fake_llm_client("one two three")
    with attributed(learner_id=learner_id, feature="chat_turn"):
        stream = llm.stream(ModelRole.SMART, HELLO, max_tokens=2000)
        await anext(stream)  # the pending row exists; the stream has not finished
        with pytest.raises(BudgetExceeded):
            await llm.complete(ModelRole.FAST, HELLO, max_tokens=2000)
        await stream.aclose()


async def test_background_work_stops_at_ninety_percent(
    db_session: AsyncSession, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=10_000)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=9_000)
    llm = fake_llm_client("ok")

    with attributed(learner_id=learner_id, feature="profile_refresh", background=True):
        with pytest.raises(BudgetExceeded):
            await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)
    with attributed(learner_id=learner_id, feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # live work still allowed


async def test_the_deployment_ceiling(db_session: AsyncSession, monkeypatch) -> None:
    _settings(monkeypatch, spend_budget_usd=10.0)
    await _spent(db_session, None, tokens=1, cost=9.5)
    llm = fake_llm_client("ok")

    with attributed(background=True, feature="reindex"), pytest.raises(BudgetExceeded) as exc:
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)
    assert exc.value.scope == "deployment"
    assert await spend_guard.background_paused() is True

    await _spent(db_session, None, tokens=1, cost=1.0)
    spend_guard.reset_cache()
    with attributed(feature="chat_turn"), pytest.raises(BudgetExceeded):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)


async def test_the_deployment_total_is_cached(db_session: AsyncSession, monkeypatch) -> None:
    _settings(monkeypatch, spend_budget_usd=10.0, spend_guard_cache_seconds=3600)
    llm = fake_llm_client("ok")
    with attributed(feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # caches a small total
    await _spent(db_session, None, tokens=1, cost=50.0)
    with attributed(feature="chat_turn"):
        await llm.complete(ModelRole.FAST, HELLO, max_tokens=10)  # still the cached total


async def test_a_pending_row_counts_at_its_estimate(db_session: AsyncSession) -> None:
    learner_id = await _learner(db_session)
    db_session.add(
        LLMCall(
            learner_id=learner_id,
            role="smart",
            provider="fake",
            model="fake-1",
            input_tokens=10,
            output_tokens=990,
            status="pending",
            estimated=True,
            feature="chat_turn",
        )
    )
    await db_session.commit()
    assert (await spend_guard.spend_since(db_session, learner_id)).tokens == 1000
```

(Use `cast(FakeProvider, llm._providers["fake"]).prompts_sent` if `ty` rejects the attribute access, as earlier slices do.)

In `tests/test_chat_budget.py`, replace `budget.spend_since` → `spend_guard.spend_since`, `budget.require_budget(db_session, learner.id, settings)` → `spend_guard.check(db_session, learner.id)` with settings monkeypatched as above, `budget.BudgetExceeded` → `app.llm.meter.BudgetExceeded`, the `match="token limit"` → assert `exc.value.scope == "learner"`, and the route test's `"spend limit" in response.json()["detail"]` → `response.json()["detail"]["code"] == "budget_exceeded"` (Task 5 adds the handler; until then keep the route raising `HTTPException(429, detail={"code": "budget_exceeded", "scope": exc.scope, "message": exc.message})` itself).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_spend_guard.py tests/test_chat_budget.py -q` → FAIL (`spend_guard` missing).

- [ ] **Step 3: Implement** `app/services/spend_guard.py`:

```python
"""What may be spent, checked before every paid call (S47).

The learner's caps are exact, including under concurrency: admission and the pending row are
one accounting transaction holding an advisory lock on the learner, so parallel calls queue for
a moment and each sees the others' reservations. What counts is every row in the trailing 24
hours — settled at its real numbers, pending at its reserved estimate, including a pending row
a crashed process left behind (unknown spend counts until it leaves the window).

The deployment ceiling reads a total cached for ``spend_guard_cache_seconds`` per process:
summing the whole window on every call would cost more than it protects. It can therefore lag
by that long — a soft edge on a hard ceiling; the learner caps stay exact.

Background work (write-back, profile refresh, concept links, reindex) yields first: it is
refused at 90% of either limit, so automatic work never spends what a learner needs for their
own turns, and never takes the deployment to its ceiling.
"""

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import meter
from app.llm.attribution import Attribution
from app.llm.meter import BudgetExceeded, Estimate
from app.models.chat import LLMCall
from app.services.llm_log import accounting_session

log = structlog.get_logger(__name__)

WINDOW = timedelta(hours=24)
BACKGROUND_SHARE = 0.9

_cache: tuple[float, float] | None = None  # (monotonic time read, deployment total)


def reset_cache() -> None:
    global _cache
    _cache = None


@dataclass(frozen=True)
class Spend:
    cost_usd: float
    tokens: int


def _since(window: timedelta) -> datetime:
    return (datetime.now(UTC) - window).replace(tzinfo=None)


async def spend_since(
    session: AsyncSession, learner_id: uuid.UUID, *, window: timedelta = WINDOW
) -> Spend:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
                func.coalesce(func.sum(LLMCall.input_tokens + LLMCall.output_tokens), 0),
            ).where(LLMCall.learner_id == learner_id, LLMCall.created_at >= _since(window))
        )
    ).one()
    return Spend(cost_usd=float(row[0]), tokens=int(row[1]))


def _learner_over(spend: Spend, estimate: Estimate | None, *, background: bool) -> bool:
    settings = get_settings()
    share = BACKGROUND_SHARE if background else 1.0
    extra_cost = (estimate.cost_usd or 0.0) if estimate else 0.0
    extra_tokens = (estimate.input_tokens + estimate.output_tokens) if estimate else 0
    cost_limit = settings.learner_daily_cost_usd_limit
    token_limit = settings.learner_daily_token_limit
    over_cost = bool(cost_limit) and spend.cost_usd + extra_cost > cost_limit * share
    over_tokens = bool(token_limit) and spend.tokens + extra_tokens > token_limit * share
    return over_cost or over_tokens


async def _deployment_total(session: AsyncSession) -> float | None:
    global _cache
    settings = get_settings()
    if _cache is not None and time.monotonic() - _cache[0] < settings.spend_guard_cache_seconds:
        return _cache[1]
    try:
        total = float(
            await session.scalar(
                select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0)).where(
                    LLMCall.created_at >= _since(timedelta(hours=settings.spend_window_hours))
                )
            )
            or 0.0
        )
    except Exception:
        log.warning("budget.deployment_total_unreadable")
        return _cache[1] if _cache is not None else None
    _cache = (time.monotonic(), total)
    return total


async def _check_deployment(session: AsyncSession, *, background: bool) -> None:
    budget = get_settings().spend_budget_usd
    if not budget:
        return
    total = await _deployment_total(session)
    if total is None:
        return
    if total >= budget * (BACKGROUND_SHARE if background else 1.0):
        raise BudgetExceeded("deployment")


async def check(
    session: AsyncSession, learner_id: uuid.UUID | None, *, background: bool = False
) -> None:
    """Refuse early (the chat preflight) without reserving anything."""
    await _check_deployment(session, background=background)
    if learner_id is not None and _learner_over(
        await spend_since(session, learner_id), None, background=background
    ):
        raise BudgetExceeded("learner")


async def admit(session: AsyncSession, who: Attribution, estimate: Estimate) -> None:
    """``meter.ADMIT``: runs in the transaction that then writes the pending row."""
    await _check_deployment(session, background=who.background)
    if who.learner_id is None:
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"spend:{who.learner_id}"},
    )
    if _learner_over(
        await spend_since(session, who.learner_id), estimate, background=who.background
    ):
        raise BudgetExceeded("learner")


async def background_paused() -> bool:
    """Whether the deployment is past the share at which background work stops."""
    budget = get_settings().spend_budget_usd
    if not budget:
        return False
    async with accounting_session() as session:
        total = await _deployment_total(session)
    return total is not None and total >= budget * BACKGROUND_SHARE


meter.ADMIT = admit
```

Make sure the guard is imported wherever a client is used: import it for its side effect in `app/llm/__init__.py`? No — that would be a cycle (`spend_guard` imports `app.llm`). Instead import `app.services.spend_guard` in `app/main.py` and `app/workers/tasks.py` (next to the other service imports), and in `tests/conftest.py` so every test runs guarded. A comment at each import: "installs the spend guard on the meter (S47)".

`app/api/v1/chat.py` preflight:

```python
    try:
        await spend_guard.check(session, learner.id)
    except BudgetExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"code": "budget_exceeded", "scope": exc.scope, "message": exc.message},
        ) from exc
```

Delete `app/services/budget.py` and its import.

- [ ] **Step 4: Run** — `uv run pytest tests/test_spend_guard.py tests/test_chat_budget.py tests/test_meter.py -q` → PASS; `uv run poe check && uv run poe format-check` → green. A test elsewhere that makes many calls for one learner could now meet the default 2M-token cap through reservations (`max_tokens` is reserved per call); none should, but if one does, raise its learner's limit via settings in that test and ledger it.

- [ ] **Step 5: Commit**

```bash
git add app/services/spend_guard.py app/services/budget.py app/api/v1/chat.py app/main.py app/workers/tasks.py tests/conftest.py tests/test_spend_guard.py tests/test_chat_budget.py
git status
git commit -m "feat(budget): every paid call is admitted against the learner and deployment caps [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Attribution everywhere; `log_llm_call` goes [S48]

**Files:**
- Modify: `app/core/middleware.py` (request id), `app/api/deps.py` (`get_current_learner` sets the learner), every service listed below, `app/workers/tasks.py` (task attribution), `app/services/llm_log.py` (delete `log_llm_call`), `tests/conftest.py` (attribution guard), `tests/test_fault_injection.py`
- Delete: `tests/test_llm_log.py` (its assertions moved to `tests/test_meter.py` in Task 2)

**Interfaces:**
- Consumes: `metered`, `attributed` (Task 1); the meter (Task 2).

- [ ] **Step 1: Failing test** — the attribution guard. In `tests/conftest.py`, make the `api_client` fixture fail its test when that test recorded an unattributed call:

```python
async def _no_unattributed_calls(session: AsyncSession) -> None:
    """S48: a paid call reached from an API request with no feature is an attribution gap."""
    from app.models.chat import LLMCall

    rows = (
        await session.scalars(
            select(LLMCall.role).where(LLMCall.feature == "unattributed")
        )
    ).all()
    if rows:
        pytest.fail(f"{len(rows)} model call(s) recorded with no feature: {sorted(set(rows))}")
```

called after `yield client` in `api_client` (and in `admin_client`). Then run `uv run pytest tests -q -x -p no:randomly -k "api or chat or practice or onboarding or placement or lesson or notes" 2>&1 | tail` → FAIL wherever an API path makes a call no service has attributed yet. That list is this task's checklist.

- [ ] **Step 2: Entry points.**

`request_id_middleware` wraps the downstream call:

```python
    with attributed(request_id=request_id):
        response = await call_next(request)
```

`get_current_learner` (and `get_current_admin`, `AccountHolder`'s dependency) — after the learner is resolved and before returning it:

```python
    bind(learner_id=who.learner.id)  # every model call in this request is theirs (S48)
```

`bind`, not `attributed`: a dependency returns before the endpoint runs, so a `with` block would already have ended. Each request has its own copy of the context, so nothing leaks between requests; add a test in `tests/test_attribution.py` that two requests from different learners each record their own learner (make one API call per learner through `api_client` / a second signed-in client and assert the rows' `learner_id`).

Worker tasks (`app/workers/tasks.py`) wrap their bodies:
- `_memory_write_back_task`: `with attributed(conversation_id=uuid.UUID(conversation_id), background=True):` (the learner is set inside `write_back`, below).
- `_profile_refresh_task`: `with attributed(learner_id=uuid.UUID(learner_id), background=True):`
- `_judge_concept_links_task`: `with attributed(learner_id=uuid.UUID(learner_id), background=True):`
- `_ingest_source_task` / `_retag_source_task`: no wrapper — the pipeline sets its learner from the source.

- [ ] **Step 3: Services.** Replace each `log_llm_call(...)` with attribution, deleting the call and any `if usage.total_tokens:` guard around it (and `usage` variables that become unused). Use `@metered(...)` on the function when the ids are arguments; use a `with attributed(...)` block around the model calls when they come from objects loaded inside the function.

| Function | Change |
| --- | --- |
| `rag/pipeline.py` `run` | `@metered("ingestion", learner="source.learner_id")` |
| `rag/pipeline.py` `_tag_chunks` | `@metered("ingestion", learner="source.learner_id")` |
| `services/assessment.py` `_grade` | `@metered("practice_grading", learner="learner_id")` |
| `services/session_runner.py` `_generate_and_log` | `@metered("item_generation", learner="learner_id")` |
| `services/memory.py` `write_back` | `@metered("memory_write_back", conversation="conversation_id")`, and `with attributed(learner_id=conversation.learner_id):` around extraction, embedding and the judge |
| `services/memory.py` `correct_memory` | `@metered("memory_correction", learner="learner_id")` |
| `services/profile.py` `refresh_profile` | `@metered("profile_refresh", learner="learner_id")` |
| `services/onboarding.py` `run_goal_refinement_turn` | `@metered("onboarding", learner="learner_id")` |
| `services/onboarding.py` `generate_curriculum_for_onboarding` | `@metered("curriculum", learner="learner_id")` |
| `services/decisions.py` `_fast_intent` | `@metered("intent_check", learner="context.learner_id", conversation="context.conversation_id")` |
| `services/notes.py` `_render_and_cache`, `refresh_note` | `@metered("note_distillation", learner="learner_id")` |
| `services/agentic.py` `run_agentic_turn` | `@metered("chat_turn", learner="learner_id", conversation="conversation.id")` |
| `services/chat.py` `run_tutor_turn` | `@metered("chat_turn", learner="learner_id", conversation="conversation.id")` |
| `services/refinement.py` `run_refinement_turn` | `@metered("goal_refinement", learner="learner_id", conversation="conversation.id")` |
| `services/workflow.py` `run_workflow_turn` | `@metered("practice_turn", learner="learner_id", conversation="conversation.id")` |
| `services/content.py` `generate_block` | `@metered("lesson_generation", learner="learner_id")` |
| `services/content.py` `check_block_citations` | `@metered("citation_check", learner="learner_id")` |
| `services/placement.py` `run_placement` | `@metered("placement", learner="learner_id")` |
| `services/reindex.py` `_reembed` | `@metered("reindex", learner="stale.learner_id", background=True)` |
| `services/lesson_plan.py` `generate_lesson_plan` | `@metered("lesson_plan", learner="learner_id")` |
| `services/concept_links.py` `judge_pending` | `@metered("concept_links", learner="learner_id")` |

Where a function signature above differs from what you find (e.g. `run_tutor_turn` takes `conversation_id` rather than `conversation`), name the argument that is actually there. Then work the list from Step 1: each remaining unattributed call is a model call reached from an API path through a function not in the table (query embeddings in retrieval, tool calls, grading inside a turn); give the nearest service entry function a `@metered(...)` with a feature naming what it pays for, and ledger each addition.

Delete `log_llm_call` from `app/services/llm_log.py` (keep the module docstring's accounting-session rationale, `set_accounting_session_factory` and `accounting_session`), delete `tests/test_llm_log.py`, and in `tests/test_fault_injection.py` replace the `monkeypatch.setattr(pipeline, "log_llm_call", _record)` capture with a read of the recorded `LLMCall` row (feature `ingestion`).

- [ ] **Step 4: Run** — `uv run pytest tests -q` → PASS, with the attribution guard active; `uv run poe check && uv run poe format-check` → green. Tests that counted `LLMCall` rows now see exactly one row per call again.

- [ ] **Step 5: Commit**

```bash
git add app/core/middleware.py app/api/deps.py app/rag/pipeline.py app/services app/workers/tasks.py tests/conftest.py tests/test_fault_injection.py tests/test_llm_log.py tests/test_attribution.py
git status   # confirm only this task's files are staged — app/services is a directory; unstage anything unrelated
git commit -m "feat(llm): every call says what paid for it; log_llm_call is gone [S48]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: What a refused call looks like [S47]

**Files:**
- Modify: `app/main.py` (exception handler), `app/api/v1/chat.py` (preflight relies on it), the five turn services (`chat.py`, `agentic.py`, `refinement.py`, `workflow.py`, `onboarding.py`), `app/workers/tasks.py` (deferral, sweep pause), `frontend/src/api/sse.ts`
- Test: `tests/test_spend_guard.py` (append), `frontend/src/api/sse.test.ts` (create)

**Interfaces:**
- Consumes: `BudgetExceeded`, `spend_guard.background_paused` (Task 3).

- [ ] **Step 1: Failing tests.** Append to `tests/test_spend_guard.py`:

```python
async def test_a_refused_route_answers_429_with_its_scope(
    api_client, db_session: AsyncSession, api_learner: Learner, monkeypatch
) -> None:
    _settings(monkeypatch, learner_daily_token_limit=10)
    await _spent(db_session, api_learner.id, tokens=10)
    conv = (await api_client.post("/api/v1/conversations", json={})).json()

    r = await api_client.post(f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"})

    assert r.status_code == 429
    assert r.json()["detail"] == {
        "code": "budget_exceeded",
        "scope": "learner",
        "message": "You've reached today's usage limit. It resets over the next 24 hours.",
    }


async def test_a_turn_refused_mid_way_says_why(db_session: AsyncSession, monkeypatch) -> None:
    """Review focus 3: the learner sees the budget message, not "generation failed"."""
    from app.models.chat import Conversation
    from app.services import chat as chat_svc

    _settings(monkeypatch, learner_daily_token_limit=10)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=10)
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.commit()

    events = [
        e
        async for e in chat_svc.run_tutor_turn(
            db_session,
            fake_llm_client("unreached"),
            learner_id=learner_id,
            conversation=conversation,
            history=[],
            user_content="hello",
            max_tokens=200,
            source_ids=[],
        )
    ]

    errors = [e for e in events if e.type == "error"]
    assert errors and errors[0].detail.startswith("You've reached today's usage limit")


async def test_background_work_refused_is_deferred_quietly(
    db_session: AsyncSession, monkeypatch, caplog
) -> None:
    """Review focus 4."""
    import contextlib
    import logging

    from app.models.chat import Conversation, Message
    from app.workers import tasks

    caplog.set_level(logging.INFO)
    _settings(monkeypatch, learner_daily_token_limit=100)
    learner_id = await _learner(db_session)
    await _spent(db_session, learner_id, tokens=95)

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(
        tasks, "build_llm_client", lambda _s: fake_llm_client('{"interests": ["chess"]}')
    )
    conversation = Conversation(learner_id=learner_id)
    db_session.add(conversation)
    await db_session.flush()
    # A message, so the interests estimator would make a model call.
    db_session.add(Message(conversation_id=conversation.id, role="user", content="I love chess."))
    await db_session.commit()

    await tasks._profile_refresh_task(str(learner_id))  # must not raise
    assert "budget.deferred" in caplog.text
```

(Adjust `run_tutor_turn`'s arguments to its real signature — read it; the point is a turn whose first model call is refused.)

Create `frontend/src/api/sse.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { streamTurn } from "./sse";

describe("a refused turn", () => {
  it("throws the server's message, not a status line", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            JSON.stringify({
              detail: {
                code: "budget_exceeded",
                scope: "learner",
                message: "You've reached today's usage limit. It resets over the next 24 hours.",
              },
            }),
            { status: 429, headers: { "content-type": "application/json" } },
          ),
        ),
      ),
    );
    const turn = streamTurn("c-1", { content: "hi" } as never);
    await expect(turn.next()).rejects.toThrow("You've reached today's usage limit");
  });
});
```

(If `apiFetch` prefixes a base URL or needs auth state, check `src/api/client.ts` and stub accordingly.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_spend_guard.py -q -k "refused or deferred"` and `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/api/sse.test.ts` → FAIL.

- [ ] **Step 3: Implement.**

`app/main.py`:

```python
@app.exception_handler(BudgetExceeded)
async def _budget_exceeded(_request: Request, exc: BudgetExceeded) -> JSONResponse:
    """Any paid call refused by the spend guard (S47) — one shape for every route."""
    return JSONResponse(
        status_code=429,
        content={"detail": {"code": "budget_exceeded", "scope": exc.scope, "message": exc.message}},
    )
```

and the chat preflight becomes a bare `await spend_guard.check(session, learner.id)` (the handler maps it).

In each of the five turn services, before the generic `except Exception` that yields `"generation failed"`:

```python
    except BudgetExceeded as exc:
        yield TurnEvent(type="error", detail=exc.message)
        return
```

Workers — in `_memory_write_back_task`, `_profile_refresh_task` and `_judge_concept_links_task`:

```python
        except BudgetExceeded:
            logger.info("budget.deferred task=%s", "<task name>")
            return
```

(`_memory_write_back_task` already has an `except Exception` that logs and re-raises: put the `BudgetExceeded` clause before it.) In `_refresh_due_once`, first thing: `if await spend_guard.background_paused(): return`.

Ingestion needs nothing: a refused call raises inside the pipeline, which already records the failure with the exception's text as the source's `last_error` — and `BudgetExceeded`'s text is the learner-facing message. Add one assertion to prove it (a pipeline test whose learner is over the cap ends with that message as `last_error`), in `tests/test_spend_guard.py`.

`frontend/src/api/sse.ts`, replacing the status-line throw:

```ts
  if (!res.ok || !res.body) {
    let message = `stream failed: ${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: { message?: string } | string };
      if (typeof body.detail === "object" && body.detail?.message) message = body.detail.message;
    } catch {
      // not JSON: keep the status line
    }
    throw new Error(message);
  }
```

- [ ] **Step 4: Run** — the new tests PASS; `uv run poe check && uv run poe format-check && uv run poe api-contract` → green; frontend `vitest run && npm run build && npm run lint` → green.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/api/v1/chat.py app/services/chat.py app/services/agentic.py app/services/refinement.py app/services/workflow.py app/services/onboarding.py app/workers/tasks.py frontend/src/api/sse.ts frontend/src/api/sse.test.ts tests/test_spend_guard.py
git status
git commit -m "feat(budget): a refused call says why, and background work defers [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Cost by feature, and the alerts [S48]

**Files:**
- Modify: `app/services/spend.py` (`by_feature`, `failed_calls`, `partial_calls`, `estimated_calls`, `stale_pending`), `app/core/alerts.py`, `app/api/v1/ops.py`, `app/workers/tasks.py` (`_alerts_once`), `frontend/src/pages/Admin.tsx`, `frontend/src/pages/Admin.test.tsx`, `frontend/src/api/schema.d.ts`, `tests/test_ops_signals.py`
- Test: `tests/test_spend_guard.py` or `tests/test_ops_signals.py` (append)

**Interfaces:**
- Produces: `SpendWindow.by_feature: list[SpendBucket]`, `SpendWindow.failed_calls: int`, `partial_calls: int`, `estimated_calls: int`, `near_budget: bool`; `async def stale_pending(session, *, older_than: timedelta) -> int`; `evaluate(..., stale_pending: int = 0)`; alerts `spend_near_budget`, `calls_pending_stale`.

- [ ] **Step 1: Failing tests** (append to `tests/test_ops_signals.py`, reusing its helpers):

```python
async def test_spend_is_reported_by_feature_with_failures_counted(db_session) -> None:
    from app.models.chat import LLMCall
    from app.services import spend

    for feature, status, cost in (
        ("chat_turn", "ok", 0.5),
        ("chat_turn", "failed", 0.1),
        ("lesson_generation", "partial", None),
    ):
        db_session.add(
            LLMCall(
                role="smart", provider="fake", model="fake-1", input_tokens=10,
                output_tokens=5, cost_usd=cost, feature=feature, status=status,
                estimated=status != "ok",
            )
        )
    await db_session.commit()

    report = await spend.window(db_session, settings=Settings())

    by = {b.name: b for b in report.by_feature}
    assert by["chat_turn"].calls >= 2 and by["lesson_generation"].unpriced_calls >= 1
    assert report.failed_calls >= 1 and report.partial_calls >= 1 and report.estimated_calls >= 2


def test_near_budget_and_stale_pending_alerts() -> None:
    from app.core.alerts import evaluate

    near = _spend().model_copy(update={"near_budget": True})
    report = evaluate(
        readiness=_ready(), backlog=_backlog(), spend=near, settings=Settings(), stale_pending=2
    )
    names = [a.name for a in report.firing]
    assert "spend_near_budget" in names and "calls_pending_stale" in names
```

and update `test_a_healthy_deployment_fires_nothing_and_still_says_what_it_checked` to 10 checked conditions.

- [ ] **Step 2: Run to verify failure** → FAIL (no `by_feature`, no alerts).

- [ ] **Step 3: Implement.** In `spend.window`: `by_feature=await _buckets(session, LLMCall.feature, since)`; the three counts from one `select(func.count().filter(LLMCall.status == "failed"), func.count().filter(LLMCall.status == "partial"), func.count().filter(LLMCall.estimated.is_(True)))` over the window; `near_budget = budget is not None and cost >= budget * 0.9`. `stale_pending`:

```python
async def stale_pending(session: AsyncSession, *, older_than: timedelta) -> int:
    """Calls reserved and never settled — a process died mid-call (S48)."""
    cutoff = (datetime.now(UTC) - older_than).replace(tzinfo=None)
    return int(
        await session.scalar(
            select(func.count()).select_from(LLMCall).where(
                LLMCall.status == "pending", LLMCall.created_at < cutoff
            )
        )
        or 0
    )
```

Alerts (`evaluate` gains `stale_pending: int = 0`; `checked` gains both names):

```python
    if spend.near_budget and not spend.over_budget:
        firing.append(
            Alert(
                name="spend_near_budget",
                severity="warning",
                detail=f"${spend.cost_usd:.2f} of ${spend.budget_usd:.2f} in {spend.window_hours}h",
                action="Background work (memory write-back, profile refresh, concept links, "
                "reindex) is paused until spend falls below 90%. Live turns continue.",
            )
        )
    if stale_pending > 0:
        firing.append(
            Alert(
                name="calls_pending_stale",
                severity="warning",
                detail=f"{stale_pending} model call(s) reserved over 15 minutes ago and never settled",
                action="A process died mid-call. Check app and worker restarts. These count at "
                "their estimate against spend until they leave the window.",
            )
        )
```

Update the `spend_over_budget` action to: "Every paid call is being refused until spend falls below the budget. Raise GURU_SPEND_BUDGET_USD or wait for the window to roll." Pass `stale_pending=await spend.stale_pending(session, older_than=timedelta(minutes=15))` from `ops.py` and `_alerts_once`.

`Admin.tsx`: a third `<BucketRows buckets={spend.data.by_feature} />` under a "By feature" heading, and one line "Failed {failed_calls} · partial {partial_calls} · estimated {estimated_calls}". Extend `Admin.test.tsx`'s fixture with `by_feature: []` and the three counts, plus an assertion that "By feature" renders.

- [ ] **Step 4: Run** — tests PASS; `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage the schema; `uv run poe api-contract`; frontend gates → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/spend.py app/core/alerts.py app/api/v1/ops.py app/workers/tasks.py frontend/src/pages/Admin.tsx frontend/src/pages/Admin.test.tsx frontend/src/api/schema.d.ts tests/test_ops_signals.py
git status
git commit -m "feat(ops): cost by feature, failed and partial calls, and two spend alerts [S48]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Docs [S47]

- [ ] **Step 1: RUNBOOK** — `## 18. Spend limits (S47, S48)`: every call is recorded by the client (`pending` → `ok`/`failed`/`partial`; `estimated` rows; `prompt_hash`, `app_version`, `feature`, `request_id`); the learner caps (`GURU_LEARNER_DAILY_COST_USD_LIMIT`, `GURU_LEARNER_DAILY_TOKEN_LIMIT`), exact under concurrency, reservations at `max_tokens`; the deployment ceiling (`GURU_SPEND_BUDGET_USD`, `GURU_SPEND_WINDOW_HOURS`, cache `GURU_SPEND_GUARD_CACHE_SECONDS`) and its soft edge; the 90% background rule; what a refusal looks like (429 `budget_exceeded`, the turn error, `budget.deferred`); reading cost by feature on `/ops/spend` and the admin dashboard; `calls_pending_stale`; `GURU_APP_VERSION`.
- [ ] **Step 2: OPERATIONS.md** — the settings and the two new alert rows (in the existing alert table's format), and the updated `spend_over_budget` meaning.
- [ ] **Step 3: Tracker** — S48 → **Implemented** (move to completed, sorted by id): "Every model call is recorded by the client, before and after: pending, ok, failed or partial, with feature, request, prompt hash and app version; cost is reported by feature with failed/partial/estimated counts." S47 stays **Partial**, next step rewritten: "Caps are enforced on every paid call: the learner's exactly (advisory lock and reservations), the deployment ceiling from a 30-second cached total, background work paused at 90% of either. Remaining: whole-request deadlines and cancellation (workstream 5 slice B)." Add deferred minors from the final review.
- [ ] **Step 4: CLAUDE.md** — a bullet after the S43 one: "**Every paid call is recorded and admitted by the client** (S47, S48) — `LLMClient` writes a `pending` row before each call and settles it (`ok`/`failed`/`partial`); `app/services/spend_guard.py` refuses a call over the learner's daily caps (exact under concurrency) or the deployment ceiling, and background work stops at 90%. Services say what they are with `@metered(...)` (`app/llm/attribution.py`); nothing calls a logging function by hand."
- [ ] **Step 5:** commit

```bash
git add docs/RUNBOOK.md docs/OPERATIONS.md docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record spend limits and call accounting [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
