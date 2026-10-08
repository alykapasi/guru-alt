# Durable Practice Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Paused practice and goal negotiations whose schema is set up by the deploy step, whose checkpoints never outlive their conversation, onboarding session or learner, that a second process can really resume, and that a deploy changing a graph drops cleanly instead of breaking.

**Architecture:** `checkpointing.migrate()` (run by `poe db-upgrade` and `tests.testdb`) owns LangGraph's DDL under an advisory lock; `checkpointing.start()` only checks the version. Each graph stamps its checkpoints with a version through run-config metadata, and one helper (`checkpoints.paused_state`) reads paused state for every resume path, dropping what this code cannot resume. Deletions erase threads through the saver and queue refusals as `pending_erasures`; the worker's purge also expires onboarding sessions and sweeps orphan threads.

**Tech Stack:** Python 3.13, LangGraph 1.2.7 + langgraph-checkpoint-postgres 3.1.2 (psycopg3), SQLAlchemy async + asyncpg, Postgres advisory locks, pytest.

**Spec:** `docs/superpowers/specs/2026-10-08-durable-practice-lifecycle-design.md`

## Global Constraints

- Branch `feat/workstream-2` (open PR #44). Never reset, amend, rebase, squash or force-push.
- Stage only the task's files by name, then `git status`.
- Commit subjects end `[S17]`; every commit message ends with the exact line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never read or print `.env` or any secret; never set `GURU_JEV_SMOKE`; no paid model runs (tests use `fake_llm_client`).
- No Alembic migration: `pending_erasures.kind` is plain text, so `ErasureKind.CHECKPOINT = "checkpoint"` needs none. The four LangGraph tables stay excluded in `db/migrations/env.py`.
- Migrate advisory lock: `pg_advisory_lock(0x47555255, 0x43484B50)` ("GURU", "CHKP").
- Expected checkpoint schema version: `len(AsyncPostgresSaver.MIGRATIONS) - 1`.
- Log events, verbatim: `checkpointer.schema_behind` (error), `checkpointer.schema_ahead` (warning), `checkpointer.incompatible_dropped` (warning; fields `graph`, `expected`, `found`, `thread_id` — never learner text).
- Graph versions start at 1: `WORKFLOW_GRAPH_VERSION`, `REFINEMENT_GRAPH_VERSION`; stamped as run-config `metadata={"graph_version": N}` (verified in LangGraph 1.2.7: run metadata is copied onto checkpoint metadata and returned by `aget_state`).
- Thread keys: conversation → `str(conversation_id)`; onboarding → `onboarding_sessions.thread_key(session_id, learner_id)` = `f"{learner_id}:{session_id}"`.
- Gates: `uv run poe check` (run in the background; ~3 min), `uv run poe format-check`, and `uv run poe db-upgrade` once after Task 1.
- Every finished piece updates `docs/guru-suggestions-tracker.md` (Task 6).

## Review Focus

1. A process that starts while the checkpoint schema is behind must keep serving chat on the volatile saver and say so, never crash or run DDL. Pinned in Task 1 (`test_start_falls_back_when_the_schema_is_behind`, `test_start_runs_no_ddl`).
2. A checkpoint written by an older graph shape must be dropped on resume, with the learner falling back to ordinary chat (practice), "no negotiation open" (refinement) or "expired" (onboarding) — never a crash mid-turn. Pinned in Task 2 for each path, plus an unreadable snapshot.
3. A prune must never delete the thread of a turn that is running right now. Pinned in Task 4 (`test_prune_skips_a_conversation_with_a_turn_running`).
4. A deletion made while the checkpointer is volatile must still erase the durable rows later. Pinned in Task 3 (`test_a_volatile_delete_is_queued_and_retried`).
5. Deleting a conversation while its answer is being graded must end the turn with a clear error and leave no thread. Pinned in Task 5 (`test_deleting_a_conversation_mid_answer_ends_the_turn_cleanly`).

---

### Task 1: Schema setup moves to the deploy step

**Files:**
- Modify: `app/agent/checkpointing.py` (add `MIGRATE_LOCK`, `expected_schema_version`, `schema_version`, `migrate`, `_main`; change `start`; module docstring paragraph "Why a second connection pool")
- Modify: `pyproject.toml` (`db-upgrade`)
- Modify: `tests/testdb.py` (`main`)
- Modify: `db/migrations/env.py` (comment above `CHECKPOINTER_TABLES`)
- Create: `tests/test_checkpoint_schema.py`

**Interfaces:**
- Produces: `checkpointing.expected_schema_version() -> int`; `async checkpointing.schema_version(conn: psycopg.AsyncConnection) -> int | None`; `async checkpointing.migrate(conninfo: str) -> int`; `python -m app.agent.checkpointing migrate`.

- [ ] **Step 1: Write the failing tests**

`tests/test_checkpoint_schema.py`:

```python
"""The checkpointer's schema is the deploy step's job, not every process's (S17).

Every process used to run LangGraph's ``setup()`` as it started: DDL from the app's role, and
two processes starting together raced on ``checkpoint_migrations`` — the loser fell back to
volatile state. Now ``poe db-upgrade`` migrates it once, under a lock, and a process only
checks the version it finds.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.conninfo import make_conninfo

from app.agent import checkpointing
from app.core.config import get_settings


@pytest_asyncio.fixture
async def empty_schema() -> AsyncIterator[str]:
    """A conninfo whose search path is a brand-new, empty schema — a database never migrated."""
    name = f"s17_{uuid.uuid4().hex[:8]}"
    base = checkpointing.psycopg_dsn(get_settings().database_url)
    async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield make_conninfo(base, options=f"-c search_path={name}")
    finally:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA "{name}" CASCADE')


async def _version_at(conninfo: str) -> int | None:
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        return await checkpointing.schema_version(conn)


async def test_an_unmigrated_database_has_no_version(empty_schema: str) -> None:
    assert await _version_at(empty_schema) is None


async def test_migrate_brings_the_schema_to_the_libraries_version(empty_schema: str) -> None:
    assert await checkpointing.migrate(empty_schema) == checkpointing.expected_schema_version()
    assert await _version_at(empty_schema) == len(AsyncPostgresSaver.MIGRATIONS) - 1


async def test_two_deploys_migrating_at_once_both_succeed(empty_schema: str) -> None:
    """Overlapping deploys take turns on the advisory lock instead of racing the DDL."""
    first, second = await asyncio.gather(
        checkpointing.migrate(empty_schema), checkpointing.migrate(empty_schema)
    )
    assert first == second == checkpointing.expected_schema_version()


async def test_start_runs_no_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(_self: object) -> None:
        raise AssertionError("start() must not run setup()")

    monkeypatch.setattr(AsyncPostgresSaver, "setup", refuse)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is True
    finally:
        await checkpointing.stop()


async def test_start_falls_back_when_the_schema_is_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A LangGraph upgrade deployed without ``poe db-upgrade``: serve chat, volatile, loudly."""
    real = checkpointing.expected_schema_version()
    monkeypatch.setattr(checkpointing, "expected_schema_version", lambda: real + 1)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is False
    finally:
        await checkpointing.stop()


async def test_start_falls_back_when_the_schema_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing(_conn: object) -> None:
        return None

    monkeypatch.setattr(checkpointing, "schema_version", missing)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is False
    finally:
        await checkpointing.stop()


async def test_start_stays_durable_when_the_schema_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer release already migrated it: the tables are a superset, so keep them."""
    real = checkpointing.expected_schema_version()
    monkeypatch.setattr(checkpointing, "expected_schema_version", lambda: real - 1)
    await checkpointing.stop()
    try:
        await checkpointing.start(get_settings())
        assert checkpointing.is_durable() is True
    finally:
        await checkpointing.stop()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_checkpoint_schema.py -q`
Expected: FAIL — `AttributeError: module 'app.agent.checkpointing' has no attribute 'schema_version'` / `'migrate'` / `'expected_schema_version'`; `test_start_runs_no_ddl` fails with `is_durable() is False` (start calls the refused `setup()`).

- [ ] **Step 3: Implement**

In `app/agent/checkpointing.py`, add `import sys` and `from typing import Any` to the imports, then below `_lock`:

```python
MIGRATE_LOCK = (0x47555255, 0x43484B50)
"""The advisory lock ``migrate`` holds ("GURU", "CHKP"), so overlapping deploys take turns."""


def expected_schema_version() -> int:
    """The newest checkpoint migration this LangGraph release knows (``setup()`` stores it)."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return len(AsyncPostgresSaver.MIGRATIONS) - 1


async def schema_version(conn: Any) -> int | None:
    """The newest checkpoint migration applied, or ``None`` when the table is missing or empty.

    ``conn`` is an autocommit psycopg connection, so a missing table costs nothing to ask about.
    """
    from psycopg import errors

    try:
        cursor = await conn.execute("SELECT v FROM checkpoint_migrations ORDER BY v DESC LIMIT 1")
        row = await cursor.fetchone()
    except errors.UndefinedTable:
        return None
    if row is None:
        return None
    return int(row["v"] if isinstance(row, dict) else row[0])


async def migrate(conninfo: str) -> int:
    """Create or upgrade LangGraph's checkpoint tables. Returns the version now applied.

    The deploy step's job (``poe db-upgrade``), never a running process's: it is DDL, and two
    callers at once would race on ``checkpoint_migrations`` — hence the advisory lock.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(
        conninfo, autocommit=True, prepare_threshold=0, row_factory=dict_row
    ) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s, %s)", MIGRATE_LOCK)
        try:
            await AsyncPostgresSaver(conn).setup()  # ty: ignore[invalid-argument-type]
            version = await schema_version(conn)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s, %s)", MIGRATE_LOCK)
    if version is None:
        raise RuntimeError("checkpoint migrations ran but recorded no version")
    return version
```

Replace the body of `start` from `pool = AsyncConnectionPool(` through the final `log.info(...)` with:

```python
            pool = AsyncConnectionPool(
                conninfo=psycopg_dsn(settings.database_url),
                min_size=settings.checkpointer_pool_min_size,
                max_size=settings.checkpointer_pool_max_size,
                # LangGraph's Postgres saver requires all three: it runs its own transactions,
                # and pipelines statements that a prepared-statement cache would break.
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=False,
            )
            await pool.open(wait=True, timeout=settings.checkpointer_connect_timeout_seconds)
            async with pool.connection() as conn:
                found = await schema_version(conn)
        except Exception as exc:
            # Chat still works; paused conversations still will not survive a restart. Both
            # halves of that are true and both are reported rather than one being assumed.
            log.error("checkpointer.durable_start_failed", error=str(exc))
            _saver, _pool = _VOLATILE, None
            return
        expected = expected_schema_version()
        if found is None or found < expected:
            # Deployed without `poe db-upgrade` after a LangGraph upgrade, or never migrated.
            log.error("checkpointer.schema_behind", expected=expected, found=found)
            await pool.close()
            _saver, _pool = _VOLATILE, None
            return
        if found > expected:
            log.warning("checkpointer.schema_ahead", expected=expected, found=found)
        _saver = AsyncPostgresSaver(pool)  # ty: ignore[invalid-argument-type]
        _pool = pool
        log.info("checkpointer.durable", min_size=pool.min_size, max_size=pool.max_size)
```

Change `start`'s docstring to `"""Open the checkpointer's pool and check its schema version. Idempotent."""`. In the module docstring, replace the paragraph starting `**Why a second connection pool.**` with:

```
**Why a second connection pool.** The checkpointer is LangGraph's own schema and it speaks
psycopg3 while the application speaks asyncpg through SQLAlchemy. Hand-writing its tables into
Alembic would fork a schema the library owns and upgrades; pointing it at our engine is not
possible across drivers. So it gets its own small pool against the same database, opened once
and closed with the app. Its tables are created and upgraded by the deploy step
(:func:`migrate`, run by ``poe db-upgrade``); a process only checks the version it finds.
```

At the end of the file:

```python
def _main(argv: list[str]) -> None:
    if argv != ["migrate"]:
        raise SystemExit("usage: python -m app.agent.checkpointing migrate")
    version = asyncio.run(migrate(psycopg_dsn(get_settings().database_url)))
    print(f"checkpointer schema at version {version}")


if __name__ == "__main__":
    _main(sys.argv[1:])
```

`pyproject.toml`, replace the `db-upgrade` line:

```toml
db-upgrade = { sequence = [{ cmd = "alembic upgrade head" }, { cmd = "python -m app.agent.checkpointing migrate" }], help = "Apply Alembic migrations, then LangGraph's checkpoint schema" }
```

`tests/testdb.py` `main`, after `command.upgrade(Config("alembic.ini"), "head")`:

```python
    from app.agent.checkpointing import migrate, psycopg_dsn

    asyncio.run(migrate(psycopg_dsn(url)))
```

`db/migrations/env.py`, replace the first comment line pair with:

```python
# LangGraph's checkpointer owns its own tables; the deploy step creates and upgrades them
# (``poe db-upgrade`` runs ``python -m app.agent.checkpointing migrate`` after Alembic). They are not in
```

(keep the rest of that comment as is).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_checkpoint_schema.py tests/test_durable_state.py -q`
Expected: PASS (all).

Run: `uv run poe db-upgrade && uv run python -m tests.testdb`
Expected: Alembic output, then `checkpointer schema at version N`; testdb prints `database '…_test' at head`.

- [ ] **Step 5: Commit**

```bash
git add app/agent/checkpointing.py pyproject.toml tests/testdb.py db/migrations/env.py tests/test_checkpoint_schema.py
git commit -m "feat(checkpoints): the deploy step migrates the checkpoint schema; processes only check it [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 2: Graph versions, and one way to read paused state

**Files:**
- Modify: `app/agent/workflow.py:45-46` (`WORKFLOW_GRAPH_VERSION`, `workflow_config`)
- Modify: `app/agent/refinement.py:31-32` (`REFINEMENT_GRAPH_VERSION`, `refinement_config`)
- Modify: `app/services/checkpoints.py` (`compatible`, `paused_state`)
- Modify: `app/services/workflow.py:59-98` (`paused_item_id`)
- Modify: `app/services/refinement.py:34-42` (`is_awaiting_reply`)
- Modify: `app/services/onboarding.py:66-75` (`EXPIRED_DETAIL`, resume check)
- Modify: `app/api/v1/onboarding.py:120-121` (forget the session on expiry)
- Create: `tests/test_graph_versions.py`

**Interfaces:**
- Produces: `WORKFLOW_GRAPH_VERSION: int = 1`, `REFINEMENT_GRAPH_VERSION: int = 1`; `checkpoints.compatible(snapshot: StateSnapshot, version: int) -> bool`; `async checkpoints.paused_state(graph: CompiledStateGraph, config: RunnableConfig, *, graph_name: str, version: int) -> StateSnapshot | None`; `onboarding.EXPIRED_DETAIL: str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_graph_versions.py`:

```python
"""A deploy that changes a graph drops what it can no longer resume (S17).

A checkpoint is the state of one graph shape. Resume it under another — a renamed node, a new
state key — and it runs the wrong node or raises mid-turn. Each graph carries a version, every
checkpoint is stamped with it, and a checkpoint stamped with another (or none, or unreadable)
is discarded and the learner falls back cleanly.
"""

import hashlib
import json
import uuid
from typing import Any, get_type_hints

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.agent.refinement import (
    REFINEMENT_GRAPH_VERSION,
    RefinementState,
    build_refinement_graph,
    refinement_config,
)
from app.agent.workflow import (
    WORKFLOW_GRAPH_VERSION,
    WorkflowState,
    build_workflow_graph,
    workflow_config,
)
from app.api.deps import get_llm_client
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole, Usage
from app.main import app
from app.models.chat import OnboardingSession
from app.models.learner import Learner
from app.services import checkpoints, onboarding
from app.services import refinement as refinement_svc
from app.services import workflow as workflow_svc
from tests.test_workflow import PRESENT, _conversation_with_active_step, _drain

API = "/api/v1"
PROPOSAL = "Learn the basics of photosynthesis in two weeks."


# --- the stamp --------------------------------------------------------------------------------


async def test_a_paused_question_carries_its_graph_version(db_session: AsyncSession) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    await _drain(db_session, llm, conv, user_content="let's practice")

    graph = build_workflow_graph(llm, db_session, learner_id=conv.learner_id)
    snapshot = await graph.aget_state(workflow_config(str(conv.id)))
    assert snapshot.metadata["graph_version"] == WORKFLOW_GRAPH_VERSION


# --- practice ---------------------------------------------------------------------------------


async def test_a_paused_question_from_another_graph_version_is_dropped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    await _drain(db_session, llm, conv, user_content="let's practice")
    assert await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)

    monkeypatch.setattr(workflow_svc, "WORKFLOW_GRAPH_VERSION", WORKFLOW_GRAPH_VERSION + 1)
    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )
    graph = build_workflow_graph(llm, db_session, learner_id=conv.learner_id)
    assert (await graph.aget_state(workflow_config(str(conv.id)))).values == {}


async def test_an_unstamped_paused_question_is_dropped(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything paused before this change shipped: dropped once, on first resume."""
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
    monkeypatch.setattr(
        workflow_svc, "workflow_config", lambda t: {"configurable": {"thread_id": t}}
    )
    await _drain(db_session, llm, conv, user_content="let's practice")
    monkeypatch.undo()

    assert (
        await workflow_svc.paused_item_id(llm, db_session, conv.id, learner_id=conv.learner_id)
        is None
    )


async def test_an_unreadable_checkpoint_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    discarded: list[str] = []

    async def record(thread_id: str) -> bool:
        discarded.append(thread_id)
        return True

    class _Broken:
        async def aget_state(self, _config: Any) -> Any:
            raise ValueError("cannot deserialize")

    monkeypatch.setattr(checkpointing, "discard_thread", record)
    snapshot = await checkpoints.paused_state(
        _Broken(),  # ty: ignore[invalid-argument-type]
        workflow_config("t-broken"),
        graph_name="workflow",
        version=WORKFLOW_GRAPH_VERSION,
    )
    assert snapshot is None
    assert discarded == ["t-broken"]


# --- refinement -------------------------------------------------------------------------------


async def test_a_negotiation_from_another_graph_version_is_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = fake_llm_client(script=[FakeTurn(text=PROPOSAL)])
    conversation_id = uuid.uuid4()
    graph = build_refinement_graph(llm)
    await graph.ainvoke(
        {
            "messages": [ChatMessage(role=ChatRole.USER, content="photosynthesis")],
            "system": "s",
            "max_tokens": 100,
            "max_rounds": 3,
            "proposal": "",
            "usage": Usage(),
            "rounds": 0,
            "satisfied": False,
            "auto_committed": False,
            "agreed_goal": "",
        },
        refinement_config(str(conversation_id)),
    )
    assert await refinement_svc.is_awaiting_reply(llm, conversation_id) is True

    monkeypatch.setattr(refinement_svc, "REFINEMENT_GRAPH_VERSION", REFINEMENT_GRAPH_VERSION + 1)
    assert await refinement_svc.is_awaiting_reply(llm, conversation_id) is False


# --- onboarding -------------------------------------------------------------------------------


@pytest.fixture
def onboarding_llm() -> Any:
    client = fake_llm_client(script=[FakeTurn(text=PROPOSAL)])
    app.dependency_overrides[get_llm_client] = lambda: client
    yield client
    app.dependency_overrides.pop(get_llm_client, None)


def _sse(text: str) -> list[dict]:
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


async def test_an_onboarding_session_from_another_graph_version_expires(
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
    onboarding_llm: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = (await api_client.post(f"{API}/onboarding/goal-sessions")).json()["session_id"]
    r = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={"session_id": session_id, "content": "photosynthesis", "mode": "start"},
    )
    assert any(e["type"] == "awaiting_reply" for e in _sse(r.text))

    monkeypatch.setattr(onboarding, "REFINEMENT_GRAPH_VERSION", REFINEMENT_GRAPH_VERSION + 1)
    r = await api_client.post(
        f"{API}/onboarding/goal-turns",
        json={"session_id": session_id, "content": "yes", "mode": "resume", "satisfied": True},
    )
    errors = [e for e in _sse(r.text) if e["type"] == "error"]
    assert errors and errors[0]["detail"] == onboarding.EXPIRED_DETAIL
    gone = await db_session.scalar(
        select(OnboardingSession).where(OnboardingSession.session_id == session_id)
    )
    assert gone is None


# --- the bump guard ---------------------------------------------------------------------------


def _fingerprint(compiled: Any, state: type) -> str:
    g = compiled.get_graph()
    shape = {
        "nodes": sorted(g.nodes),
        "edges": sorted([e.source, e.target, bool(e.conditional)] for e in g.edges),
        "state": sorted(get_type_hints(state)),
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


PINNED = {
    "workflow": (1, "980c9d6b8ca529aa"),
    "refinement": (1, "36fd942e189dc05d"),
}


def test_a_graph_shape_change_bumps_its_version() -> None:
    llm = fake_llm_client()
    current = {
        "workflow": (
            WORKFLOW_GRAPH_VERSION,
            _fingerprint(
                build_workflow_graph(llm, AsyncSession(), learner_id=uuid.uuid4()), WorkflowState
            ),
        ),
        "refinement": (
            REFINEMENT_GRAPH_VERSION,
            _fingerprint(build_refinement_graph(llm), RefinementState),
        ),
    }
    for name, pinned in PINNED.items():
        assert current[name] == pinned, (
            f"the {name} graph changed shape: bump {name.upper()}_GRAPH_VERSION and update "
            f"PINNED[{name!r}] to {current[name]}"
        )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_graph_versions.py -q`
Expected: FAIL at collection — `ImportError: cannot import name 'REFINEMENT_GRAPH_VERSION'`.

- [ ] **Step 3: Implement**

`app/agent/workflow.py`, replace `workflow_config`:

```python
WORKFLOW_GRAPH_VERSION = 1
"""Bump when this graph's nodes, edges or state keys change (S17). Every checkpoint is stamped
with it and one stamped otherwise is dropped on resume; ``tests/test_graph_versions.py`` pins
the shape so a change without a bump fails."""


def workflow_config(thread_id: str) -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {"graph_version": WORKFLOW_GRAPH_VERSION},
    }
```

`app/agent/refinement.py`, replace `refinement_config` the same way with `REFINEMENT_GRAPH_VERSION = 1` (docstring as above, "this graph" — onboarding uses it too).

`app/services/checkpoints.py`: add imports `from typing import Any`, `from langchain_core.runnables import RunnableConfig`, `from langgraph.types import StateSnapshot`, then after `prune`:

```python
def compatible(snapshot: StateSnapshot, version: int) -> bool:
    """Whether a checkpoint was written by the graph shape this code resumes."""
    return (snapshot.metadata or {}).get("graph_version") == version


async def paused_state(
    graph: Any, config: RunnableConfig, *, graph_name: str, version: int
) -> StateSnapshot | None:
    """The paused snapshot this code can resume, or ``None``.

    ``None`` when nothing is paused, and when something is but this code cannot resume it — a
    checkpoint from another graph version, an unstamped one from before versions existed, or
    one that cannot be read at all. Those are discarded here, once, so the caller's fallback
    (ordinary chat, no negotiation, an expired session) is what the learner gets.
    """
    thread_id = config["configurable"]["thread_id"]
    found: object
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        found = "unreadable"
    else:
        if not snapshot.next:
            return None
        if compatible(snapshot, version):
            return snapshot
        found = (snapshot.metadata or {}).get("graph_version")
    log.warning(
        "checkpointer.incompatible_dropped",
        graph=graph_name,
        expected=version,
        found=found,
        thread_id=thread_id,
    )
    await checkpointing.discard_thread(thread_id)
    return None
```

`app/services/workflow.py`: import `WORKFLOW_GRAPH_VERSION` alongside `workflow_config` from `app.agent.workflow`; in `paused_item_id` replace

```python
    config = workflow_config(str(conversation_id))
    snapshot = await graph.aget_state(config)
    if not snapshot.next:
        return None
```

with

```python
    snapshot = await checkpoints.paused_state(
        graph,
        workflow_config(str(conversation_id)),
        graph_name="workflow",
        version=WORKFLOW_GRAPH_VERSION,
    )
    if snapshot is None:
        return None
```

`app/services/refinement.py`: import `REFINEMENT_GRAPH_VERSION` from `app.agent.refinement` and `from app.services import checkpoints`; `is_awaiting_reply` becomes:

```python
async def is_awaiting_reply(llm: LLMClient, conversation_id: uuid.UUID) -> bool:
    """Whether the gate is paused mid-negotiation for this conversation, in a shape this code
    can resume (see ``checkpoints.paused_state``)."""
    graph = build_refinement_graph(llm)
    snapshot = await checkpoints.paused_state(
        graph,
        refinement_config(str(conversation_id)),
        graph_name="refinement",
        version=REFINEMENT_GRAPH_VERSION,
    )
    return snapshot is not None
```

`app/services/onboarding.py`: import `REFINEMENT_GRAPH_VERSION` from `app.agent.refinement` and `checkpoints` from `app.services`; add below `log`:

```python
EXPIRED_DETAIL = "this goal session has expired; start a new one"
```

and replace the resume check `if not (await graph.aget_state(config)).values:` block with:

```python
        if (
            await checkpoints.paused_state(
                graph, config, graph_name="refinement", version=REFINEMENT_GRAPH_VERSION
            )
            is None
        ):
            yield TurnEvent(type="error", detail=EXPIRED_DETAIL)
            return
```

(keep the comment above it, adding "or it was written by another version of the graph" to its list).

`app/api/v1/onboarding.py`, the `error` branch of `event_stream`:

```python
                    elif event.type == "error":
                        if event.detail == onboarding.EXPIRED_DETAIL:
                            # Nothing left to resume, so nothing left to own (S17).
                            await onboarding_sessions.forget(session, request.session_id)
                        yield _sse(error_frame(event))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_graph_versions.py tests/test_workflow.py tests/test_practice_pause.py tests/test_refinement.py tests/test_onboarding.py tests/test_onboarding_api.py tests/test_durable_state.py tests/test_checkpoint_lifecycle.py -q`
Expected: PASS. If an onboarding test resumed a negotiation that had already committed and expected `committed` again, it now gets `EXPIRED_DETAIL` (nothing is paused): record a ruling and update that test's expectation.

- [ ] **Step 5: Commit**

```bash
git add app/agent/workflow.py app/agent/refinement.py app/services/checkpoints.py app/services/workflow.py app/services/refinement.py app/services/onboarding.py app/api/v1/onboarding.py tests/test_graph_versions.py
git commit -m "feat(checkpoints): graphs stamp a version and a deploy drops what it cannot resume [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 3: Deleting a conversation or an account erases its threads

**Files:**
- Modify: `app/agent/checkpointing.py` (`delete_thread`, `erase_threads`)
- Modify: `app/models/erasure.py` (`ErasureKind.CHECKPOINT`)
- Modify: `app/services/removal.py:261-282` (`delete_conversation`)
- Modify: `app/services/retention.py` (`RETENTION` entries, `DeletionReport.threads_failed`, `delete_learner`, `erase_learner`, `_retry_one`)
- Modify: `app/workers/tasks.py:425-433` (`_start_checkpoint_purge`)
- Modify: `tests/conftest.py` (fixture `durable_checkpointer`)
- Create: `tests/checkpoint_helpers.py`, `tests/test_checkpoint_erasure.py`

**Interfaces:**
- Consumes: Task 1's `start` (durable when migrated).
- Produces: `async checkpointing.delete_thread(thread_id: str) -> None` (raises when volatile or on failure); `async checkpointing.erase_threads(thread_ids: Iterable[str]) -> list[str]` (the ids it could not erase); `ErasureKind.CHECKPOINT = "checkpoint"`; `DeletionReport.threads_failed: list[str]`; test helpers `put_checkpoint(thread_id: str) -> None`, `has_checkpoint(thread_id: str) -> bool`; fixture `durable_checkpointer`.

- [ ] **Step 1: Write the helpers and the failing tests**

`tests/checkpoint_helpers.py`:

```python
"""Writing and reading raw checkpoints through whichever saver is current."""

import uuid

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import Checkpoint, CheckpointMetadata

from app.agent import checkpointing


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


async def put_checkpoint(thread_id: str) -> None:
    checkpoint: Checkpoint = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": "",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": None,
    }
    metadata: CheckpointMetadata = {"source": "update", "step": 1, "parents": {}}
    await checkpointing.checkpointer().aput(_config(thread_id), checkpoint, metadata, {})


async def has_checkpoint(thread_id: str) -> bool:
    return bool([c async for c in checkpointing.checkpointer().alist(_config(thread_id))])
```

`tests/conftest.py`, add (with `from app.agent import checkpointing` in the imports):

```python
@pytest_asyncio.fixture
async def durable_checkpointer() -> AsyncIterator[None]:
    """The real Postgres saver for one test. Its writes commit on its own pool, so a test that
    uses it removes the threads it made."""
    await checkpointing.stop()
    await checkpointing.start(get_settings())
    assert checkpointing.is_durable()
    try:
        yield
    finally:
        await checkpointing.stop()
```

`tests/test_checkpoint_erasure.py`:

```python
"""No checkpoint outlives its conversation or its learner (S17, V12).

A paused practice or negotiation holds what the learner typed. Deleting the conversation or the
account used to leave it in the checkpoint tables for good; now the thread is erased with its
owner, and an erase the saver refuses is queued and retried like a refused blob.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import checkpointing
from app.core.config import get_settings
from app.models.chat import Conversation
from app.models.erasure import ErasureKind, PendingErasure
from app.models.learner import Learner
from app.services import onboarding_sessions, removal, retention
from app.storage.memory import InMemoryBlobStore
from app.workers import tasks
from tests.checkpoint_helpers import has_checkpoint, put_checkpoint


async def _learner_with_conversation(session: AsyncSession) -> tuple[Learner, Conversation]:
    learner = Learner(handle=f"ce-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    conversation = Conversation(learner_id=learner.id)
    session.add(conversation)
    await session.commit()
    return learner, conversation


async def _queued(session: AsyncSession, target: str) -> PendingErasure | None:
    return await session.scalar(
        select(PendingErasure)
        .where(PendingErasure.kind == ErasureKind.CHECKPOINT, PendingErasure.target == target)
        .execution_options(populate_existing=True)
    )


async def test_erase_threads_deletes_through_the_saver(durable_checkpointer: None) -> None:
    thread = f"t-{uuid.uuid4().hex}"
    await put_checkpoint(thread)
    assert await checkpointing.erase_threads([thread]) == []
    assert not await has_checkpoint(thread)


async def test_a_volatile_saver_refuses_every_erase() -> None:
    """A process running volatile cannot reach the durable rows, so it must not claim it did."""
    await checkpointing.stop()
    assert await checkpointing.erase_threads(["a", "b"]) == ["a", "b"]


async def test_deleting_a_conversation_erases_its_thread(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    await put_checkpoint(str(conversation.id))

    await removal.delete_conversation(db_session, learner.id, conversation.id, forget=False)

    assert not await has_checkpoint(str(conversation.id))
    assert await _queued(db_session, str(conversation.id)) is None


async def test_a_volatile_delete_is_queued_and_retried(db_session: AsyncSession) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    await checkpointing.stop()  # volatile

    await removal.delete_conversation(db_session, learner.id, conversation.id, forget=False)
    row = await _queued(db_session, str(conversation.id))
    assert row is not None

    now = datetime.now(UTC) + timedelta(seconds=1)
    assert await retention.retry_erasures(db_session, InMemoryBlobStore(), None, now=now) == 0
    row = await _queued(db_session, str(conversation.id))
    assert row is not None and row.attempts == 1  # still volatile: refused, backed off

    await checkpointing.start(get_settings())
    try:
        await put_checkpoint(str(conversation.id))
        later = now + timedelta(days=1)
        assert await retention.retry_erasures(db_session, InMemoryBlobStore(), None, now=later) == 1
        assert not await has_checkpoint(str(conversation.id))
        assert await _queued(db_session, str(conversation.id)) is None
    finally:
        await checkpointing.stop()


async def test_erasing_an_account_erases_every_thread_it_had(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    onboarding_thread = onboarding_sessions.thread_key(record.session_id, learner.id)
    await put_checkpoint(str(conversation.id))
    await put_checkpoint(onboarding_thread)

    await retention.erase_learner(db_session, InMemoryBlobStore(), None, learner.id)

    assert not await has_checkpoint(str(conversation.id))
    assert not await has_checkpoint(onboarding_thread)


async def test_an_account_erased_while_volatile_queues_its_threads(
    db_session: AsyncSession,
) -> None:
    learner, conversation = await _learner_with_conversation(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    await checkpointing.stop()

    report = await retention.erase_learner(db_session, InMemoryBlobStore(), None, learner.id)

    expected = {str(conversation.id), onboarding_sessions.thread_key(record.session_id, learner.id)}
    assert set(report.threads_failed) == expected
    for thread in expected:
        assert await _queued(db_session, thread) is not None


async def test_the_worker_opens_the_checkpointer_even_without_the_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Erasure retries run in the worker; without a durable saver every one is refused."""
    no_purge = get_settings().model_copy(update={"checkpoint_purge_interval_seconds": 0})
    monkeypatch.setattr(tasks, "get_settings", lambda: no_purge)
    state = SimpleNamespace()
    await checkpointing.stop()
    try:
        await tasks._start_checkpoint_purge(state)  # ty: ignore[invalid-argument-type]
        assert checkpointing.is_durable() is True
    finally:
        await tasks._stop_checkpoint_purge(state)  # ty: ignore[invalid-argument-type]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_checkpoint_erasure.py -q`
Expected: FAIL — `AttributeError: module 'app.agent.checkpointing' has no attribute 'erase_threads'`; `ErasureKind` has no `CHECKPOINT`.

- [ ] **Step 3: Implement**

`app/models/erasure.py`:

```python
class ErasureKind(StrEnum):
    BLOB = "blob"
    IDENTITY = "identity"
    CHECKPOINT = "checkpoint"
```

`app/agent/checkpointing.py`, add `from collections.abc import Iterable` and after `discard_thread`:

```python
async def delete_thread(thread_id: str) -> None:
    """Delete one thread's checkpoints, or raise. For erasure, where a refusal must be retried.

    A volatile saver is a refusal: it cannot reach the durable rows a previous process wrote.
    """
    if not is_durable():
        raise RuntimeError("checkpointer is volatile")
    await checkpointer().adelete_thread(thread_id)


async def erase_threads(thread_ids: Iterable[str]) -> list[str]:
    """Erase each thread; returns the ids that could not be erased, for the caller to queue."""
    failed: list[str] = []
    for thread_id in thread_ids:
        try:
            await delete_thread(thread_id)
        except Exception as exc:
            log.warning("checkpointer.erase_failed", thread_id=thread_id, error=str(exc))
            failed.append(thread_id)
    return failed
```

`app/services/removal.py`: add `from app.agent import checkpointing`; in `delete_conversation`, after `await session.commit()`:

```python
    # The paused practice or negotiation holds what they typed (S17, V12).
    for thread_id in await checkpointing.erase_threads([str(conversation_id)]):
        await retention.queue_erasure(
            session, ErasureKind.CHECKPOINT, thread_id, "refused at conversation delete"
        )
```

`app/services/retention.py`:

- import `from app.agent import checkpointing` and `OnboardingSession` from `app.models.chat`, and `from app.services import onboarding_sessions`.
- In `RETENTION`, after the `onboarding_sessions` entry, replace that entry's reason and add one:

```python
    StoreRetention(
        "onboarding_sessions",
        "deleted",
        "Cascades from the learner, and expires after the same idle window as a paused "
        "conversation, together with the negotiation it owns.",
    ),
    StoreRetention(
        "checkpoints/checkpoint_blobs/checkpoint_writes",
        "deleted",
        "A paused practice question or goal negotiation. Erased with its conversation, its "
        "onboarding session or the account; idle ones are pruned and ownerless ones swept.",
    ),
```

- `DeletionReport`, after `blobs_failed`:

```python
    # Paused-state threads the checkpointer could not erase (volatile, or refused); queued.
    threads_failed: list[str] = field(default_factory=list)
```

- `delete_learner`, before `authored = ...`:

```python
    thread_ids = [str(cid) for cid in (await session.scalars(
        select(Conversation.id).where(Conversation.learner_id == learner_id)
    )).all()] + [
        onboarding_sessions.thread_key(sid, learner_id)
        for sid in (await session.scalars(
            select(OnboardingSession.session_id).where(OnboardingSession.learner_id == learner_id)
        )).all()
    ]
```

  and after the `for source_id in source_ids:` loop:

```python
    report.threads_failed = await checkpointing.erase_threads(thread_ids)
```

- `erase_learner`, after the blob queue loop:

```python
    for thread_id in report.threads_failed:
        await queue_erasure(session, ErasureKind.CHECKPOINT, thread_id, "refused at account erase")
```

- `_retry_one`, after the `BLOB` branch:

```python
        elif row.kind == ErasureKind.CHECKPOINT:
            await checkpointing.delete_thread(row.target)
```

`app/workers/tasks.py`, `_start_checkpoint_purge`:

```python
async def _start_checkpoint_purge(state: TaskiqState) -> None:
    # The worker owns its own checkpointer pool, whether or not it purges: pruning and
    # erasure retries go through the saver rather than through SQL (see
    # ``app.agent.checkpointing.delete_thread``), and against the volatile fallback the purge
    # would discard nothing and every queued checkpoint erasure would be refused.
    await checkpointing.start()
    interval = get_settings().checkpoint_purge_interval_seconds
    if interval <= 0:
        return
    state.checkpoint_purge = asyncio.create_task(_purge_checkpoints_loop(interval))
```

Run `uv run ruff format app/services/retention.py` after editing.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_checkpoint_erasure.py tests/test_retention.py tests/test_account_deletion.py tests/test_erasure_retry.py tests/test_removal.py tests/test_hosted_identity_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/agent/checkpointing.py app/models/erasure.py app/services/removal.py app/services/retention.py app/workers/tasks.py tests/conftest.py tests/checkpoint_helpers.py tests/test_checkpoint_erasure.py
git commit -m "feat(checkpoints): deleting a conversation or an account erases its paused state [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 4: Onboarding expiry, orphan sweep, and a prune that respects a running turn

**Files:**
- Modify: `app/core/db.py` (`engine_of`)
- Modify: `app/services/ingestion.py:672-675` (use `engine_of`; delete `_engine_of`)
- Modify: `app/services/onboarding_sessions.py` (`require` touches `updated_at`)
- Modify: `app/services/checkpoints.py` (`prune`, `prune_orphans`, `_discard_unless_busy`)
- Modify: `app/workers/tasks.py:223-231` (`_purge_checkpoints_once`)
- Modify: `tests/test_checkpoint_lifecycle.py` (new tests)

**Interfaces:**
- Consumes: Task 3's `put_checkpoint`, `has_checkpoint`, `durable_checkpointer`, `checkpointing.erase_threads`.
- Produces: `app.core.db.engine_of(session: AsyncSession) -> AsyncEngine`; `async checkpoints.prune(session, *, older_than) -> int` (now conversations + onboarding sessions); `async checkpoints.prune_orphans(session: AsyncSession) -> int`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_checkpoint_lifecycle.py` (add imports `from sqlalchemy import select, update`, `from sqlalchemy.ext.asyncio import AsyncEngine`, `from app.models.chat import OnboardingSession`, `from app.services import onboarding_sessions, turn_lock`, `from tests.checkpoint_helpers import has_checkpoint, put_checkpoint`):

```python
# --- what else ends: onboarding, orphans, and never a running turn ---------------------------


async def test_prune_skips_a_conversation_with_a_turn_running(
    db_session: AsyncSession, engine: AsyncEngine
) -> None:
    """A learner answering a weeks-old question right now is not an abandoned conversation."""
    learner = await _learner(db_session)
    stale = await _conversation(db_session, learner, age=timedelta(days=40))
    await put_checkpoint(str(stale.id))

    claim = await turn_lock.claim(engine, stale.id)
    assert claim is not None
    try:
        assert await svc.prune(db_session, older_than=timedelta(days=30)) == 0
        assert await has_checkpoint(str(stale.id))
    finally:
        await claim.release()


async def test_an_idle_onboarding_session_expires_with_its_negotiation(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    thread = onboarding_sessions.thread_key(record.session_id, learner.id)
    await put_checkpoint(thread)
    await db_session.execute(
        update(OnboardingSession)
        .where(OnboardingSession.session_id == record.session_id)
        .values(updated_at=_naive(timedelta(days=40)))
    )

    assert await svc.prune(db_session, older_than=timedelta(days=30)) == 1
    assert not await has_checkpoint(thread)
    assert (
        await db_session.scalar(
            select(OnboardingSession).where(OnboardingSession.session_id == record.session_id)
        )
        is None
    )


async def test_a_live_onboarding_session_is_left_alone(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    assert await svc.prune(db_session, older_than=timedelta(days=30)) == 0
    assert await db_session.get(OnboardingSession, record.session_id) is not None


async def test_taking_a_round_keeps_an_onboarding_session_alive(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    record = await onboarding_sessions.issue(db_session, learner.id)
    old = _naive(timedelta(days=40))
    await db_session.execute(
        update(OnboardingSession)
        .where(OnboardingSession.session_id == record.session_id)
        .values(updated_at=old)
    )

    found = await onboarding_sessions.require(db_session, record.session_id, learner.id)
    await db_session.refresh(found)
    assert found.updated_at > old


async def test_the_sweep_removes_threads_nothing_owns(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    learner = await _learner(db_session)
    conversation = await _conversation(db_session, learner, age=timedelta(hours=1))
    record = await onboarding_sessions.issue(db_session, learner.id)
    owned = [str(conversation.id), onboarding_sessions.thread_key(record.session_id, learner.id)]
    orphans = [str(uuid.uuid4()), f"{learner.id}:never-issued", "not-a-thread-we-make"]
    for thread in owned + orphans:
        await put_checkpoint(thread)
    try:
        assert await svc.prune_orphans(db_session) >= len(orphans)
        for thread in orphans:
            assert not await has_checkpoint(thread)
        for thread in owned:
            assert await has_checkpoint(thread)
    finally:
        await checkpointing.erase_threads(owned + orphans)


async def test_the_sweep_does_nothing_without_a_durable_saver(db_session: AsyncSession) -> None:
    await checkpointing.stop()
    assert await svc.prune_orphans(db_session) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_checkpoint_lifecycle.py -q`
Expected: FAIL — `prune` returns 1 with the turn running; onboarding not expired (0); `updated_at` unchanged; `AttributeError: ... has no attribute 'prune_orphans'`.

- [ ] **Step 3: Implement**

`app/core/db.py`, add (importing `AsyncEngine`, `AsyncSession` if not already):

```python
def engine_of(session: AsyncSession) -> AsyncEngine:
    """The engine behind ``session``, for work that needs its own connection to the same
    database (advisory locks: ingestion slots, turn claims)."""
    bind = session.bind
    return bind if isinstance(bind, AsyncEngine) else bind.engine
```

`app/services/ingestion.py`: delete `_engine_of`, import `engine_of` from `app.core.db`, and replace each `_engine_of(` with `engine_of(`. (If ty flags `bind` as possibly `None` in `engine_of`, keep exactly the annotation the old `_engine_of` used.)

`app/services/onboarding_sessions.py` `require`, before `return record` (import `func`, `update` from sqlalchemy):

```python
    # A round is activity: the idle window that expires a session counts from here (S17).
    await session.execute(
        update(OnboardingSession)
        .where(OnboardingSession.session_id == session_id)
        .values(updated_at=func.now())
    )
    await session.commit()
```

`app/services/checkpoints.py` (imports: `from sqlalchemy import delete, func, select, text`, `from app.core.db import engine_of`, `from app.models.chat import Conversation, Message, OnboardingSession`, `from app.services import onboarding_sessions, turn_lock`):

```python
async def _discard_unless_busy(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Discard a conversation's thread unless a turn is running in it right now.

    Taken through the same claim a turn takes, so a learner answering a weeks-old question at
    the moment the sweep runs keeps their question.
    """
    claim = await turn_lock.claim(engine_of(session), conversation_id)
    if claim is None:
        return False
    try:
        return await checkpointing.discard_thread(str(conversation_id))
    finally:
        await claim.release()


async def prune(session: AsyncSession, *, older_than: timedelta) -> int:
    """Discard paused state nobody has touched since the cutoff; returns threads discarded.

    Conversations: both graphs key their thread on the conversation id, so one discard covers
    whichever left state behind. Onboarding sessions: the row goes with its negotiation.
    """
    ids = await stale_conversation_ids(session, older_than=older_than)
    discarded = 0
    for conversation_id in ids:
        if await _discard_unless_busy(session, conversation_id):
            discarded += 1
    cutoff = (datetime.now(UTC) - older_than).replace(tzinfo=None)
    idle = (
        await session.execute(
            select(OnboardingSession.session_id, OnboardingSession.learner_id).where(
                OnboardingSession.updated_at < cutoff
            )
        )
    ).all()
    for session_id, learner_id in idle:
        await checkpointing.discard_thread(onboarding_sessions.thread_key(session_id, learner_id))
        await onboarding_sessions.forget(session, session_id)
        discarded += 1
    if discarded:
        log.info("checkpoints.pruned", threads=discarded, candidates=len(ids) + len(idle))
    return discarded


async def prune_orphans(session: AsyncSession) -> int:
    """Delete every thread whose conversation or onboarding session no longer exists.

    The backstop for erasures that never ran — threads from before S17's erasure, or a crash
    between a delete's commit and its erase. Reads thread ids with plain SQL; deletes through
    the saver, so the library still owns every write to its tables. An owner row is always
    committed before its thread is first written, so a thread created during the sweep is
    never mistaken for an orphan.
    """
    if not checkpointing.is_durable():
        return 0
    threads = list(await session.scalars(text("SELECT DISTINCT thread_id FROM checkpoints")))
    conversations: dict[uuid.UUID, str] = {}
    negotiations: dict[tuple[uuid.UUID, str], str] = {}
    orphans: list[str] = []
    for thread in threads:
        learner_part, colon, session_part = thread.partition(":")
        try:
            if colon:
                negotiations[(uuid.UUID(learner_part), session_part)] = thread
            else:
                conversations[uuid.UUID(thread)] = thread
        except ValueError:
            orphans.append(thread)
    live_conversations = set(
        await session.scalars(select(Conversation.id).where(Conversation.id.in_(conversations)))
    )
    live_negotiations = {
        (row.learner_id, row.session_id)
        for row in await session.execute(
            select(OnboardingSession.learner_id, OnboardingSession.session_id).where(
                OnboardingSession.session_id.in_([sid for _lid, sid in negotiations])
            )
        )
    }
    orphans += [t for cid, t in conversations.items() if cid not in live_conversations]
    orphans += [t for key, t in negotiations.items() if key not in live_negotiations]
    failed = await checkpointing.erase_threads(orphans)
    swept = len(orphans) - len(failed)
    if swept:
        log.info("checkpoints.orphans_swept", threads=swept)
    return swept
```

Delete the old `prune`.

`app/workers/tasks.py` `_purge_checkpoints_once`:

```python
async def _purge_checkpoints_once() -> None:
    """Discard paused state nobody came back to, and threads nothing owns any more (S17)."""
    settings = get_settings()
    async with SessionFactory() as session:
        discarded = await checkpoints_svc.prune(
            session, older_than=timedelta(days=settings.checkpoint_retention_days)
        )
        swept = await checkpoints_svc.prune_orphans(session)
    if discarded or swept:
        logger.info("discarded %d idle and %d ownerless checkpoint thread(s)", discarded, swept)
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_checkpoint_lifecycle.py tests/test_durable_state.py tests/test_ingestion_jobs.py tests/test_ingestion_stages.py tests/test_ingest_slots.py tests/test_onboarding.py tests/test_onboarding_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/core/db.py app/services/ingestion.py app/services/onboarding_sessions.py app/services/checkpoints.py app/workers/tasks.py tests/test_checkpoint_lifecycle.py
git commit -m "feat(checkpoints): idle onboarding expires, orphan threads are swept, a running turn is never pruned [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 5: A real second process, and the races

**Files:**
- Modify: `app/services/workflow.py` (`DELETED_DETAIL`, `_gone`, the check after the stream in `run_workflow_turn`)
- Create: `tests/restart_probe.py`, `tests/test_restart.py`

**Interfaces:**
- Consumes: Task 3's `durable_checkpointer`, `has_checkpoint`; Task 3's `retention.delete_learner` erasing threads (cleanup).
- Produces: `workflow.DELETED_DETAIL: str`.

- [ ] **Step 1: Write the probe and the failing tests**

`tests/restart_probe.py`:

```python
"""A second process for ``tests/test_restart.py``: resume a practice another process paused.

``python -m tests.restart_probe <conversation_id> <learner_id>`` prints one JSON line: whether
the saver was durable, the paused item it found, and the event types of resuming it.
"""

import asyncio
import json
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.agent import checkpointing
from app.core.db import SessionFactory
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import workflow
from app.services.llm_log import set_accounting_session_factory

RIGHT_GRADE = '{"score": 0.9, "rationale": "much better"}'
RESPOND = "Great job, you've got it!"


class _Discarded:
    def add(self, obj: object) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def execute(self, *args: object, **kwargs: object) -> None:
        pass


@asynccontextmanager
async def _discard() -> AsyncIterator[Any]:
    yield _Discarded()


async def _main(conversation_id: uuid.UUID, learner_id: uuid.UUID) -> dict[str, Any]:
    set_accounting_session_factory(_discard)
    await checkpointing.start()
    try:
        if not checkpointing.is_durable():
            return {"durable": False}
        llm = fake_llm_client(script=[FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND)])
        async with SessionFactory() as session:
            conversation = await session.get(Conversation, conversation_id)
            assert conversation is not None
            item = await workflow.paused_item_id(
                llm, session, conversation_id, learner_id=learner_id
            )
            events = [
                e.type
                async for e in workflow.run_workflow_turn(
                    session,
                    llm,
                    learner_id=learner_id,
                    conversation=conversation,
                    user_content="sunlight -> sugars",
                    max_tokens=300,
                    max_rounds=3,
                    resume=True,
                )
            ]
        return {"durable": True, "paused_item_id": str(item) if item else None, "events": events}
    finally:
        await checkpointing.stop()


if __name__ == "__main__":
    report = asyncio.run(_main(uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2])))
    print(json.dumps(report))
```

`tests/test_restart.py`:

```python
"""Paused practice across a real restart, and the races durability made reachable (S17).

``test_durable_state`` resumes through a second saver in the same process; here the second
process is real. The races are the ones a durable, shared checkpoint opened: a second answer
while one is being graded, and a conversation deleted under a turn.
"""

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agent import checkpointing
from app.api.deps import get_llm_client
from app.core.config import get_settings
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.knowledge import Subject
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.services import removal, retention, turn_lock
from app.services import workflow as workflow_svc
from app.storage.memory import InMemoryBlobStore
from tests.checkpoint_helpers import has_checkpoint
from tests.test_workflow import (
    PRESENT,
    RESPOND_2,
    RIGHT_GRADE,
    _conversation_with_active_step,
    _drain,
    _learner_and_subject_with_active_step,
    _parse_sse,
)

API = "/api/v1"


async def test_a_second_process_resumes_what_this_one_paused(
    engine: AsyncEngine, durable_checkpointer: None
) -> None:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        conv = await _conversation_with_active_step(session)  # committed for real
        llm = fake_llm_client(script=[FakeTurn(text=PRESENT)])
        await _drain(session, llm, conv, user_content="let's practice")
        item_id = await workflow_svc.paused_item_id(
            llm, session, conv.id, learner_id=conv.learner_id
        )
        assert item_id is not None
        subject_id = conv.subject_id
    await checkpointing.stop()  # this process is gone
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tests.restart_probe",
            str(conv.id),
            str(conv.learner_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "GURU_DATABASE_URL": get_settings().database_url},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        assert proc.returncode == 0, err.decode()[-2000:]
        report = json.loads(out.decode().strip().splitlines()[-1])
        assert report["durable"] is True
        assert report["paused_item_id"] == str(item_id)
        assert "done" in report["events"]
    finally:
        await checkpointing.start(get_settings())
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await retention.delete_learner(session, InMemoryBlobStore(), conv.learner_id)
            await session.execute(delete(Subject).where(Subject.id == subject_id))
            await session.commit()


@pytest.fixture
def practice_llm() -> Iterator[None]:
    client = fake_llm_client(
        script=[
            FakeTurn(text=PRESENT),
            FakeTurn(text='{"intent": "attempt"}'),
            FakeTurn(text=RIGHT_GRADE),
            FakeTurn(text=RESPOND_2),
        ]
    )
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def test_a_second_answer_while_one_is_being_graded_is_refused(
    api_client: AsyncClient,
    db_session: AsyncSession,
    engine: AsyncEngine,
    api_learner: Learner,
    practice_llm: None,
) -> None:
    """Pins the guarantee S17's durability depends on: one paused question, one grade."""
    _learner, subject = await _learner_and_subject_with_active_step(db_session, learner=api_learner)
    r = await api_client.post(f"{API}/conversations", json={"subject_id": str(subject.id)})
    conversation_id = r.json()["id"]
    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "let's practice", "mode": "workflow"},
    )
    assert any(e["type"] == "awaiting_reply" for e in _parse_sse(r.text))

    in_flight = await turn_lock.claim(engine, uuid.UUID(conversation_id))
    assert in_flight is not None
    try:
        r = await api_client.post(
            f"{API}/conversations/{conversation_id}/messages",
            json={"content": "sunlight -> sugars"},
        )
        assert r.status_code == 409
        graded = await db_session.scalar(
            select(func.count())
            .select_from(LearningEvent)
            .where(LearningEvent.learner_id == api_learner.id)
        )
        assert graded == 0
    finally:
        await in_flight.release()

    r = await api_client.post(
        f"{API}/conversations/{conversation_id}/messages",
        json={"content": "sunlight -> sugars"},
    )
    assert any(e["type"] == "done" for e in _parse_sse(r.text))


async def test_deleting_a_conversation_mid_answer_ends_the_turn_cleanly(
    db_session: AsyncSession, durable_checkpointer: None
) -> None:
    conv = await _conversation_with_active_step(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text=PRESENT), FakeTurn(text=RIGHT_GRADE), FakeTurn(text=RESPOND_2)]
    )
    await _drain(db_session, llm, conv, user_content="let's practice")

    stream = workflow_svc.run_workflow_turn(
        db_session,
        llm,
        learner_id=conv.learner_id,
        conversation=conv,
        user_content="sunlight -> sugars",
        max_tokens=300,
        max_rounds=3,
        resume=True,
    )
    events = [await anext(stream)]  # graded, and the feedback has started streaming
    await removal.delete_conversation(db_session, conv.learner_id, conv.id, forget=False)
    events += [e async for e in stream]

    assert events[-1].type == "error"
    assert events[-1].detail == workflow_svc.DELETED_DETAIL
    assert not await has_checkpoint(str(conv.id))
```

- [ ] **Step 2: Run to verify**

Run: `uv run pytest tests/test_restart.py -q`
Expected: `test_a_second_process_resumes_what_this_one_paused` and `test_a_second_answer_while_one_is_being_graded_is_refused` PASS (they pin existing behaviour: the durable saver and the turn lock); `test_deleting_a_conversation_mid_answer_ends_the_turn_cleanly` FAILS — an `IntegrityError` (the assistant message's conversation is gone) or `AttributeError: ... has no attribute 'DELETED_DETAIL'`.

- [ ] **Step 3: Implement**

`app/services/workflow.py`: add `from sqlalchemy import select`; below `CHECK_FIRST_SYSTEM_PROMPT`:

```python
DELETED_DETAIL = "this conversation was deleted"


async def _gone(session: AsyncSession, conversation_id: uuid.UUID) -> bool:
    """Whether the conversation was deleted while this turn ran (asked of the database, not
    of the identity map, which still holds the object)."""
    found = await session.scalar(select(Conversation.id).where(Conversation.id == conversation_id))
    return found is None
```

In `run_workflow_turn`, immediately after the `try: … except Exception …` block around `graph.astream`, before `snapshot = await graph.aget_state(config)`:

```python
    if await _gone(session, conversation.id):
        # Deleted under the turn (S17): nothing to write to, and the graph has just written a
        # checkpoint the delete already erased once — erase it again so nothing outlives it.
        await checkpointing.discard_thread(str(conversation.id))
        yield TurnEvent(type="error", detail=DELETED_DETAIL)
        return
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_restart.py tests/test_workflow.py tests/test_practice_pause.py -q`
Expected: PASS. If the grade itself fails on the deleted conversation (an exception inside `astream`), the turn ends at the existing `generation failed` error instead: record a ruling, move the `_gone` check into the `except Exception` branch too (before its `yield`), and assert `events[-1].type == "error"` with either detail.

- [ ] **Step 5: Commit**

```bash
git add app/services/workflow.py tests/restart_probe.py tests/test_restart.py
git commit -m "test(checkpoints): a real second process resumes paused practice; a delete mid-answer ends cleanly [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```

---

### Task 6: Gates and docs

**Files:**
- Modify: `docs/RUNBOOK.md` (§3, §16)
- Modify: `CLAUDE.md` (Key Technical Decisions)
- Modify: `docs/guru-suggestions-tracker.md` (S17 row → Completed; counts; next up)

- [ ] **Step 1: Full gates**

Run (background): `uv run poe check > .superpowers/sdd/2026-10-08-durable-practice-lifecycle/check.log 2>&1`; then `tail -5` of that log.
Expected: all passed (≈2720), 10 skipped; ruff and ty clean.

Run: `uv run poe format-check`
Expected: all files already formatted.

- [ ] **Step 2: RUNBOOK**

§3, after the `db-upgrade` code block's paragraph about `db-check`, add:

```markdown
`db-upgrade` also creates and upgrades LangGraph's checkpoint tables
(`python -m app.agent.checkpointing migrate`, under an advisory lock). Processes no longer run
that DDL: a process whose checkpoint schema is missing or behind serves chat on volatile paused
state and logs `checkpointer.schema_behind` (and `/ready` shows `durable_checkpoints: false`).
**A LangGraph upgrade may need `poe db-upgrade` before it is deployed.**
```

§16, at the end, add:

```markdown
### Paused practice and negotiations (S17)

Checkpoint threads are erased with their conversation, onboarding session or account; an erase
the checkpointer refuses (volatile process, database error) becomes a `pending_erasures` row of
kind `checkpoint`, retried by the worker, which always opens its own checkpointer. The worker's
purge also expires onboarding sessions idle for `checkpoint_retention_days` and sweeps threads
whose owner no longer exists (`checkpoints.orphans_swept`). It never prunes a conversation with
a turn running. After a deploy that changes a graph, paused state from the old shape is dropped
on first resume (`checkpointer.incompatible_dropped`); bump `WORKFLOW_GRAPH_VERSION` /
`REFINEMENT_GRAPH_VERSION` when `tests/test_graph_versions.py` says so.
```

- [ ] **Step 3: CLAUDE.md**

Append to the "Deleting an account is a state, then an erase" bullet, before "See":
`Paused practice state is erased with its conversation, onboarding session or account, and a deploy that changes a graph drops (never resumes) the old shape's paused state (S17).`

- [ ] **Step 4: Tracker**

Move S17 to Completed (Live v0 16 → 15, Done 49 → 50); Next up: "S62, then S53". Note deferred minors from the final review there.

- [ ] **Step 5: Commit**

```bash
git add docs/RUNBOOK.md CLAUDE.md docs/guru-suggestions-tracker.md
git commit -m "docs: record the durable practice lifecycle [S17]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status
```
