"""Direct Anthropic (Claude) provider.

Uses the native ``anthropic`` SDK. ``system`` is a top-level parameter (not a message),
and we deliberately send no sampling/thinking params — they 400 on Opus 4.7+ and aren't
needed for tutor chat. Model selection is the registry's job; this provider is
model-agnostic.
"""

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from anthropic import AsyncAnthropic

from app.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatResponse,
    ChatRole,
    ImagePart,
    TextPart,
    ToolDef,
    Usage,
    text_of,
)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key or "missing")

    @staticmethod
    def _content(content: str | list) -> str | list[dict[str, Any]]:
        """Translate message content to Anthropic shape: a string, or text/image blocks."""
        if isinstance(content, str):
            return content
        blocks: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, TextPart):
                blocks.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                b64 = base64.b64encode(part.data).decode()
                source = {"type": "base64", "media_type": part.media_type, "data": b64}
                blocks.append({"type": "image", "source": source})
        return blocks

    @classmethod
    def _split(
        cls, messages: Sequence[ChatMessage], system: str | None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Anthropic takes ``system`` separately; messages are user/assistant only."""
        system_parts = [system] if system else []
        convo: list[dict[str, Any]] = []
        for m in messages:
            if m.role is ChatRole.SYSTEM:
                system_parts.append(text_of(m.content))  # system is text-only
            else:
                convo.append({"role": m.role.value, "content": cls._content(m.content)})
        return ("\n\n".join(system_parts) or None), convo

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        system_text, convo = self._split(messages, system)
        extra: dict[str, Any] = {"system": system_text} if system_text else {}
        msg = await self._client.messages.create(
            model=model, max_tokens=max_tokens, messages=cast(Any, convo), **extra
        )
        content = "".join(b.text for b in msg.content if b.type == "text")
        usage = Usage(input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
        return ChatResponse(content=content, usage=usage, model=model)

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        system_text, convo = self._split(messages, system)
        extra: dict[str, Any] = {"system": system_text} if system_text else {}
        async with self._client.messages.stream(
            model=model, max_tokens=max_tokens, messages=cast(Any, convo), **extra
        ) as stream:
            async for text in stream.text_stream:
                yield ChatChunk(text=text)
            final = await stream.get_final_message()
            yield ChatChunk(
                usage=Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                )
            )

    async def embed(self, *, model: str, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError(
            "Anthropic has no embeddings API; route the EMBED role to ollama/openrouter."
        )
