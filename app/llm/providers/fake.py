"""Deterministic, offline provider for tests."""

import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ToolCall, ToolDef, Usage, text_of


@dataclass(frozen=True)
class FakeTurn:
    """One scripted response: text and/or tool calls, for a scripted FakeProvider."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class FakeProvider:
    """Echoes a canned reply, word by word, or plays back a scripted sequence of turns.

    No network, fully deterministic. Accepts multimodal messages (images) and still
    returns its canned reply — this is what lets vision-OCR paths be exercised offline.
    """

    name = "fake"
    supports_embeddings = True

    def __init__(
        self,
        reply: str = "Hello from the fake tutor.",
        *,
        script: Sequence[FakeTurn] | None = None,
    ) -> None:
        self._reply = reply
        self._script = list(script) if script is not None else None
        self._call_index = 0

    def _next_turn(self) -> FakeTurn:
        turn = (
            self._script[self._call_index]
            if self._script and self._call_index < len(self._script)
            else FakeTurn(text=self._reply)
        )
        self._call_index += 1
        return turn

    def _usage(self, messages: Sequence[ChatMessage], turn: FakeTurn) -> Usage:
        return Usage(
            input_tokens=sum(len(text_of(m.content).split()) for m in messages),
            output_tokens=len(turn.text.split()),
        )

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        turn = self._next_turn()
        return ChatResponse(
            content=turn.text,
            usage=self._usage(messages, turn),
            model=model,
            tool_calls=turn.tool_calls,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        turn = self._next_turn()
        for i, word in enumerate(turn.text.split()):
            yield ChatChunk(text=word if i == 0 else f" {word}")
        yield ChatChunk(usage=self._usage(messages, turn), tool_calls=turn.tool_calls)

    async def embed(self, *, model: str, texts: Sequence[str]) -> list[list[float]]:
        # Match the configured embedding dim so fake vectors fit the pgvector column.
        dim = get_settings().embed_dim
        return [self._vec(t, dim) for t in texts]

    @staticmethod
    def _vec(text: str, dim: int) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(dim)]
