"""taskiq task wrappers — thin shells around services.

Each task opens its **own** DB session (workers run outside the request lifecycle) and
builds the blob store + LLM client from settings, then delegates to a service. Run a worker
with ``uv run poe worker``.
"""

import uuid

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.llm import build_llm_client
from app.rag.demux import build_demuxer
from app.rag.transcription import build_transcriber
from app.services import ingestion
from app.services import memory as memory_svc
from app.storage import build_blob_store
from app.workers.broker import broker


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
memory_write_back_task = broker.task(_memory_write_back_task)
