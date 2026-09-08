"""OpenAI-compatible provider — serves both Ollama (local) and OpenRouter (cloud).

Both speak the OpenAI Chat Completions + Embeddings API, so a single implementation
configured with a different ``base_url`` covers both.
"""

import base64
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import structlog
from openai import AsyncOpenAI

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
)

log = structlog.get_logger(__name__)

TRUNCATED = "length"
"""OpenAI's ``finish_reason`` when the model hit ``max_tokens`` mid-answer."""


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Tool-call arguments arrive as a JSON string; tolerate a malformed one from the model.

    Tolerate, but say so: an empty argument dict and "the model produced invalid JSON" look
    identical to the tool that receives them, and only one of those is worth investigating.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("llm.tool_arguments_unparseable", raw=raw[:200])
        return {}
    if not isinstance(parsed, dict):
        log.warning("llm.tool_arguments_not_an_object", raw=raw[:200])
        return {}
    return parsed


def _warn_if_truncated(finish_reason: str | None, *, model: str, streaming: bool) -> bool:
    """A ``max_tokens`` cutoff is not an error to the SDK, and the caller cannot see it.

    Downstream it surfaces as a JSON parse failure or a half-finished explanation with no clue
    why, so the one place that knows records it.
    """
    if finish_reason != TRUNCATED:
        return False
    log.warning("llm.response_truncated", model=model, streaming=streaming)
    return True


class OpenAICompatProvider:
    supports_embeddings = True

    def __init__(
        self, *, name: str, base_url: str, api_key: str, timeout: float, max_retries: int
    ) -> None:
        self.name = name
        # Ollama ignores the key but the SDK requires a non-empty string.
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "not-needed",
            timeout=timeout,
            max_retries=max_retries,
        )

    @staticmethod
    def _content(content: str | list) -> str | list[dict[str, Any]]:
        """Translate message content to OpenAI shape: a string, or text/image_url parts.

        ToolUsePart/ToolResultPart are handled separately by ``_message``/``_tool_results`` —
        OpenAI represents them as sibling message fields (``tool_calls``, ``role: "tool"``),
        not content parts.
        """
        if isinstance(content, str):
            return content
        out: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, TextPart):
                out.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                b64 = base64.b64encode(part.data).decode()
                url = f"data:{part.media_type};base64,{b64}"
                out.append({"type": "image_url", "image_url": {"url": url}})
        return out

    @classmethod
    def _message(cls, m: ChatMessage) -> dict[str, Any]:
        """A non-tool-result message. An assistant message carrying ToolUsePart(s) gets
        OpenAI's ``tool_calls`` field instead of those parts inline in ``content``."""
        if isinstance(m.content, str):
            return {"role": m.role.value, "content": m.content}
        tool_uses = [p for p in m.content if isinstance(p, ToolUsePart)]
        if not tool_uses:
            return {"role": m.role.value, "content": cls._content(m.content)}
        text = next((p.text for p in m.content if isinstance(p, TextPart)), None)
        return {
            "role": m.role.value,
            "content": text,
            "tool_calls": [
                {
                    "id": t.id,
                    "type": "function",
                    "function": {"name": t.name, "arguments": json.dumps(t.input)},
                }
                for t in tool_uses
            ],
        }

    @staticmethod
    def _tool_results(content: str | list) -> list[dict[str, Any]]:
        """OpenAI wants one ``{"role": "tool", ...}`` message per result — no coalescing
        (contrast Anthropic, which requires them batched into a single message)."""
        if isinstance(content, str):
            return []
        return [
            {"role": "tool", "tool_call_id": p.tool_use_id, "content": p.content}
            for p in content
            if isinstance(p, ToolResultPart)
        ]

    @classmethod
    def _payload(cls, messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role is ChatRole.TOOL:
                out.extend(cls._tool_results(m.content))
            else:
                out.append(cls._message(m))
        return out

    @staticmethod
    def _tools_payload(tools: Sequence[ToolDef] | None) -> dict[str, Any]:
        if not tools:
            return {}
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
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
        resp = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, self._payload(messages, system)),
            max_tokens=max_tokens,
            **self._tools_payload(tools),
        )
        usage = Usage()
        if resp.usage is not None:
            usage = Usage(
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
            )
        truncated = _warn_if_truncated(resp.choices[0].finish_reason, model=model, streaming=False)
        message = resp.choices[0].message
        content = message.content or ""
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, input=_parse_arguments(tc.function.arguments))
            for tc in (message.tool_calls or [])
        ]
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
        stream = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, self._payload(messages, system)),
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **self._tools_payload(tools),
        )
        # Unlike Anthropic, OpenAI has no server-side accumulation helper: tool calls stream
        # as index-keyed partial deltas (id/name typically only on the first delta for that
        # index, arguments as string fragments) — accumulate per index and finalize once.
        pending: dict[int, dict[str, str]] = {}
        usage: Usage | None = None
        # `async with` matters here: an SSE client hanging up closes *this* generator, and
        # without it the underlying HTTP response is left open until garbage collection.
        async with stream:
            async for chunk in stream:
                if chunk.choices:
                    choice = chunk.choices[0]
                    delta = choice.delta
                    _warn_if_truncated(choice.finish_reason, model=model, streaming=True)
                    if delta.content:
                        yield ChatChunk(text=delta.content)
                    for tc in delta.tool_calls or []:
                        acc = pending.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            acc["id"] = tc.id
                        if tc.function is not None:
                            if tc.function.name:
                                acc["name"] = tc.function.name
                            if tc.function.arguments:
                                acc["arguments"] += tc.function.arguments
                if chunk.usage is not None:
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )
        # Finalize when the stream ends, not when usage happens to arrive. `include_usage` is
        # an OpenAI extension: a compatible endpoint is free to ignore it, and one that does
        # used to have every tool call it had just streamed silently discarded here.
        tool_calls = [
            ToolCall(id=acc["id"], name=acc["name"], input=_parse_arguments(acc["arguments"]))
            for acc in pending.values()
        ]
        if usage is not None or tool_calls:
            yield ChatChunk(usage=usage or Usage(), tool_calls=tool_calls)

    async def embed(self, *, model: str, texts: Sequence[str]) -> EmbedResult:
        resp = await self._client.embeddings.create(model=model, input=list(texts))
        # Ollama's OpenAI-compatible endpoint omits usage; a missing count is 0, not a crash.
        prompt_tokens = getattr(resp.usage, "prompt_tokens", 0) if resp.usage else 0
        return EmbedResult(
            vectors=[item.embedding for item in resp.data],
            usage=Usage(input_tokens=prompt_tokens),  # embeddings produce no output tokens
        )
