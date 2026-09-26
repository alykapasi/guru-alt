"""v0 accepts uploaded files, never new web content, even through old queued jobs."""

import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import build_tools
from app.api.deps import get_ingestion_enqueuer
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag import retrieval, textnorm
from app.rag.scope import SourceScope
from app.services import ingestion
from app.storage import InMemoryBlobStore
from tests.embedding import FAKE_SPACE

URL = "https://example.com/notes"


async def test_link_api_refuses_without_storing_or_queueing(
    api_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    enqueue = AsyncMock()
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: enqueue
    before = await db_session.scalar(select(func.count()).select_from(Source))
    response = await api_client.post("/api/v1/sources/link", json={"url": URL})
    assert response.status_code == 403
    assert "v0" in response.json()["detail"]
    enqueue.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(Source)) == before


@pytest.mark.parametrize("creator", ["url", "bytes", "deduplicated"])
async def test_service_cannot_create_url_sources(
    creator: str,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    store = InMemoryBlobStore()
    with pytest.raises(ingestion.WebIngestionDisabled, match=r"URL ingestion.*disabled.*v0"):
        if creator == "url":
            await ingestion.create_url_source(db_session, learner_id=api_learner.id, url=URL)
        else:
            create = (
                ingestion.create_source if creator == "bytes" else ingestion.create_or_reuse_source
            )
            await create(
                db_session,
                store,
                learner_id=api_learner.id,
                kind=SourceKind.URL,
                origin=URL,
                content_type="text/plain",
                data=b"notes",
                content_sha256=ingestion.digest_of(b"notes"),
            )
    assert not store._store
    assert not (await db_session.scalars(select(Source))).all()


@pytest.mark.parametrize("has_blob", [False, True])
async def test_old_queued_url_jobs_fail_terminally_without_fetch_or_extraction(
    has_blob: bool,
    db_session: AsyncSession,
    api_learner: Learner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryBlobStore()
    blob_key = "legacy-page" if has_blob else None
    if blob_key:
        await store.put(blob_key, b"old page", content_type="text/plain")
    source = Source(
        learner_id=api_learner.id,
        kind=SourceKind.URL,
        origin=URL,
        blob_key=blob_key,
        content_type="text/plain",
        status=SourceStatus.PENDING,
    )
    db_session.add(source)
    await db_session.flush()
    chunk = Chunk(
        source_id=source.id,
        ordinal=0,
        text="keep old content",
        provenance={},
        embedding_space=FAKE_SPACE,
        embedding=(await fake_llm_client().embed(ModelRole.EMBED, ["keep old content"])).vectors[0],
    )
    db_session.add(chunk)
    await db_session.commit()
    source_id, chunk_id = source.id, chunk.id
    fetch = AsyncMock(return_value=(b"new page", "text/plain"))
    monkeypatch.setattr("app.rag.fetch.default_fetch", fetch)
    pipeline = AsyncMock(return_value=1)
    monkeypatch.setattr("app.rag.pipeline.run", pipeline)

    result = await ingestion.ingest_source(
        db_session,
        store,
        fake_llm_client(),
        source_id,
    )
    assert result is not None
    assert result.status == SourceStatus.FAILED
    assert "disabled" in (result.error or "")
    assert result.lease_expires_at is None
    fetch.assert_not_awaited()
    pipeline.assert_not_awaited()
    kept = await db_session.get(Chunk, chunk_id)
    assert kept is not None and kept.text == "keep old content"
    assert result.blob_key == blob_key
    if blob_key:
        assert store._store[blob_key] == b"old page"
    # Duplicate task delivery cannot restart the disabled job.
    assert (
        await ingestion.ingest_source(
            db_session,
            store,
            fake_llm_client(),
            source_id,
        )
        is None
    )


@pytest.mark.parametrize("status", [SourceStatus.DONE, SourceStatus.FAILED])
async def test_url_retry_is_refused_but_existing_source_stays_readable(
    status: SourceStatus,
    api_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    source = Source(learner_id=api_learner.id, kind=SourceKind.URL, origin=URL, status=status)
    db_session.add(source)
    await db_session.commit()
    source_id = source.id
    enqueue = AsyncMock()
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: enqueue
    response = await api_client.post(f"/api/v1/sources/{source_id}/retry")
    assert response.status_code == 403
    enqueue.assert_not_awaited()
    with pytest.raises(ingestion.WebIngestionDisabled, match=r"URL ingestion.*disabled.*v0"):
        await ingestion.reset_for_reingest(db_session, source_id)
    await db_session.refresh(source)
    assert source.status == status
    assert (await api_client.get(f"/api/v1/sources/{source_id}")).status_code == 200


def test_tutor_only_exposes_internal_material_search() -> None:
    tools = build_tools(
        AsyncMock(spec=AsyncSession), fake_llm_client(), scope=SourceScope(learner_id=uuid.uuid4())
    )
    assert [tool.name for tool in tools] == ["search_materials"]


@pytest.mark.parametrize("status", [SourceStatus.DONE, SourceStatus.FAILED])
async def test_uploading_a_file_does_not_reuse_a_legacy_url(
    status: SourceStatus,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    data = b"previously imported page"
    digest = ingestion.digest_of(data)
    legacy = Source(
        learner_id=api_learner.id,
        kind=SourceKind.URL,
        origin=URL,
        status=status,
        content_sha256=digest,
        text_sha256=textnorm.fingerprint(data.decode()),
    )
    db_session.add(legacy)
    await db_session.commit()
    store = InMemoryBlobStore()
    source, queue = await ingestion.create_or_reuse_source(
        db_session,
        store,
        learner_id=api_learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=data,
        content_sha256=digest,
    )
    assert queue
    assert source.kind == SourceKind.FILE
    assert source.id != legacy.id
    await db_session.refresh(legacy)
    assert legacy.status == status
    result = await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
    assert result is not None and result.status == SourceStatus.DONE
    hits = await retrieval.retrieve(
        db_session,
        fake_llm_client(),
        "imported page",
        scope=SourceScope(learner_id=api_learner.id, source_ids=(source.id,)),
    )
    assert hits and hits[0].source_id == source.id
