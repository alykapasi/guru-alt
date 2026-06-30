"""Shared FastAPI dependencies.

``get_current_learner`` is the **stub auth seam**: it resolves (and lazily creates) a
single dev learner. Real auth (Phase 8) swaps only this function — every route already
receives a ``Learner`` / ``learner_id``.
"""

import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_session
from app.llm import LLMClient, build_llm_client
from app.models.learner import Learner
from app.storage import BlobStore, build_blob_store

DEV_LEARNER_HANDLE = "dev"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@lru_cache
def _llm_client() -> LLMClient:
    return build_llm_client(get_settings())


def get_llm_client() -> LLMClient:
    """The process-wide LLM registry. Overridden in tests with a FakeProvider client."""
    return _llm_client()


LLMClientDep = Annotated[LLMClient, Depends(get_llm_client)]


@lru_cache
def _blob_store() -> BlobStore:
    return build_blob_store(get_settings())


def get_blob_store() -> BlobStore:
    """The process-wide object store. Overridden in tests with an in-memory store."""
    return _blob_store()


BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]

IngestionEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def _enqueue_ingestion(source_id: uuid.UUID) -> None:
    from app.workers.tasks import ingest_source_task  # lazy: avoids an import cycle

    await ingest_source_task.kiq(str(source_id))


def get_ingestion_enqueuer() -> IngestionEnqueuer:
    """Returns the callable that queues an ingestion job. Overridden in tests."""
    return _enqueue_ingestion


IngestionEnqueuerDep = Annotated[IngestionEnqueuer, Depends(get_ingestion_enqueuer)]


async def get_current_learner(session: SessionDep) -> Learner:
    """Resolve the current learner (stub: the dev learner, created on first use)."""
    learner = await session.scalar(select(Learner).where(Learner.handle == DEV_LEARNER_HANDLE))
    if learner is None:
        learner = Learner(handle=DEV_LEARNER_HANDLE, display_name="Dev Learner")
        session.add(learner)
        await session.commit()
        await session.refresh(learner)
    return learner


CurrentLearner = Annotated[Learner, Depends(get_current_learner)]
