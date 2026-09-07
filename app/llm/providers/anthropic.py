"""Direct Anthropic (Claude) provider.

Uses the native ``anthropic`` SDK. ``system`` is a top-level parameter (not a message),
and we deliberately send no sampling/thinking params — they 400 on Opus 4.7+ and aren't
needed for tutor chat. Model selection is the registry's job; this provider is
model-agnostic.
"""

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import structlog
from anthropic import AsyncAnthropic

from app.llm.types import (
    ChatChunk,
    ChatMessage,
    ChatResponse,
    ChatRole,
    EmbedResult,
    ImagePart,
    TextPart,
    ToolCall,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
    Usage,
    text_of,
)

log = structlog.get_logger(__name__)

TRUNCATED = "max_tokens"
"""Anthropic's ``stop_reason`` when the model hit ``max_tokens`` mid-answer."""


def _warn_if_truncated(stop_reason: str | None, *, model: str, streaming: bool) -> bool:
    """A ``max_tokens`` cutoff is not an error to the SDK, and the caller cannot see it —
    downstream it surfaces as a JSON parse failure or a half-finished answer with no clue why."""
    if stop_reason != TRUNCATED:
        return False
    log.warning("llm.response_truncated", model=model, streaming=streaming)
    return True


class AnthropicProvider:
    name = "anthropic"
    supports_embeddings = False

    def __init__(self, *, api_key: str, timeout: float, max_retries: int) -> None:
        self._client = AsyncAnthropic(
            api_key=api_key or "missing", timeout=timeout, max_retries=max_retries
        )

    @staticmethod
    def _content(content: str | list) -> str | list[dict[str, Any]]:
        """Translate message content to Anthropic shape: a string, or content blocks."""
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
            elif isinstance(part, ToolUsePart):
                blocks.append(
                    {"type": "tool_use", "id": part.id, "name": part.name, "input": part.input}
                )
            elif isinstance(part, ToolResultPart):
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.tool_use_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
        return blocks

    @classmethod
    def _split(
        cls, messages: Sequence[ChatMessage], system: str | None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Anthropic takes ``system`` separately; messages are user/assistant only.

        Consecutive ``ChatRole.TOOL`` messages coalesce into one ``user`` message —
        Anthropic requires every pending ``tool_result`` in a single message, or it
        silently stops asking for parallel tool calls.
        """
        system_parts = [system] if system else []
        convo: list[dict[str, Any]] = []
        prev_was_tool_result = False
        for m in messages:
            if m.role is ChatRole.SYSTEM:
                system_parts.append(text_of(m.content))  # system is text-only
                prev_was_tool_result = False
            elif m.role is ChatRole.TOOL:
                content = cls._content(m.content)
                blocks = content if isinstance(content, list) else [content]
                if prev_was_tool_result:
                    convo[-1]["content"].extend(blocks)
                else:
                    convo.append({"role": "user", "content": blocks})
                prev_was_tool_result = True
            else:
                convo.append({"role": m.role.value, "content": cls._content(m.content)})
                prev_was_tool_result = False
        return ("\n\n".join(system_parts) or None), convo

    @staticmethod
    def _tools_payload(tools: Sequence[ToolDef] | None) -> dict[str, Any]:
        if not tools:
            return {}
        return {
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        }

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
        extra.update(self._tools_payload(tools))
        msg = await self._client.messages.create(
            model=model, max_tokens=max_tokens, messages=cast(Any, convo), **extra
        )
        content = "".join(b.text for b in msg.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in msg.content
            if b.type == "tool_use"
        ]
        usage = Usage(input_tokens=msg.usage.input_tokens, output_tokens=msg.usage.output_tokens)
        truncated = _warn_if_truncated(msg.stop_reason, model=model, streaming=False)
        return ChatResponse(
            content=content, usage=usage, model=model, tool_calls=tool_calls, truncated=truncated
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
        system_text, convo = self._split(messages, system)
        extra: dict[str, Any] = {"system": system_text} if system_text else {}
        extra.update(self._tools_payload(tools))
        async with self._client.messages.stream(
            model=model, max_tokens=max_tokens, messages=cast(Any, convo), **extra
        ) as stream:
            async for text in stream.text_stream:
                yield ChatChunk(text=text)
            # get_final_message() accumulates streamed input_json_delta fragments for us —
            # final.content's ToolUseBlock.input is already a fully-parsed dict.
            final = await stream.get_final_message()
            _warn_if_truncated(final.stop_reason, model=model, streaming=True)
            tool_calls = [
                ToolCall(id=b.id, name=b.name, input=b.input)
                for b in final.content
                if b.type == "tool_use"
            ]
            yield ChatChunk(
                usage=Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                ),
                tool_calls=tool_calls,
            )

    async def embed(self, *, model: str, texts: Sequence[str]) -> EmbedResult:
        raise NotImplementedError(
            "Anthropic has no embeddings API; route the EMBED role to ollama/openrouter."
        )
