"""The tool registry: build_tools + the search_materials/fetch_webpage tools.

search_materials tests are DB-backed, mirroring test_retrieval.py's fixture pattern (the tool
is a thin wrapper over that retrieval). fetch_webpage tests use a canned Fetcher, mirroring
test_weblink.py's _fake_fetch/_blocked_fetch pattern (no live network).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import Tool, build_tools
from app.core.config import get_settings
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus
from app.rag.fetch import FetchError

_FAKE = fake_llm_client()


async def _embed(text: str) -> list[float]:
    return (await _FAKE.embed(ModelRole.EMBED, [text]))[0]


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _source(session: AsyncSession, learner: Learner) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="x.txt",
        content_type="text/plain",
        status=SourceStatus.DONE,
        meta={},
    )
    session.add(source)
    await session.flush()
    return source


async def _chunk(session: AsyncSession, source: Source, text: str) -> Chunk:
    chunk = Chunk(
        source_id=source.id,
        ordinal=0,
        text=text,
        embedding=await _embed(text),
        provenance={"source_id": str(source.id), "method": "text"},
    )
    session.add(chunk)
    await session.flush()
    return chunk


def _search_materials(tools: list[Tool]) -> Tool:
    return next(t for t in tools if t.name == "search_materials")


async def test_search_materials_surfaces_seeded_chunk_content(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    source = await _source(db_session, learner)
    await _chunk(db_session, source, "mitochondria is the powerhouse of the cell")

    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id)
    result = await _search_materials(tools).execute({"query": "mitochondria"})

    assert not result.is_error
    assert "powerhouse of the cell" in result.content


async def test_search_materials_scoped_to_learner(db_session: AsyncSession) -> None:
    mine, theirs = await _learner(db_session), await _learner(db_session)
    await _chunk(db_session, await _source(db_session, theirs), "shared keyword content")

    tools = build_tools(db_session, fake_llm_client(), learner_id=mine.id)
    result = await _search_materials(tools).execute({"query": "shared"})

    assert not result.is_error
    assert "shared keyword content" not in result.content


async def test_search_materials_empty_query_is_a_tool_error_not_an_exception(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id)

    result = await _search_materials(tools).execute({"query": "   "})
    assert result.is_error


async def test_search_materials_missing_query_is_a_tool_error_not_an_exception(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id)

    result = await _search_materials(tools).execute({})
    assert result.is_error


async def test_search_materials_no_hits_is_not_an_error(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id)

    result = await _search_materials(tools).execute({"query": "nonexistent topic"})
    assert not result.is_error
    assert "No relevant passages" in result.content


# --- fetch_webpage ------------------------------------------------------------------------

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


async def _fake_html_fetch(url: str) -> tuple[bytes, str]:
    return _HTML, "text/html"


async def _fake_text_fetch(url: str) -> tuple[bytes, str]:
    return b"plain notes content", "text/plain"


async def _fake_pdf_fetch(url: str) -> tuple[bytes, str]:
    return b"%PDF-1.4 binary junk", "application/pdf"


async def _blocked_fetch(url: str) -> tuple[bytes, str]:
    raise FetchError(f"{url} resolves to a non-public address; refusing to fetch")


def _fetch_webpage(tools: list[Tool]) -> Tool:
    return next(t for t in tools if t.name == "fetch_webpage")


async def test_fetch_webpage_extracts_readable_text_from_html(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    tools = build_tools(
        db_session, fake_llm_client(), learner_id=learner.id, fetch=_fake_html_fetch
    )

    result = await _fetch_webpage(tools).execute({"url": "https://example.com/cell"})

    assert not result.is_error
    assert "powerhouse of the cell" in result.content
    assert "Home About Contact" not in result.content  # nav boilerplate stripped


async def test_fetch_webpage_falls_back_to_plain_decode_for_non_html(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    tools = build_tools(
        db_session, fake_llm_client(), learner_id=learner.id, fetch=_fake_text_fetch
    )

    result = await _fetch_webpage(tools).execute({"url": "https://example.com/notes.txt"})

    assert not result.is_error
    assert result.content == "plain notes content"


async def test_fetch_webpage_unsupported_content_type_is_a_tool_error(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id, fetch=_fake_pdf_fetch)

    result = await _fetch_webpage(tools).execute({"url": "https://example.com/doc.pdf"})
    assert result.is_error


async def test_fetch_webpage_missing_url_is_a_tool_error_not_an_exception(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    tools = build_tools(
        db_session, fake_llm_client(), learner_id=learner.id, fetch=_fake_html_fetch
    )

    result = await _fetch_webpage(tools).execute({})
    assert result.is_error


async def test_fetch_webpage_blocked_fetch_degrades_gracefully(db_session: AsyncSession) -> None:
    """The concrete regression test for 'prompt injection tells the model to fetch an
    internal URL' — a blocked fetch must degrade the turn, never crash it."""
    learner = await _learner(db_session)
    tools = build_tools(db_session, fake_llm_client(), learner_id=learner.id, fetch=_blocked_fetch)

    result = await _fetch_webpage(tools).execute({"url": "http://169.254.169.254/"})

    assert result.is_error
    assert "non-public address" in result.content


async def test_fetch_webpage_truncates_to_the_configured_cap(db_session: AsyncSession) -> None:
    max_chars = get_settings().fetch_webpage_max_chars
    long_text = "word " * (max_chars // 4)  # comfortably exceeds the cap once extracted

    async def _fake_long_fetch(url: str) -> tuple[bytes, str]:
        return long_text.encode(), "text/plain"

    learner = await _learner(db_session)
    tools = build_tools(
        db_session, fake_llm_client(), learner_id=learner.id, fetch=_fake_long_fetch
    )

    result = await _fetch_webpage(tools).execute({"url": "https://example.com/long"})

    assert not result.is_error
    assert len(result.content) <= max_chars + len("\n...[truncated]")
