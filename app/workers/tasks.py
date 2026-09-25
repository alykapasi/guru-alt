"""taskiq task wrappers — thin shells around services.

Each task opens its **own** DB session (workers run outside the request lifecycle) and
builds the blob store + LLM client from settings, then delegates to a service. Run a worker
with ``uv run poe worker``.
"""

import asyncio
import contextlib
import logging
import uuid
from datetime import timedelta

from taskiq import TaskiqEvents, TaskiqState

from app.agent import checkpointing
from app.core.alerts import evaluate
from app.core.config import get_settings
from app.core.db import SessionFactory
from app.core.readiness import readiness
from app.llm import build_llm_client
from app.models.source import Source
from app.rag import pipeline
from app.rag.demux import build_demuxer
from app.rag.transcription import build_transcriber
from app.services import alert_history, ingestion
from app.services import auth as auth_svc
from app.services import checkpoints as checkpoints_svc
from app.services import concept_links as concept_links_svc
from app.services import memory as memory_svc
from app.services.ingestion import backlog
from app.services.spend import window as spend_window
from app.storage import build_blob_store
from app.workers.broker import broker

logger = logging.getLogger(__name__)


async def _ingest_source_task(source_id: str) -> None:
    """Ingest one source by id (enqueued after upload)."""
    settings = get_settings()
    blobstore = build_blob_store(settings)
    llm = build_llm_client(settings)
    transcriber = build_transcriber(settings)  # model loads lazily; cheap to construct
    demuxer = build_demuxer(settings)  # ffmpeg is only invoked when video is ingested
    async with SessionFactory() as session:
        await ingestion.ingest_source(
            session,
            blobstore,
            llm,
            uuid.UUID(source_id),
            transcriber=transcriber,
            demuxer=demuxer,
        )


async def _memory_write_back_task(conversation_id: str) -> None:
    """Extract and persist durable memories from one conversation (enqueued on-demand)."""
    settings = get_settings()
    llm = build_llm_client(settings)
    async with SessionFactory() as session:
        await memory_svc.write_back(session, llm, conversation_id=uuid.UUID(conversation_id))


async def _judge_concept_links_task(learner_id: str) -> None:
    """Judge a learner's new concept-link candidates (enqueued when they commit a subject)."""
    llm = build_llm_client(get_settings())
    async with SessionFactory() as session:
        await concept_links_svc.judge_pending(session, llm, uuid.UUID(learner_id))


async def _enqueue_ingestion(source_id: uuid.UUID) -> None:
    """Enqueue by id, discarding the task handle — reconciliation only needs it dispatched."""
    await ingest_source_task.kiq(str(source_id))


async def _retag_source_task(source_id: str) -> None:
    """Rebuild one source's KC tags after its subject changed (enqueued by S55's reassign)."""
    settings = get_settings()
    llm = build_llm_client(settings)
    async with SessionFactory() as session:
        source = await session.get(Source, uuid.UUID(source_id))
        if source is None:
            return
        await pipeline.retag_source(session, llm, source, settings=settings)


async def _reconcile_once() -> None:
    """One reconciliation sweep, re-enqueueing through this module's own task."""
    settings = get_settings()
    async with SessionFactory() as session:
        await ingestion.reconcile_stranded(session, _enqueue_ingestion, settings=settings)


async def _reconcile_loop(interval: int) -> None:
    """Sweep for stranded sources forever.

    Every iteration is wrapped, because the one failure this loop must survive is the very
    outage it exists to recover from: if a queue or database blip killed the sweep, the
    recovery mechanism would die exactly when it is needed and stay dead until a restart.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await _reconcile_once()
        except Exception:
            logger.exception("ingestion reconciliation sweep failed; will retry")


async def _purge_sessions_once() -> None:
    """Delete sessions that can no longer authenticate anybody (S21)."""
    settings = get_settings()
    async with SessionFactory() as session:
        removed = await auth_svc.purge_expired(
            session,
            keep_revoked_for=timedelta(hours=settings.session_revoked_retention_hours),
        )
    if removed:
        logger.info("purged %d dead session(s)", removed)


async def _purge_checkpoints_once() -> None:
    """Discard paused graph state for conversations nobody has come back to (S17)."""
    settings = get_settings()
    async with SessionFactory() as session:
        discarded = await checkpoints_svc.prune(
            session, older_than=timedelta(days=settings.checkpoint_retention_days)
        )
    if discarded:
        logger.info("discarded %d abandoned checkpoint thread(s)", discarded)


async def _alerts_once() -> None:
    """Evaluate every alert condition and record what changed (P11).

    The predicate existed and nothing ran it, so a condition could fire and resolve between two
    glances at a dashboard nobody was looking at. This is what runs it.

    Transitions are logged as well as stored, at the severity they carry, because the log is
    the delivery channel this codebase already has: anything shipping logs can alert on a line,
    and choosing a pager or a webhook is a deployment decision that should not have to be made
    before the conditions are watched at all. One line per *change*, never per poll — delivering
    the firing set every minute would page somebody sixty times for one incident.
    """
    settings = get_settings()
    store = build_blob_store(settings)
    async with SessionFactory() as session:
        report = evaluate(
            readiness=await readiness(session, store),
            backlog=await backlog(session, settings=settings),
            spend=await spend_window(session, settings=settings),
            settings=settings,
        )
        changed = await alert_history.record(session, report)
    for row in changed:
        if row.firing:
            log = logger.error if row.severity == "critical" else logger.warning
            log("alert firing: %s — %s | action: %s", row.name, row.detail, row.action)
        else:
            logger.info("alert resolved: %s", row.name)


async def _alerts_loop(interval: int) -> None:
    """Watch the alert conditions forever, surviving its own failures like the sweeps above.

    A failure here is itself a monitoring outage, so it is logged at exception level and the
    loop continues: a watcher that dies on one bad evaluation is worse than no watcher, because
    the absence of alerts still reads as "nothing wrong".
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await _alerts_once()
        except Exception:
            logger.exception("alert evaluation failed; will retry")


