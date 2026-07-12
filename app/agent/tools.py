"""The tool registry: per-turn tool sets an agentic graph can call.

``build_tools`` closes over the request's ``session``/``llm``/``learner_id`` (mirrors
``build_tutor_graph(llm)`` closing over the request's LLM client) — a fresh list is built
per turn, never a global mutable registry, so a learner's session never leaks across
requests.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import trafilatura
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm import LLMClient, ToolDef
from app.rag.fetch import Fetcher, FetchError, safe_fetch
from app.rag.retrieval import RetrievalHit, retrieve

_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_UNSUPPORTED_CONTENT_TYPE_PREFIXES = ("image/", "audio/", "video/")
_UNSUPPORTED_CONTENT_TYPES = {"application/pdf", "application/octet-stream"}

__all__ = ["Tool", "ToolResult", "build_tools"]


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
    fetch: Fetcher = safe_fetch,
) -> list[Tool]:
    """The tool set for one turn."""
    return [
        _search_materials_tool(session, llm, learner_id=learner_id, subject_id=subject_id),
        _fetch_webpage_tool(fetch=fetch),
    ]


def _format_hits(hits: Sequence[RetrievalHit]) -> str:
    if not hits:
        return "No relevant passages found in the learner's materials."
    return "\n".join(f"{i}. [source={h.source_id}] {h.text}" for i, h in enumerate(hits, start=1))


def _search_materials_tool(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, subject_id: uuid.UUID | None
) -> Tool:
    async def execute(args: dict[str, object]) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult(content="A non-empty query is required.", is_error=True)
        hits = await retrieve(session, llm, query, learner_id=learner_id, subject_id=subject_id)
        return ToolResult(content=_format_hits(hits))

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
