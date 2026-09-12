"""Where a paused graph's state actually lives (S17).

Two graphs pause mid-conversation and wait for the learner: the goal-refinement gate, and the
guided-practice loop. Both compiled with LangGraph's ``InMemorySaver``, so the state between
the interrupt and its resume lived in one process's heap. A restart — a deploy, a crash, an
autoscaler moving the pod — silently discarded every paused conversation in flight. The learner
saw a practice question they could no longer answer, or a proposed goal they could no longer
accept, with nothing saying why; the gate's dispatcher degraded that to "start plain chat
instead", which is a reasonable thing to do with state that is genuinely gone and a terrible
thing to need.

That constraint reached further than the two graphs. ``turn_lock`` claimed a conversation
in-process, and ``onboarding_sessions`` recorded who owned a negotiation in a dict, each
documented as *deliberately* exactly as strong as the checkpointer behind it — a durable claim
in front of volatile state promises more than the state can keep. Making the checkpointer
durable removes that ceiling and, with it, the excuse: both move in this change too, because a
durable checkpointer with an in-process lock is strictly worse than what came before. Two
processes could then resume the *same* paused practice, grade one answer twice, and write two
mastery observations for one piece of work.

**Why a second connection pool.** The checkpointer is LangGraph's own schema, migrated by its
own ``setup()``, and it speaks psycopg3 while the application speaks asyncpg through
SQLAlchemy. Hand-writing its tables into Alembic would fork a schema the library owns and
upgrades; pointing it at our engine is not possible across drivers. So it gets its own small
pool against the same database, opened once and closed with the app.

**Degradation is loud, not silent.** If the pool cannot open, the graphs fall back to
``InMemorySaver`` rather than the process refusing to serve chat at all — but ``is_durable()``
says so, ``/ready`` reports it, and the failure is logged at error level. The old behaviour is
survivable; the old behaviour arriving unannounced is not.
"""

import asyncio

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import Settings, get_settings

log = structlog.get_logger(__name__)

_VOLATILE = InMemorySaver()
"""The fallback, and the value every caller gets before :func:`start` has run."""

_saver: BaseCheckpointSaver | None = None
_pool: object | None = None
_lock = asyncio.Lock()


def psycopg_dsn(database_url: str) -> str:
    """The application's SQLAlchemy URL as a plain libpq DSN.

    SQLAlchemy names the driver in the scheme (``postgresql+asyncpg://``); psycopg wants the
    bare ``postgresql://``. Only the ``+driver`` suffix is removed — host, credentials, port
    and database stay exactly as configured, so the checkpointer cannot end up pointed at a
    different database than the one the rest of the app is using.
    """
    scheme, separator, rest = database_url.partition("://")
    if not separator:
        return database_url
    return f"{scheme.partition('+')[0]}://{rest}"


async def start(settings: Settings | None = None) -> None:
    """Open the checkpointer's pool and migrate its schema. Idempotent."""
    global _saver, _pool
    settings = settings or get_settings()
    async with _lock:
        if _saver is not None:
            return
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

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
            saver = AsyncPostgresSaver(pool)  # ty: ignore[invalid-argument-type]
            await saver.setup()
        except Exception as exc:
            # Chat still works; paused conversations still will not survive a restart. Both
            # halves of that are true and both are reported rather than one being assumed.
            log.error("checkpointer.durable_start_failed", error=str(exc))
            _saver, _pool = _VOLATILE, None
            return
        _saver, _pool = saver, pool
        log.info("checkpointer.durable", min_size=pool.min_size, max_size=pool.max_size)


async def stop() -> None:
    """Close the pool. Safe to call when :func:`start` never ran or fell back."""
    global _saver, _pool
    async with _lock:
        pool = _pool
        _saver, _pool = None, None
    if pool is not None:
        await pool.close()  # ty: ignore[unresolved-attribute]


def checkpointer() -> BaseCheckpointSaver:
    """The saver the graphs compile against.

    Returns the volatile saver until :func:`start` has succeeded, so importing and compiling a
    graph never depends on a database being reachable — the tests that exercise graph shape
    rather than durability need no pool at all.
    """
    return _saver or _VOLATILE


def is_durable() -> bool:
    """Whether a paused conversation would survive a restart of this process."""
    return _saver is not None and _saver is not _VOLATILE
