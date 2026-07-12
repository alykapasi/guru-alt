"""The tool registry: per-turn tool sets an agentic graph can call.

``build_tools`` closes over the request's ``session``/``llm``/``learner_id`` (mirrors
``build_tutor_graph(llm)`` closing over the request's LLM client) — a fresh list is built
per turn, never a global mutable registry, so a learner's session never leaks across
requests.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.llm import LLMClient, ToolDef
from app.rag.retrieval import RetrievalHit, retrieve

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
) -> list[Tool]:
    """The tool set for one turn."""
    return [_search_materials_tool(session, llm, learner_id=learner_id, subject_id=subject_id)]


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
