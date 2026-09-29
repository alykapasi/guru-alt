# Whole-Request Deadlines and Stop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound every request's duration and let a learner stop a streaming reply, keeping the text they saw.

**Architecture:** A `TurnControl` (`app/services/turn_control.py`) wraps a flow's event stream in a per-step `asyncio.timeout_at`; a Stop reschedules that timeout to "now", so stop and deadline share one cancellation path in the turn's own task. Stops reach the turn in-process through a registry, or across processes through Postgres `LISTEN/NOTIFY` on the connection the turn lock already holds. A pure ASGI middleware bounds every request until its response starts.

**Tech Stack:** FastAPI/Starlette, asyncio, SQLAlchemy async + asyncpg (`add_listener`), Alembic, React + TanStack Query, vitest.

**Spec:** [docs/superpowers/specs/2026-09-29-deadlines-and-stop-design.md](../specs/2026-09-29-deadlines-and-stop-design.md)

## Global Constraints

- Branch `feat/workstream-2` (PR #44). Never reset, amend, rebase, squash or force-push.
- Stage only the task's files, then run `git status`. One tracker id per commit subject: `[S47]`.
- Commit trailer, exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Gates: `uv run poe check`, `uv run poe format-check`; after model/migration changes `uv run poe db-upgrade && uv run poe db-check` and `uv run python -m tests.testdb`; after API changes `uv run poe api-types` (stage `frontend/src/api/schema.d.ts`) then `uv run poe api-contract`; frontend (in `frontend/`): `npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint`.
- Settings: `turn_deadline_seconds: float = 120.0`, `request_deadline_seconds: float = 180.0` — uncalibrated, listed in S18's inventory.
- New `TurnStatus.STOPPED = "stopped"`; `messages.interrupted` is `"stopped"` | `"timed_out"` | NULL. Migration `0074_turn_stop`.
- Notification channel `turn_stop`; payload is the turn id only, never learner text.
- Timed-out SSE event: `{"type": "error", "detail": "This reply took too long and was cut off.", "code": "deadline"}`; turn `FAILED`, error `deadline`.
- Stopped SSE event: `{"type": "stopped", "message_id": <id or null>}`; turn `STOPPED`.
- Non-streamed expiry: HTTP 504 `{"detail": "deadline_exceeded"}`.
- Never print secrets; never set `GURU_JEV_SMOKE`; no paid runs.

## Review Focus

1. A Stop that lands while the stream is yielding to the client (outside an awaited step) must still stop at the next step — pinned in Task 2 (`test_a_stop_between_steps_stops_at_the_next_one`).
2. A `TimeoutError` raised by the flow itself (e.g. a Jev read's own timeout escaping) must not be reported as the turn deadline — pinned in Task 2 (`test_a_flows_own_timeout_is_not_the_deadline`).
3. Retrying a timed-out turn with the same `client_turn_id` must answer again, not be refused as completed — pinned in Task 3 (`test_a_timed_out_turn_can_be_retried`).
4. A Stop naming a turn from a different conversation (same learner) must answer 404, not stop it — pinned in Task 3 (`test_stop_refuses_a_turn_from_another_conversation`).
5. A `TimeoutError` raised by a route handler (not the middleware's deadline) must surface as the handler's error, not a 504 — pinned in Task 4 (`test_a_handlers_own_timeout_is_not_a_504`).

---

### Task 1: Data — stopped turns, interrupted messages, settings

**Files:**
- Create: `db/migrations/versions/0074_turn_stop.py`
- Modify: `app/models/chat.py` (`TurnStatus`, `Message`), `app/services/turn_common.py` (`add_message`), `app/schemas/chat.py` (`MessageRead`, `TurnRead` comment), `app/core/config.py`, `tests/eval/reliability/knobs.py`, `frontend/src/api/schema.d.ts`
- Test: `tests/test_migrations_with_data.py`, `tests/test_turn_lifecycle.py`

**Interfaces:**
- Produces: `TurnStatus.STOPPED`; `Message.interrupted: Mapped[str | None]`; `add_message(..., interrupted: str | None = None)`; `MessageRead.interrupted: str | None = None`; `Settings.turn_deadline_seconds`, `Settings.request_deadline_seconds`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_migrations_with_data.py`:

```python
async def test_existing_messages_are_not_interrupted() -> None:
    """0074 (S47): a nullable marker, so every existing reply reads as a whole one."""
    async with database_at("0073_item_settings") as connect:
        await upgrade(SCRATCH, "0074_turn_stop")
        conn = await connect()
        try:
            nullable = await conn.fetchval(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'messages' AND column_name = 'interrupted'"
            )
            assert nullable == "YES"
        finally:
            await conn.close()
```

Append to `tests/test_turn_lifecycle.py`:

```python
async def test_a_message_can_carry_how_it_was_interrupted(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    conversation_id = await _goal_conversation(api_client, db_session)
    from app.services.turn_common import add_message

    await add_message(
        db_session, uuid.UUID(conversation_id), "assistant", "half an ans", interrupted="stopped"
    )
    await db_session.commit()

    r = await api_client.get(f"{API}/conversations/{conversation_id}/messages")
    body = r.json()
    rows = body["items"] if isinstance(body, dict) else body
    assert [m["interrupted"] for m in rows if m["role"] == "assistant"] == ["stopped"]
    assert TurnStatus.STOPPED == "stopped"
```

(If the messages route's response shape differs, read it from `app/api/v1/chat.py` and adapt only the unpacking line.)

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_migrations_with_data.py::test_existing_messages_are_not_interrupted tests/test_turn_lifecycle.py::test_a_message_can_carry_how_it_was_interrupted -v`
Expected: FAIL — no revision `0074_turn_stop`; `add_message() got an unexpected keyword argument 'interrupted'`.

- [ ] **Step 3: Implement**

`db/migrations/versions/0074_turn_stop.py`:

```python
"""Stopped turns (S47): how an assistant reply was cut short, when it was.

``messages.interrupted`` is ``stopped`` (the learner pressed Stop) or ``timed_out`` (the turn
deadline expired); NULL is a reply that finished, which is every existing row. The turn status
``stopped`` is a new string in an existing text column and needs no DDL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074_turn_stop"
down_revision: str | Sequence[str] | None = "0073_item_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("interrupted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "interrupted")
```

`app/models/chat.py` — in `TurnStatus` after `CANCELLED`:

```python
    STOPPED = "stopped"  # the learner stopped it; the text so far is kept as the reply (S47)
```

and in `Message` after `grounding_count`:

```python
    # How this reply was cut short (S47): ``stopped`` by the learner or ``timed_out`` by the turn
    # deadline. NULL is a reply that finished. The text is what the learner saw, kept as is.
    interrupted: Mapped[str | None] = mapped_column(Text, default=None)
```

`app/services/turn_common.py` — `add_message` gains `interrupted: str | None = None` as its last parameter and passes `interrupted=interrupted` to `Message(...)`.

`app/schemas/chat.py` — `MessageRead` gains after `grounding_count`:

```python
    # ``stopped`` | ``timed_out`` when the reply was cut short (S47); null otherwise.
    interrupted: str | None = None
```

and `TurnRead.status`'s comment becomes `# pending | completed | failed | cancelled | stopped`.

`app/core/config.py` — after `llm_max_retries`:

```python
    # Whole-request bounds (S47). A streamed turn ends at turn_deadline_seconds keeping its
    # text; any other request answers 504 if it has not started responding by
    # request_deadline_seconds. Uncalibrated guesses, listed in the S18 inventory.
    turn_deadline_seconds: float = Field(default=120.0, gt=0)
    request_deadline_seconds: float = Field(default=180.0, gt=0)
```

`tests/eval/reliability/knobs.py` — append two entries to `KNOBS`:

```python
    Knob(
        id="deadline.turn_seconds",
        where="app.core.config.Settings.turn_deadline_seconds",
        value=120.0,
        governs="how long a streamed turn may run before it is cut off, keeping its text",
        settled_by="the turn-duration distribution from founder use: the 99th percentile of turns that finished",
    ),
    Knob(
        id="deadline.request_seconds",
        where="app.core.config.Settings.request_deadline_seconds",
        value=180.0,
        governs="how long a non-streamed request may run before it answers 504",
        settled_by="measured generation times of lessons and curricula on the chosen providers",
    ),
```

- [ ] **Step 4: Run the gates**

Run: `uv run poe db-upgrade && uv run poe db-check && uv run python -m tests.testdb && uv run pytest tests/test_migrations_with_data.py::test_existing_messages_are_not_interrupted tests/test_turn_lifecycle.py tests/eval/test_reliability_knobs.py -q && uv run poe api-types && uv run poe api-contract`
Expected: db-check clean; tests PASS; contract passes after regeneration.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0074_turn_stop.py app/models/chat.py app/services/turn_common.py app/schemas/chat.py app/core/config.py tests/eval/reliability/knobs.py tests/test_migrations_with_data.py tests/test_turn_lifecycle.py frontend/src/api/schema.d.ts
git commit -m "feat(turns): a reply can record that it was stopped or timed out [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: `TurnControl` — stop and deadline around a stream

**Files:**
- Create: `app/services/turn_control.py`
- Test: `tests/test_turn_control.py`

**Interfaces:**
- Consumes: `app.services.turn_lock.TurnClaim` (`.connection: AsyncConnection | None`), `turn_lock.claim(engine, conversation_id)`.
- Produces:
  - `class Interrupted(StrEnum)`: `STOPPED = "stopped"`, `TIMED_OUT = "timed_out"`.
  - `class TurnControl(turn_id: uuid.UUID, *, deadline_s: float)` — async context manager (registers/unregisters in-process); `stop() -> None`; `stopped: bool`; `async listen(claim: TurnClaim) -> None`; `run(events: AsyncGenerator[T, None]) -> AsyncIterator[T | Interrupted]`.
  - `async request_stop(engine: AsyncEngine, turn_id: uuid.UUID) -> None`.
  - `CHANNEL = "turn_stop"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_turn_control.py`:

```python
"""Stopping a turn and bounding how long it runs (S47)."""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services import turn_control, turn_lock
from app.services.turn_control import Interrupted, TurnControl


class _Flow:
    """A flow that yields tokens, then waits; records whether it was closed."""

    def __init__(self, tokens: list[str], *, then_wait: float = 30.0, on_step=None) -> None:
        self.tokens, self.then_wait, self.on_step = tokens, then_wait, on_step
        self.closed = False

    async def events(self) -> AsyncGenerator[str, None]:
        try:
            for t in self.tokens:
                if self.on_step:
                    self.on_step()
                yield t
            await asyncio.sleep(self.then_wait)
            yield "late"
        finally:
            self.closed = True


async def _collect(control: TurnControl, flow: _Flow) -> list:
    return [e async for e in control.run(flow.events())]


async def test_events_pass_through_when_nothing_interrupts() -> None:
    flow = _Flow(["a", "b"], then_wait=0)
    async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
        assert await _collect(control, flow) == ["a", "b", "late"]


async def test_a_stop_mid_stream_closes_the_flow_and_says_stopped() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
        flow = _Flow(["a"])
        out = []
        async for e in control.run(flow.events()):
            out.append(e)
            if e == "a":
                control.stop()
    assert out == ["a", Interrupted.STOPPED]
    assert flow.closed


async def test_a_stop_between_steps_stops_at_the_next_one() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
        flow = _Flow(["a", "b"])
        out = []
        async for e in control.run(flow.events()):
            out.append(e)
            control.stop()  # while the consumer holds the event, outside any awaited step
    assert out == ["a", Interrupted.STOPPED]


async def test_a_stop_before_any_text() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
        control.stop()
        assert await _collect(control, _Flow(["a"])) == [Interrupted.STOPPED]


async def test_the_deadline_cuts_a_slow_flow_off() -> None:
    async with TurnControl(uuid.uuid4(), deadline_s=0.05) as control:
        flow = _Flow(["a"])
        assert await _collect(control, flow) == ["a", Interrupted.TIMED_OUT]
    assert flow.closed


async def test_a_flows_own_timeout_is_not_the_deadline() -> None:
    async def raises() -> AsyncGenerator[str, None]:
        yield "a"
        raise TimeoutError("the flow's own")

    async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
        with pytest.raises(TimeoutError, match="the flow's own"):
            await _collect_gen(control, raises())


async def _collect_gen(control: TurnControl, gen: AsyncGenerator[str, None]) -> list:
    return [e async for e in control.run(gen)]


async def test_request_stop_reaches_a_turn_in_this_process(engine: AsyncEngine) -> None:
    turn_id = uuid.uuid4()
    async with TurnControl(turn_id, deadline_s=5) as control:
        await turn_control.request_stop(engine, turn_id)
        assert control.stopped


async def test_a_notification_from_another_connection_stops_a_listening_turn(
    engine: AsyncEngine,
) -> None:
    """The cross-process path: the turn listens on its claim's connection; the stop arrives
    as a NOTIFY sent over a different connection."""
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None and claim.connection is not None
    turn_id = uuid.uuid4()
    try:
        async with TurnControl(turn_id, deadline_s=5) as control:
            await control.listen(claim)
            async with engine.connect() as other:
                other = await other.execution_options(isolation_level="AUTOCOMMIT")
                await other.execute(
                    text("SELECT pg_notify(:c, :p)"), {"c": turn_control.CHANNEL, "p": str(turn_id)}
                )
            for _ in range(50):
                if control.stopped:
                    break
                await asyncio.sleep(0.02)
            assert control.stopped
    finally:
        await claim.release()


async def test_a_notification_for_another_turn_is_ignored(engine: AsyncEngine) -> None:
    claim = await turn_lock.claim(engine, uuid.uuid4())
    assert claim is not None
    try:
        async with TurnControl(uuid.uuid4(), deadline_s=5) as control:
            await control.listen(claim)
            async with engine.connect() as other:
                other = await other.execution_options(isolation_level="AUTOCOMMIT")
                await other.execute(
                    text("SELECT pg_notify(:c, :p)"), {"c": turn_control.CHANNEL, "p": str(uuid.uuid4())}
                )
            await asyncio.sleep(0.2)
            assert not control.stopped
    finally:
        await claim.release()


async def test_the_registry_is_empty_after_the_turn() -> None:
    turn_id = uuid.uuid4()
    async with TurnControl(turn_id, deadline_s=5):
        assert turn_id in turn_control._local
    assert turn_id not in turn_control._local
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_turn_control.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.turn_control'`.

- [ ] **Step 3: Implement `app/services/turn_control.py`**

```python
"""Stopping a turn, and bounding how long it runs (S47).

A turn used to run until its flow finished, however long that took: the provider SDKs bound
each network *read*, not a call, and a turn makes several calls. And a learner could not stop a
reply — only close the page, which records the turn as ``cancelled`` and loses the text.

**One cancellation path for both.** :meth:`TurnControl.run` awaits each step of the flow inside
``asyncio.timeout_at(deadline)``. A Stop reschedules that same timeout to *now*. Either way the
flow is cancelled at its current await, in the turn's own task (so context variables and the
request's session behave exactly as they do for a disconnect), its ``finally`` blocks close the
provider stream — which the meter already settles as ``partial`` — and ``run`` yields one
:class:`Interrupted` marker. The caller decides what that means for the transcript.

**How a Stop arrives.** In this process, through ``_local``. From another process, as a
Postgres notification on channel ``turn_stop`` whose payload is the turn id — delivered on the
connection the turn's lock already holds (``TurnClaim.connection``), so only a turn that is
really running is listening. The payload is an id, never learner text.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Self

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.services.turn_lock import TurnClaim

log = structlog.get_logger(__name__)

CHANNEL = "turn_stop"


class Interrupted(StrEnum):
    STOPPED = "stopped"
    TIMED_OUT = "timed_out"


# Turns running in this process, by id. A Stop for one of these never needs the database.
_local: dict[uuid.UUID, "TurnControl"] = {}


class TurnControl:
    def __init__(self, turn_id: uuid.UUID, *, deadline_s: float) -> None:
        self.turn_id = turn_id
        self.deadline_s = deadline_s
        self.stopped = False
        self._timeout: asyncio.Timeout | None = None
        self._unlisten: Callable[[], Awaitable[None]] | None = None

    async def __aenter__(self) -> Self:
        _local[self.turn_id] = self
        return self

    async def __aexit__(self, *exc: object) -> None:
        _local.pop(self.turn_id, None)
        if self._unlisten is not None:
            await self._unlisten()

    def stop(self) -> None:
        """Stop at the current step, or at the next one if none is in flight. Idempotent."""
        self.stopped = True
        if self._timeout is not None:
            self._timeout.reschedule(asyncio.get_running_loop().time())

    async def listen(self, claim: TurnClaim) -> None:
        """Take Stops from other processes on the claim's own connection.

        Without a connection (the lock fell back to in-process) only in-process Stops arrive —
        the same single-process degradation the lock has. A failure to listen leaves the turn
        running without Stop; its deadline still holds.
        """
        if claim.connection is None:
            return
        try:
            raw = await claim.connection.get_raw_connection()
            driver = raw.driver_connection
            assert driver is not None

            def on_notify(_conn: object, _pid: int, _channel: str, payload: str) -> None:
                if payload == str(self.turn_id):
                    self.stop()

            await driver.add_listener(CHANNEL, on_notify)

            async def unlisten() -> None:
                try:
                    await driver.remove_listener(CHANNEL, on_notify)
                except Exception:  # the connection may already be closing with the claim
                    pass

            self._unlisten = unlisten
        except Exception as exc:
            log.warning("turn_control.listen_failed", turn_id=str(self.turn_id), error=str(exc))

    async def run[T](self, events: AsyncGenerator[T, None]) -> AsyncIterator[T | Interrupted]:
        loop = asyncio.get_running_loop()
        end = loop.time() + self.deadline_s
        try:
            while True:
                timeout = asyncio.timeout_at(end)
                try:
                    async with timeout:
                        self._timeout = timeout
                        if self.stopped:
                            timeout.reschedule(loop.time())
                        event = await anext(events)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    if not timeout.expired():
                        raise  # the flow's own timeout, not ours
                    yield Interrupted.STOPPED if self.stopped else Interrupted.TIMED_OUT
                    return
                finally:
                    self._timeout = None
                yield event
        finally:
            await events.aclose()


async def request_stop(engine: AsyncEngine, turn_id: uuid.UUID) -> None:
    """Ask the running turn ``turn_id`` to stop, wherever it runs."""
    local = _local.get(turn_id)
    if local is not None:
        local.stop()
        return
    async with engine.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": CHANNEL, "payload": str(turn_id)},
        )
```

Note: if `ty` rejects the PEP 695 method generic, use a module-level `T = TypeVar("T")` and annotate `run(self, events: AsyncGenerator[T, None]) -> AsyncIterator[T | Interrupted]` — ledger the ruling.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_turn_control.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/turn_control.py tests/test_turn_control.py
git commit -m "feat(turns): a turn can be stopped or cut off at its deadline [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: Chat and onboarding streams use `TurnControl`; the Stop route

**Files:**
- Modify: `app/api/v1/chat.py` (`send_message`'s `event_stream`, new `stop_turn` route beside `list_turns`), `app/api/v1/onboarding.py` (`event_stream`), `frontend/src/api/schema.d.ts`
- Test: `tests/test_turn_stop.py`

**Interfaces:**
- Consumes: Task 1 (`TurnStatus.STOPPED`, `add_message(..., interrupted=)`, `Settings.turn_deadline_seconds`); Task 2 (`TurnControl`, `Interrupted`, `request_stop`).
- Produces: SSE events `{"type": "turn", "turn_id": str}` (first frame of every chat stream), `{"type": "stopped", "message_id": str | None}`, and the deadline error frame; route `POST /api/v1/conversations/{conversation_id}/turns/{turn_id}/stop` → 202 `{"status": "stopping"}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_turn_stop.py`:

```python
"""A learner can stop a reply and keep what they saw; a turn cannot run forever (S47)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import get_llm_client
from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.chat import Conversation, Message, Turn, TurnStatus
from app.services import turn as turn_svc
from app.services import turn_control, turn_lock
from app.services.turn_common import TurnEvent

API = "/api/v1"


@pytest.fixture
def fake_llm() -> Iterator[None]:
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client("A whole reply.")
    yield
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(body: str) -> list[dict]:
    return [json.loads(x[6:]) for x in body.splitlines() if x.startswith("data: ")]


async def _conversation(api_client: AsyncClient, db_session: AsyncSession) -> str:
    r = await api_client.post(f"{API}/conversations", json={"title": "Calc"})
    cid = r.json()["id"]
    conversation = await db_session.get(Conversation, uuid.UUID(cid))
    assert conversation is not None
    conversation.goal = "Understand derivatives"
    await db_session.commit()
    return cid


async def _turn(db_session: AsyncSession, cid: str) -> Turn:
    return (await db_session.scalars(select(Turn).where(Turn.conversation_id == uuid.UUID(cid)))).one()


async def _assistant(db_session: AsyncSession, cid: str) -> list[Message]:
    rows = await db_session.scalars(
        select(Message).where(
            Message.conversation_id == uuid.UUID(cid), Message.role == "assistant"
        )
    )
    return list(rows.all())


def _stopping_flow():
    """Streams two tokens, then the learner presses Stop, then it would go on for ever."""

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Half ")
        yield TurnEvent(type="token", text="an answer")
        for control in list(turn_control._local.values()):
            control.stop()
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    return flow


async def test_the_stream_names_its_turn_first(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    events = _sse(r.text)
    assert events[0] == {"type": "turn", "turn_id": str((await _turn(db_session, cid)).id)}


async def test_a_stopped_reply_keeps_its_text(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _stopping_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    events = _sse(r.text)
    [reply] = await _assistant(db_session, cid)
    assert events[-1] == {"type": "stopped", "message_id": str(reply.id)}
    assert (reply.content, reply.interrupted) == ("Half an answer", "stopped")
    turn = await _turn(db_session, cid)
    assert (turn.status, turn.assistant_message_id, turn.error) == (
        TurnStatus.STOPPED,
        reply.id,
        None,
    )


async def test_a_stop_before_any_text_saves_no_reply(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)

    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        for control in list(turn_control._local.values()):
            control.stop()
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", flow)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {"type": "stopped", "message_id": None}
    assert await _assistant(db_session, cid) == []
    assert (await _turn(db_session, cid)).status == TurnStatus.STOPPED


def _short_deadline(monkeypatch) -> None:
    settings = get_settings().model_copy(update={"turn_deadline_seconds": 0.1})
    monkeypatch.setattr("app.api.v1.chat.get_settings", lambda: settings)


def _slow_flow():
    async def flow(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        yield TurnEvent(type="token", text="Slow start")
        await asyncio.sleep(30)
        yield TurnEvent(type="done")

    return flow


async def test_a_turn_past_its_deadline_is_cut_off_keeping_its_text(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    _short_deadline(monkeypatch)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _slow_flow())

    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})

    assert _sse(r.text)[-1] == {
        "type": "error",
        "detail": "This reply took too long and was cut off.",
        "code": "deadline",
    }
    [reply] = await _assistant(db_session, cid)
    assert (reply.content, reply.interrupted) == ("Slow start", "timed_out")
    turn = await _turn(db_session, cid)
    assert (turn.status, turn.error) == (TurnStatus.FAILED, "deadline")


async def test_a_timed_out_turn_can_be_retried(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    key = str(uuid.uuid4())
    _short_deadline(monkeypatch)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _slow_flow())
    await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )
    monkeypatch.undo()

    r = await api_client.post(
        f"{API}/conversations/{cid}/messages", json={"content": "Hi", "client_turn_id": key}
    )

    assert r.status_code == 200
    assert _sse(r.text)[-1]["type"] == "done"
    assert (await _turn(db_session, cid)).status == TurnStatus.COMPLETED


async def test_the_next_turn_sees_a_stopped_reply(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, monkeypatch
) -> None:
    cid = await _conversation(api_client, db_session)
    monkeypatch.setattr("app.services.chat.run_tutor_turn", _stopping_flow())
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    seen: list = []

    async def capture(*args, **kwargs) -> AsyncIterator[TurnEvent]:
        seen.append(kwargs.get("history", args[3] if len(args) > 3 else None))
        yield TurnEvent(type="done")

    monkeypatch.setattr("app.services.chat.run_tutor_turn", capture)
    await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Go on"})

    assert any(getattr(m, "content", None) == "Half an answer" for m in seen[0])


# --- the Stop route ------------------------------------------------------------------------


async def test_stop_asks_a_running_turn_to_stop(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None, engine: AsyncEngine
) -> None:
    cid = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Hi"
    )
    claim = await turn_lock.claim(engine, uuid.UUID(cid))
    assert claim is not None
    try:
        async with turn_control.TurnControl(turn.id, deadline_s=5) as control:
            r = await api_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
            assert (r.status_code, r.json()) == (202, {"status": "stopping"})
            assert control.stopped
    finally:
        await claim.release()


async def test_stop_refuses_a_turn_that_is_not_running(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    r = await api_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Hi"})
    assert r.status_code == 200
    turn = await _turn(db_session, cid)  # completed
    r = await api_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
    assert r.status_code == 409

    dead = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Again"
    )  # pending, but no process holds the conversation
    r = await api_client.post(f"{API}/conversations/{cid}/turns/{dead.id}/stop")
    assert r.status_code == 409


async def test_stop_refuses_a_turn_from_another_conversation(
    api_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    first = await _conversation(api_client, db_session)
    second = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(first), flow="tutor", content="Hi"
    )
    r = await api_client.post(f"{API}/conversations/{second}/turns/{turn.id}/stop")
    assert r.status_code == 404


async def test_stop_refuses_another_learners_conversation(
    api_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession, fake_llm: None
) -> None:
    cid = await _conversation(api_client, db_session)
    turn = await turn_svc.open_turn(
        db_session, conversation_id=uuid.UUID(cid), flow="tutor", content="Hi"
    )
    other = Learner(handle=f"other-{uuid.uuid4().hex[:8]}", display_name="Other")
    db_session.add(other)
    await db_session.flush()
    await sign_in(anon_client, db_session, other)

    r = await anon_client.post(f"{API}/conversations/{cid}/turns/{turn.id}/stop")
    assert r.status_code == 404
```

Add to the imports: `Learner` from the module `tests/conftest.py` imports it from, and `from tests.conftest import sign_in`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_turn_stop.py -v`
Expected: FAIL — no `turn` first frame; no `stopped` event; stop route 404/405.

- [ ] **Step 3: Implement in `app/api/v1/chat.py`**

Imports: `from app.services import turn_control` and `from app.services.turn_common import add_message` (if not already imported), `from app.services.turn_control import Interrupted`.

Module constant near the top:

```python
DEADLINE_DETAIL = "This reply took too long and was cut off."
```

In `send_message`, after `stream = _build_stream(...)` succeeds (inside the `try`), create the control:

```python
        control = turn_control.TurnControl(turn.id, deadline_s=settings.turn_deadline_seconds)
```

Replace the body of `event_stream` so that it runs inside the control, announces the turn first, collects token text, and handles the marker. The existing per-event branches stay as they are; only the loop header, the token branch's accumulation, the marker handling and the close change:

```python
    async def event_stream() -> AsyncIterator[str]:
        awaiting_reply = False
        check_open = False
        active_item: uuid.UUID | None = None
        outcome: TurnStatus | None = None
        assistant_message_id: uuid.UUID | None = None
        error: str | None = None
        streamed: list[str] = []
        async with control:
            await control.listen(claim)
            # First, so the client can address a Stop to this turn.
            yield _sse({"type": "turn", "turn_id": str(turn.id)})
            async for ev in control.run(stream):
                if isinstance(ev, Interrupted):
                    # The flow was cancelled at an await; anything it had not committed is
                    # rolled back, exactly as for a disconnect. What the learner saw is kept.
                    await session.rollback()
                    text_so_far = "".join(streamed)
                    if text_so_far:
                        message = await add_message(
                            session, conversation_id, "assistant", text_so_far,
                            interrupted=ev.value,
                        )
                        assistant_message_id = message.id
                    if ev is Interrupted.STOPPED:
                        outcome = TurnStatus.STOPPED
                        yield _sse(
                            {
                                "type": "stopped",
                                "message_id": str(assistant_message_id)
                                if assistant_message_id
                                else None,
                            }
                        )
                    else:
                        outcome, error = TurnStatus.FAILED, "deadline"
                        yield _sse({"type": "error", "detail": DEADLINE_DETAIL, "code": "deadline"})
                    break
                if ev.type == "token":
                    streamed.append(ev.text)
                    yield _sse({"type": "token", "text": ev.text})
                # ... every other existing elif branch, unchanged ...

        await svc.record_phase(...)  # unchanged
        await turn_svc.close_turn(
            session,
            turn.id,
            outcome or TurnStatus.CANCELLED,
            assistant_message_id=assistant_message_id,
            error=error if outcome else "the stream ended without a terminal event",
        )
```

`_build_stream` must return an async generator (it does if it is one, or returns one); if `ty` reports `AsyncIterator` where `AsyncGenerator` is needed, change `_build_stream`'s return annotation to `AsyncGenerator[TurnEvent, None]` and ledger it.

The Stop route, beside `list_turns`:

```python
@router.post(
    "/conversations/{conversation_id}/turns/{turn_id}/stop", status_code=status.HTTP_202_ACCEPTED
)
async def stop_turn(
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    db_engine: EngineDep,
) -> dict[str, str]:
    """Ask a running turn to stop (S47). Its text so far is kept as the reply.

    409 when the turn is not running — it finished first, or the process running it is gone —
    which a client racing a finishing turn can ignore: its ``done`` has arrived or will.
    """
    conversation = await svc.get_conversation(session, conversation_id, learner_id=learner.id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    turn = await session.get(Turn, turn_id)
    if turn is None or turn.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "turn not found")
    if turn.status != TurnStatus.PENDING or not await turn_lock.is_active(
        session, conversation_id
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "this turn is not running")
    await turn_control.request_stop(db_engine, turn_id)
    return {"status": "stopping"}
```

(Import `Turn` from `app.models.chat` if not already.)

`app/api/v1/onboarding.py` — the goal-refinement stream gets the deadline, not Stop:

```python
    async def event_stream() -> AsyncIterator[str]:
        control = turn_control.TurnControl(
            uuid.uuid4(), deadline_s=get_settings().turn_deadline_seconds
        )
        try:
            async with control:
                async for event in control.run(
                    onboarding.run_goal_refinement_turn(...same arguments...)
                ):
                    if event is Interrupted.TIMED_OUT:
                        yield _sse(
                            {"type": "error", "detail": DEADLINE_DETAIL, "code": "deadline"}
                        )
                        break
                    ...existing branches...
        except Exception as exc:
            yield _sse({"type": "error", "detail": str(exc)})
```

Import `DEADLINE_DETAIL` from `app.api.v1.chat` or move it into `turn_control` as `DEADLINE_DETAIL` and import it in both (preferred; ledger the choice).

Add one onboarding test to `tests/test_turn_stop.py` only if the onboarding tests already have a helper to start a goal session (grep `onboarding/goal-sessions` in `tests/`); otherwise ledger that the onboarding path is covered by review only.

- [ ] **Step 4: Run to verify**

Run: `uv run pytest tests/test_turn_stop.py tests/test_turn_lifecycle.py tests/test_chat.py -q && uv run poe api-types && uv run poe api-contract`
Expected: PASS; contract passes after regeneration.

- [ ] **Step 5: Full gate and commit**

Run: `uv run poe check && uv run poe format-check`
Expected: all green.

```bash
git add app/api/v1/chat.py app/api/v1/onboarding.py app/services/turn_control.py tests/test_turn_stop.py frontend/src/api/schema.d.ts
git commit -m "feat(chat): stop a reply and keep its text; turns have a deadline [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: `RequestDeadlineMiddleware`

**Files:**
- Create: `app/core/deadline.py`
- Modify: `app/main.py` (register before the other `add_middleware` calls so it is innermost)
- Test: `tests/test_request_deadline.py`

**Interfaces:**
- Consumes: `Settings.request_deadline_seconds`.
- Produces: `RequestDeadlineMiddleware(app, *, seconds: Callable[[], float] | None = None)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_request_deadline.py`:

```python
"""Every request is bounded until it starts responding (S47)."""

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.core.deadline import RequestDeadlineMiddleware
from app.main import app as guru_app

cancelled: list[str] = []


async def slow(request):
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        cancelled.append("slow")
        raise
    return JSONResponse({"ok": True})


async def fast(request):
    return JSONResponse({"ok": True})


async def own_timeout(request):
    raise TimeoutError("the handler's own")


async def stream(request):
    async def body():
        yield b"data: a\n\n"
        await asyncio.sleep(0.3)
        yield b"data: b\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _client(seconds: float) -> httpx.AsyncClient:
    inner = Starlette(
        routes=[
            Route("/slow", slow),
            Route("/fast", fast),
            Route("/own", own_timeout),
            Route("/stream", stream),
        ]
    )
    wrapped = RequestDeadlineMiddleware(inner, seconds=lambda: seconds)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped, raise_app_exceptions=True),
        base_url="http://t",
    )


async def test_a_slow_request_answers_504_and_its_handler_is_cancelled() -> None:
    cancelled.clear()
    async with _client(0.05) as c:
        r = await c.get("/slow")
    assert (r.status_code, r.json()) == (504, {"detail": "deadline_exceeded"})
    assert cancelled == ["slow"]


async def test_a_fast_request_is_untouched() -> None:
    async with _client(1) as c:
        assert (await c.get("/fast")).json() == {"ok": True}


async def test_a_stream_runs_past_the_limit_once_it_has_started() -> None:
    async with _client(0.1) as c:
        r = await c.get("/stream")
    assert r.status_code == 200
    assert "data: b" in r.text


async def test_a_handlers_own_timeout_is_not_a_504() -> None:
    async with _client(1) as c:
        try:
            r = await c.get("/own")
        except TimeoutError as exc:
            assert "the handler's own" in str(exc)
        else:
            assert r.status_code != 504


def test_the_app_installs_it() -> None:
    assert any(m.cls is RequestDeadlineMiddleware for m in guru_app.user_middleware)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_request_deadline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.deadline'`.

- [ ] **Step 3: Implement `app/core/deadline.py`**

```python
"""A deadline on every request, until it starts responding (S47).

A route that calls a model had no bound on how long it could run: the provider SDKs bound each
network read, and a lesson or curriculum makes several calls. This middleware cancels the
handler and answers 504 if no response has *started* by ``request_deadline_seconds``.

Once the response starts the limit is lifted, so a server-sent-event stream — whose headers go
out at once — is governed by its turn's own deadline (``app.services.turn_control``) instead.
It applies to every route, so no endpoint can be forgotten. A ``TimeoutError`` the handler
raises itself is its own error, not a deadline, and passes through.
"""

import asyncio
from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings


class RequestDeadlineMiddleware:
    def __init__(self, app: ASGIApp, *, seconds: Callable[[], float] | None = None) -> None:
        self.app = app
        self.seconds = seconds or (lambda: get_settings().request_deadline_seconds)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        timeout = asyncio.timeout(self.seconds())

        async def send_and_lift(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                timeout.reschedule(None)
            await send(message)

        try:
            async with timeout:
                await self.app(scope, receive, send_and_lift)
        except TimeoutError:
            if not timeout.expired() or started:
                raise
            await JSONResponse({"detail": "deadline_exceeded"}, status_code=504)(
                scope, receive, send
            )
```

`app/main.py` — immediately before `app.add_middleware(AdminAuditMiddleware)`:

```python
# Innermost, so a 504 still passes through audit and the rest (S47).
app.add_middleware(RequestDeadlineMiddleware)
```

with `from app.core.deadline import RequestDeadlineMiddleware`.

- [ ] **Step 4: Run to verify**

Run: `uv run pytest tests/test_request_deadline.py -v && uv run poe check`
Expected: PASS; full check green.

- [ ] **Step 5: Commit**

```bash
git add app/core/deadline.py app/main.py tests/test_request_deadline.py
git commit -m "feat(api): every request has a deadline until it starts responding [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: Frontend — Stop, and marking cut-short replies

**Files:**
- Modify: `frontend/src/api/sse.ts` (event types, `TERMINAL_EVENTS`, `stopTurn`), `frontend/src/hooks/useChatConversation.ts` (`stop`), `frontend/src/components/chat/Composer.tsx` (Stop button), `frontend/src/components/chat/MessageBlock.tsx` (`interrupted` note), `frontend/src/components/chat/MessageList.tsx` (pass `interrupted`), `frontend/src/pages/Chat.tsx` (wire `stop`)
- Test: `frontend/src/components/chat/Composer.test.tsx` (create), `frontend/src/components/chat/MessageBlock.test.tsx` (create), `frontend/src/api/sse.test.ts`

**Interfaces:**
- Consumes: Task 3's SSE frames and route; Task 1's `MessageRead.interrupted`.
- Produces: `stopTurn(conversationId: string, turnId: string): Promise<void>`; hook returns `stop: () => void`; `Composer` props `streaming?: boolean`, `onStop?: () => void`; `MessageBlock` prop `interrupted?: string | null`.

- [ ] **Step 1: Write the failing tests**

`frontend/src/components/chat/Composer.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Composer } from "./Composer";

describe("Composer", () => {
  it("offers Stop instead of Send while a reply streams", async () => {
    const onStop = vi.fn();
    render(<Composer disabled streaming onStop={onStop} onSend={() => {}} />);
    expect(screen.queryByRole("button", { name: "Send" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("offers Send when nothing is streaming", () => {
    render(<Composer disabled={false} onSend={() => {}} />);
    expect(screen.getByRole("button", { name: "Send" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
  });
});
```

`frontend/src/components/chat/MessageBlock.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageBlock } from "./MessageBlock";

describe("MessageBlock", () => {
  it("marks a reply the learner stopped", () => {
    render(<MessageBlock role="assistant" content="Half an answer" interrupted="stopped" />);
    expect(screen.getByText("Stopped")).toBeTruthy();
  });

  it("marks a reply cut off by the deadline", () => {
    render(<MessageBlock role="assistant" content="Slow start" interrupted="timed_out" />);
    expect(screen.getByText("This reply took too long and was cut off.")).toBeTruthy();
  });

  it("says nothing about a whole reply", () => {
    render(<MessageBlock role="assistant" content="A whole reply." />);
    expect(screen.queryByText("Stopped")).toBeNull();
  });
});
```

Append to `frontend/src/api/sse.test.ts`:

```ts
import { isTerminal } from "./sse";

describe("stopped", () => {
  it("is a terminal event", () => {
    expect(isTerminal({ type: "stopped", message_id: null })).toBe(true);
    expect(isTerminal({ type: "turn", turn_id: "t" })).toBe(false);
  });
});
```

(Merge the import with the file's existing imports if `isTerminal` is already imported.)

- [ ] **Step 2: Run to verify they fail**

Run (in `frontend/`): `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/chat/Composer.test.tsx src/components/chat/MessageBlock.test.tsx src/api/sse.test.ts`
Expected: FAIL — no Stop button; no "Stopped" text; `stopped` not terminal.

- [ ] **Step 3: Implement**

`sse.ts`:
- `TurnEvent` gains `| { type: "turn"; turn_id: string } | { type: "stopped"; message_id: string | null }`, and the error member becomes `{ type: "error"; detail: string; code?: string }`.
- `TERMINAL_EVENTS = ["done", "awaiting_reply", "committed", "error", "stopped"] as const;`
- Add:

```ts
/** Ask the running turn to stop (S47). A 409 means it already finished — nothing to do. */
export async function stopTurn(conversationId: string, turnId: string): Promise<void> {
  const res = await apiFetch(
    `/api/v1/conversations/${conversationId}/turns/${turnId}/stop`,
    { method: "POST" },
  );
  if (!res.ok && res.status !== 409) {
    throw new Error(await failureMessage(res, `stop failed: ${res.status} ${res.statusText}`));
  }
}
```

`apiFetch` and `failureMessage` are the helpers `streamTurn` already uses in this file.

`useChatConversation.ts`:
- `const turnIdRef = useRef<string | null>(null);` reset to `null` at the start of `run`.
- In the event loop: `if (ev.type === "turn") turnIdRef.current = ev.turn_id;` — and `stopped` needs no branch (it is terminal; the transcript refetch brings the saved partial reply).
- Add:

```ts
  /** Stop the reply being written (S47). The stream keeps going until the server's `stopped`
   * event, so what stays on screen is exactly what was saved. */
  const stop = useCallback(() => {
    const turnId = turnIdRef.current;
    if (!conversationId || !turnId) return;
    void stopTurn(conversationId, turnId).catch(() => {});
  }, [conversationId]);
```

and return `stop` from the hook.

`Composer.tsx` — props `streaming?: boolean; onStop?: () => void`; replace the Send button with:

```tsx
          {streaming && onStop ? (
            <button onClick={onStop} className="btn btn-square" aria-label="Stop">
              <Square size={16} />
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={disabled || !content.trim()}
              className="btn btn-primary btn-square"
              aria-label="Send"
            >
              <ArrowUp size={18} />
            </button>
          )}
```

(`Square` from `lucide-react`.)

`MessageBlock.tsx` — prop `interrupted?: string | null` (default `null`); in the assistant branch, after the content and before/alongside the coverage chip:

```tsx
        {interrupted === "stopped" && (
          <span className="text-caption text-base-content/50">Stopped</span>
        )}
        {interrupted === "timed_out" && (
          <span className="text-caption text-base-content/50">
            This reply took too long and was cut off.
          </span>
        )}
```

`MessageList.tsx` — pass `interrupted={m.interrupted ?? null}` where it renders a persisted message's `MessageBlock`.

`Chat.tsx` — `<Composer disabled={!!pending} streaming={!!pending} onStop={stop} onSend={...} />`, taking `stop` from the hook.

- [ ] **Step 4: Run to verify**

Run (in `frontend/`): `npm run build && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run lint`
Expected: build succeeds; all tests pass; lint clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/sse.ts frontend/src/api/sse.test.ts frontend/src/hooks/useChatConversation.ts frontend/src/components/chat/Composer.tsx frontend/src/components/chat/Composer.test.tsx frontend/src/components/chat/MessageBlock.tsx frontend/src/components/chat/MessageBlock.test.tsx frontend/src/components/chat/MessageList.tsx frontend/src/pages/Chat.tsx
git commit -m "feat(chat): a Stop button, and cut-short replies say so [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: Docs

**Files:**
- Modify: `docs/guru-suggestions-tracker.md` (S47 → Implemented row in Completed; remove from Live work and the At-a-glance list; S18 remaining text mentions the two deadlines), `docs/RUNBOOK.md` §18 (the two settings), `CLAUDE.md` (the "Every paid call…" bullet)

- [ ] **Step 1: Edit**

- Tracker: move S47 out of Workstream 5's live table into Completed: "Spend caps (exact per-learner admission, deployment ceiling, background pause at 90%, 429) and whole-request bounds: a streamed turn is cut off at `turn_deadline_seconds` keeping its text; other requests answer 504 at `request_deadline_seconds`; a learner can Stop a reply and keep what they saw." Hand-off: "S18 (the two deadlines); alpha caps are an operating decision". Update the At-a-glance counts and the "Next up" line to start at S49. Add any deferred minors from the final review under **S47** in Deferred minors.
- RUNBOOK §18: a short paragraph naming `GURU_TURN_DEADLINE_SECONDS` (120) and `GURU_REQUEST_DEADLINE_SECONDS` (180), what each ends, and that a stopped/timed-out reply is kept with `messages.interrupted` set.
- CLAUDE.md: append to the spend bullet: "A streamed turn has a deadline and can be stopped by the learner, keeping its text (`app/services/turn_control.py`); other requests answer 504 past theirs."

(Confirm the env prefix is `GURU_` by reading `Settings.model_config` in `app/core/config.py`.)

- [ ] **Step 2: Commit**

```bash
git add docs/guru-suggestions-tracker.md docs/RUNBOOK.md CLAUDE.md
git commit -m "docs: record deadlines and stopping a reply [S47]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```
