"""Upload API: store a source + queue ingestion; poll status; inspect chunks."""

import uuid
from collections.abc import Iterator

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_blob_store, get_ingestion_enqueuer
from app.core.config import Settings
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.source import Source
from app.services import ingestion
from app.storage import InMemoryBlobStore

API = "/api/v1"


@pytest.fixture
def fake_ingest() -> Iterator[tuple[InMemoryBlobStore, list[uuid.UUID]]]:
    """Override the object store (in-memory) and capture enqueued ingestion jobs."""
    store = InMemoryBlobStore()
    enqueued: list[uuid.UUID] = []

    async def _enqueue(source_id: uuid.UUID) -> None:
        enqueued.append(source_id)

    app.dependency_overrides[get_blob_store] = lambda: store
    app.dependency_overrides[get_ingestion_enqueuer] = lambda: _enqueue
    yield store, enqueued
    app.dependency_overrides.pop(get_blob_store, None)
    app.dependency_overrides.pop(get_ingestion_enqueuer, None)


async def _upload(client: AsyncClient, content: bytes, name: str = "notes.txt") -> Response:
    return await client.post(f"{API}/sources/upload", files={"file": (name, content, "text/plain")})


async def test_upload_creates_pending_source_and_enqueues(
    api_client: AsyncClient, db_session: AsyncSession, fake_ingest
) -> None:
    store, enqueued = fake_ingest
    r = await _upload(api_client, b"hello teaching world")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["kind"] == "file"
    assert body["origin"] == "notes.txt"
    assert "blob_key" not in body  # internal detail not exposed

    source_id = uuid.UUID(body["id"])
    assert enqueued == [source_id]
    source = await db_session.get(Source, source_id)
    assert source is not None and source.blob_key is not None
    assert await store.get(source.blob_key) == b"hello teaching world"


async def test_upload_empty_file_400(api_client: AsyncClient, fake_ingest) -> None:
    r = await _upload(api_client, b"")
    assert r.status_code == 400


async def test_upload_rejects_oversize_file(api_client: AsyncClient, fake_ingest) -> None:
    app.dependency_overrides[get_app_settings] = lambda: Settings(max_upload_bytes=8)
    try:
        r = await _upload(api_client, b"way more than eight bytes")
        assert r.status_code == 413, r.text
    finally:
        app.dependency_overrides.pop(get_app_settings, None)


async def test_get_source_status(api_client: AsyncClient, fake_ingest) -> None:
    body = (await _upload(api_client, b"content")).json()
    r = await api_client.get(f"{API}/sources/{body['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "pending"


async def test_get_missing_source_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/sources/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_get_chunks_after_ingest(
    api_client: AsyncClient, db_session: AsyncSession, fake_ingest
) -> None:
    store, _ = fake_ingest
    body = (await _upload(api_client, b"Mitochondria produce ATP for the cell.")).json()
    source_id = uuid.UUID(body["id"])

    # Run ingestion directly with the same in-memory store + a fake embedder.
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source_id)

    r = await api_client.get(f"{API}/sources/{source_id}/chunks")
    assert r.status_code == 200
    chunks = r.json()
    assert len(chunks) >= 1
    assert "embedding" not in chunks[0]  # raw vector withheld
    assert chunks[0]["provenance"]["method"] == "text"
    assert chunks[0]["text"]
