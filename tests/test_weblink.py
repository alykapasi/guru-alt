"""Weblink ingestion: robots logic, HTML extraction, URL-ingest job, and the link API."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ingestion_enqueuer
from app.llm.registry import fake_llm_client
from app.main import app
from app.models.learner import Learner
from app.models.source import Chunk, SourceStatus
from app.rag.adapters import ExtractContext
from app.rag.adapters.html import HtmlAdapter
from app.rag.fetch import RobotsDisallowed, robots_allows
from app.services import ingestion
from app.storage import InMemoryBlobStore

API = "/api/v1"
URL = "https://example.com/cell"

_HTML = b"""<html><head><title>Cells</title></head><body>
<nav>Home About Contact</nav>
<article>
<h1>The Cell</h1>
<p>The cell is the basic structural and functional unit of all living organisms. Cells
are often called the building blocks of life and were first described in the 1600s.</p>
<p>The mitochondrion is the powerhouse of the cell, generating most of the cell's supply
of ATP through the process of cellular respiration.</p>
</article>
<footer>Copyright 2026 Example Corp</footer>
</body></html>"""


async def _fake_fetch(url: str) -> tuple[bytes, str]:
    return _HTML, "text/html"


async def _blocked_fetch(url: str) -> tuple[bytes, str]:
    raise RobotsDisallowed(f"robots.txt disallows {url}")


@pytest.fixture
def capture_enqueue() -> Iterator[list[uuid.UUID]]:
    enqueued: list[uuid.UUID] = []

    async def _enqueue(source_id: uuid.UUID) -> None:
        enqueued.append(source_id)

    app.dependency_overrides[get_ingestion_enqueuer] = lambda: _enqueue
    yield enqueued
    app.dependency_overrides.pop(get_ingestion_enqueuer, None)


# --- robots (pure) ----------------------------------------------------------


def test_robots_allows_permits_unlisted_path() -> None:
    txt = "User-agent: *\nDisallow: /private/"
    assert robots_allows(txt, "GuruBot", "https://x.com/public/page")


def test_robots_allows_blocks_disallowed_path() -> None:
    assert not robots_allows("User-agent: *\nDisallow: /", "GuruBot", "https://x.com/anything")


# --- HTML extraction --------------------------------------------------------


async def test_html_adapter_extracts_main_content(tmp_path: Path) -> None:
    page = tmp_path / "page.html"
    page.write_bytes(_HTML)
    units = await HtmlAdapter().extract(page, meta={"url": URL}, ctx=ExtractContext())
    assert len(units) == 1
    assert units[0].locator == {"url": URL}
    assert "mitochondrion" in units[0].text.lower()
    assert "About Contact" not in units[0].text  # nav stripped


async def test_html_adapter_empty_on_no_content(tmp_path: Path) -> None:
    empty = tmp_path / "empty.html"
    empty.write_bytes(b"<html><body></body></html>")
    assert await HtmlAdapter().extract(empty, meta={}, ctx=ExtractContext()) == []


# --- URL ingestion job ------------------------------------------------------


async def test_url_ingest_fetches_extracts_and_traces(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    source = await ingestion.create_url_source(db_session, learner_id=learner.id, url=URL)
    result = await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, fetch=_fake_fetch
    )
    assert result.status == SourceStatus.DONE
    assert result.content_type == "text/html"
    assert result.blob_key is not None  # fetched page was stored

    chunks = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert len(chunks) >= 1
    assert chunks[0].provenance["method"] == "html"
    assert chunks[0].provenance["url"] == URL
    assert "mitochondrion" in " ".join(c.text for c in chunks).lower()


async def test_url_ingest_robots_blocked_fails(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    source = await ingestion.create_url_source(db_session, learner_id=learner.id, url=URL)
    result = await ingestion.ingest_source(
        db_session, store, fake_llm_client(), source.id, fetch=_blocked_fetch
    )
    assert result.status == SourceStatus.FAILED
    assert "robots" in (result.error or "").lower()
    assert result.blob_key is None


# --- link API ---------------------------------------------------------------


async def test_link_endpoint_creates_pending_source(
    api_client: AsyncClient, capture_enqueue: list[uuid.UUID]
) -> None:
    r = await api_client.post(f"{API}/sources/link", json={"url": URL})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["kind"] == "url"
    assert body["status"] == "pending"
    assert URL in body["origin"]
    assert capture_enqueue == [uuid.UUID(body["id"])]


async def test_link_endpoint_rejects_bad_url(api_client: AsyncClient) -> None:
    r = await api_client.post(f"{API}/sources/link", json={"url": "not-a-url"})
    assert r.status_code == 422
