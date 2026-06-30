"""Deterministic, offline provider for tests."""

import hashlib
from collections.abc import AsyncIterator, Sequence

from app.llm.types import ChatChunk, ChatMessage, ChatResponse, Usage


class FakeProvider:
    """Echoes a canned reply, word by word. No network, fully deterministic."""

    name = "fake"

    def __init__(self, reply: str = "Hello from the fake tutor.") -> None:
        self._reply = reply

    def _usage(self, messages: Sequence[ChatMessage]) -> Usage:
        return Usage(
            input_tokens=sum(len(m.content.split()) for m in messages),
            output_tokens=len(self._reply.split()),
        )

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        return ChatResponse(content=self._reply, usage=self._usage(messages), model=model)

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[ChatChunk]:
        for i, word in enumerate(self._reply.split()):
            yield ChatChunk(text=word if i == 0 else f" {word}")
        yield ChatChunk(usage=self._usage(messages))

    async def embed(self, *, model: str, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    @staticmethod
    def _vec(text: str, dim: int = 8) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(dim)]