async def _purge_checkpoints_loop(interval: int) -> None:
    """Sweep abandoned checkpoints forever, surviving its own failures like the others."""
    while True:
        await asyncio.sleep(interval)
        try:
            await _purge_checkpoints_once()
        except Exception:
            logger.exception("checkpoint purge sweep failed; will retry")


async def _purge_sessions_loop(interval: int) -> None:
    """Sweep dead sessions forever, surviving its own failures like the reconciler above."""
    while True:
        await asyncio.sleep(interval)
        try:
            await _purge_sessions_once()
        except Exception:
            logger.exception("session purge sweep failed; will retry")


async def _start_reconciler(state: TaskiqState) -> None:
    interval = get_settings().ingest_reconcile_interval_seconds
    if interval <= 0:
        return
    state.reconciler = asyncio.create_task(_reconcile_loop(interval))


async def _stop_reconciler(state: TaskiqState) -> None:
    await _cancel(getattr(state, "reconciler", None))


async def _start_alerts(state: TaskiqState) -> None:
    interval = get_settings().alert_poll_interval_seconds
    if interval <= 0:
        return
    state.alerts = asyncio.create_task(_alerts_loop(interval))


async def _stop_alerts(state: TaskiqState) -> None:
    await _cancel(getattr(state, "alerts", None))


async def _start_session_purge(state: TaskiqState) -> None:
    interval = get_settings().session_purge_interval_seconds
    if interval <= 0:
        return
    state.session_purge = asyncio.create_task(_purge_sessions_loop(interval))


async def _stop_session_purge(state: TaskiqState) -> None:
    await _cancel(getattr(state, "session_purge", None))


async def _start_checkpoint_purge(state: TaskiqState) -> None:
    interval = get_settings().checkpoint_purge_interval_seconds
    if interval <= 0:
        return
    # The worker owns its own checkpointer pool: pruning goes through the saver rather than
    # through SQL (see ``app.agent.checkpointing.discard_thread``), and without this the
    # sweep would run against the volatile fallback and silently discard nothing.
    await checkpointing.start()
    state.checkpoint_purge = asyncio.create_task(_purge_checkpoints_loop(interval))


async def _stop_checkpoint_purge(state: TaskiqState) -> None:
    await _cancel(getattr(state, "checkpoint_purge", None))
    await checkpointing.stop()


async def _cancel(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# Applied as a plain call, not `@broker.task` decorator syntax: beartype's package-wide import
# hook (``beartype_this_package`` in ``app/__init__.py``) rewrites every function *definition*
# it sees, and when `@broker.task` sits directly in that same decorator stack, beartype ends up
# decorating the resulting ``AsyncTaskiqDecoratedTask`` *instance* (a plain callable object, not
# a function) — since it can only meaningfully wrap `.__call__`, the module-level name gets
# rebound to a plain function proxying that call, silently losing `.kiq()` and every other task
# method. Defining the coroutine normally (beartype checks it like any other function) and
# calling `broker.task(...)` afterward as an ordinary assignment sidesteps this entirely, since
# beartype's claw hook only rewrites `def`/`async def` nodes, never plain assignment statements.
ingest_source_task = broker.task(_ingest_source_task)
retag_source_task = broker.task(_retag_source_task)
memory_write_back_task = broker.task(_memory_write_back_task)
judge_concept_links_task = broker.task(_judge_concept_links_task)


# Same reasoning as above for the event handlers: registered by plain call rather than the
# `@broker.on_event` decorator, so beartype's claw hook never sees them in a decorator stack.
broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, _start_reconciler)
broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, _stop_reconciler)
broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, _start_session_purge)
broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, _stop_session_purge)
broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, _start_checkpoint_purge)
broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, _stop_checkpoint_purge)
broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, _start_alerts)
broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, _stop_alerts)
