"""The tool registry: build_tools + the search_materials tool (DB-backed, mirrors
test_retrieval.py's fixture pattern — the tool is a thin wrapper over that retrieval).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools import Tool, build_tools
from app.llm import ModelRole
from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, Source, SourceKind, SourceStatus

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
