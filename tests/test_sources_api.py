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
from app.models.knowledge import Subject
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


# --- list + single-chunk fetch (Phase 7: conversation scope + citations) ----


async def test_list_sources(api_client: AsyncClient, fake_ingest) -> None:
    a = (await _upload(api_client, b"content a", name="a.txt")).json()
    b = (await _upload(api_client, b"content b", name="b.txt")).json()

    r = await api_client.get(f"{API}/sources")
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()}
    assert {a["id"], b["id"]} <= ids


async def test_list_sources_filtered_by_subject(
    api_client: AsyncClient, db_session: AsyncSession, fake_ingest
) -> None:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="Physics")
    db_session.add(subject)
    await db_session.commit()

    r = await api_client.post(
        f"{API}/sources/upload",
        data={"subject_id": str(subject.id)},
        files={"file": ("scoped.txt", b"scoped content", "text/plain")},
    )
    assert r.status_code == 202, r.text
    scoped = r.json()
    unscoped = (await _upload(api_client, b"other content", name="other.txt")).json()

    r = await api_client.get(f"{API}/sources", params={"subject_id": str(subject.id)})
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()}
    assert scoped["id"] in ids
    assert unscoped["id"] not in ids


async def test_get_chunk_by_id(
    api_client: AsyncClient, db_session: AsyncSession, fake_ingest
) -> None:
    store, _ = fake_ingest
    body = (await _upload(api_client, b"Mitochondria produce ATP for the cell.")).json()
    source_id = uuid.UUID(body["id"])
    await ingestion.ingest_source(db_session, store, fake_llm_client(), source_id)

    chunks = (await api_client.get(f"{API}/sources/{source_id}/chunks")).json()
    chunk_id = chunks[0]["id"]

    r = await api_client.get(f"{API}/chunks/{chunk_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == chunk_id
    assert r.json()["text"]


async def test_get_missing_chunk_404(api_client: AsyncClient) -> None:
    r = await api_client.get(f"{API}/chunks/{uuid.uuid4()}")
    assert r.status_code == 404
