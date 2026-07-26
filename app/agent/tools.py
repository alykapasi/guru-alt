"""The tool registry: per-turn tool sets an agentic graph can call.

``build_tools`` closes over the request's ``session``/``llm``/``learner_id`` (mirrors
``build_tutor_graph(llm)`` closing over the request's LLM client) — a fresh list is built
per turn, never a global mutable registry, so a learner's session never leaks across
requests.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import trafilatura
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ToolDef
from app.rag.fetch import Fetcher, FetchError, safe_fetch
from app.rag.retrieval import RetrievalHit, retrieve
from app.services.turn_common import format_grounding

_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_UNSUPPORTED_CONTENT_TYPE_PREFIXES = ("image/", "audio/", "video/")
_UNSUPPORTED_CONTENT_TYPES = {"application/pdf", "application/octet-stream"}

__all__ = ["CitationAccumulator", "Tool", "ToolResult", "build_tools"]


@dataclass
class CitationAccumulator:
    """Turn-scoped, ordered record of every chunk ``search_materials`` has retrieved so far.

    The model may call the tool several times in one agentic turn; numbering must stay stable
    and deduped across all of them — a chunk retrieved again on a later call keeps its first
    marker number rather than getting a new one. Passed by the caller (``run_agentic_turn``) so
    it can read the final set after the loop ends; a fresh one per turn, never shared/global.
    """

    hits: list[RetrievalHit] = field(default_factory=list)
    _seen: set[uuid.UUID] = field(default_factory=set)

    def add(self, hits: Sequence[RetrievalHit]) -> None:
        for hit in hits:
            if hit.chunk_id not in self._seen:
                self._seen.add(hit.chunk_id)
                self.hits.append(hit)


@dataclass(frozen=True)
class ToolResult:
    """A tool's outcome — always returned, never raised, so a bad call degrades the turn
    rather than crashing it."""

    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, object]  # JSON schema for the tool's model-controlled input
    execute: Callable[[dict[str, object]], Awaitable[ToolResult]]

    def to_def(self) -> ToolDef:
        return ToolDef(name=self.name, description=self.description, parameters=self.parameters)


def build_tools(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None = None,
    source_ids: Sequence[uuid.UUID] | None = None,
    citations: CitationAccumulator | None = None,
    fetch: Fetcher = safe_fetch,
) -> list[Tool]:
    """The tool set for one turn. ``citations`` defaults to a fresh, throwaway accumulator when
    the caller doesn't need to read it back (e.g. most existing tests) — pass one explicitly
    (``run_agentic_turn`` does) to collect what was cited across the whole turn."""
    citations = citations if citations is not None else CitationAccumulator()
    return [
        _search_materials_tool(
            session,
            llm,
            learner_id=learner_id,
            subject_id=subject_id,
            source_ids=source_ids,
            citations=citations,
        ),
        _fetch_webpage_tool(fetch=fetch),
    ]


def _search_materials_tool(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    source_ids: Sequence[uuid.UUID] | None,
    citations: CitationAccumulator,
) -> Tool:
    async def execute(args: dict[str, object]) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(content="A non-empty query is required.", is_error=True)
        hits = await retrieve(
            session,
            llm,
            query,
            learner_id=learner_id,
            subject_id=subject_id,
            source_ids=source_ids,
        )
        citations.add(hits)
        # The full accumulated set, not just this call's hits — keeps [N] numbering stable and
        # consistent across every search_materials call in this turn (see CitationAccumulator).
        grounding = format_grounding(citations.hits)
        return ToolResult(
            content=grounding or "No relevant passages found in the learner's materials."
        )

    return Tool(
        name="search_materials",
        description=(
            "Search the learner's uploaded materials and notes for passages relevant to a "
            "query. Use this when the learner asks about specific content from their "
            "sources, or grounding beyond general knowledge would help."
        ),
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to search for."}},
            "required": ["query"],
        },
        execute=execute,
    )


def _is_unsupported_content_type(content_type: str) -> bool:
    return content_type in _UNSUPPORTED_CONTENT_TYPES or content_type.startswith(
        _UNSUPPORTED_CONTENT_TYPE_PREFIXES
    )


def _extract_text(data: bytes, content_type: str) -> str:
    decoded = data.decode("utf-8", errors="replace")
    if content_type in _HTML_CONTENT_TYPES:
        return trafilatura.extract(decoded) or ""
    return decoded


def _fetch_webpage_tool(*, fetch: Fetcher) -> Tool:
    async def execute(args: dict[str, object]) -> ToolResult:
        url = args.get("url")
        if not isinstance(url, str) or not url.strip():
            return ToolResult(content="A non-empty url is required.", is_error=True)
        try:
            data, content_type = await fetch(url)
        except FetchError as exc:
            return ToolResult(content=str(exc), is_error=True)
        if _is_unsupported_content_type(content_type):
            return ToolResult(content=f"Unsupported content type: {content_type}", is_error=True)
        text = _extract_text(data, content_type).strip()
        if not text:
            return ToolResult(content=f"No readable text content found at {url}.", is_error=True)
        max_chars = get_settings().fetch_webpage_max_chars
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return ToolResult(content=text)

    return Tool(
        name="fetch_webpage",
        description=(
            "Fetch a webpage by URL and return its readable text. Use this when the learner "
            "references an external page/article or asks about something beyond your "
            "knowledge or the learner's own materials. Only public http/https URLs are "
            "supported — private, loopback, and internal addresses are refused."
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The absolute http(s) URL to fetch."}
            },
            "required": ["url"],
        },
        execute=execute,
    )
