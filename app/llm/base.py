"""The provider interface every backend implements."""

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ToolDef


@runtime_checkable
class LLMProvider(Protocol):
    """A chat + embeddings backend. Implementations translate to/from their SDK."""

    name: str

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse: ...

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]: ...

    async def embed(self, *, model: str, texts: Sequence[str]) -> list[list[float]]: ...
