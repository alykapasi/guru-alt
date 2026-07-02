"""OpenAI-compatible provider — serves both Ollama (local) and OpenRouter (cloud).

Both speak the OpenAI Chat Completions + Embeddings API, so a single implementation
configured with a different ``base_url`` covers both.
"""

import base64
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from openai import AsyncOpenAI

from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ImagePart, TextPart, Usage


class OpenAICompatProvider:
    def __init__(self, *, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        # Ollama ignores the key but the SDK requires a non-empty string.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")

    @staticmethod
    def _content(content: str | list) -> str | list[dict[str, Any]]:
        """Translate message content to OpenAI shape: a string, or text/image_url parts."""
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
    def _payload(cls, messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend({"role": m.role.value, "content": cls._content(m.content)} for m in messages)
        return out

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> ChatResponse:
        resp = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, self._payload(messages, system)),
            max_tokens=max_tokens,
        )
        usage = Usage()
        if resp.usage is not None:
            usage = Usage(
                input_tokens=resp.usage.prompt_tokens,
                output_tokens=resp.usage.completion_tokens,
            )
        content = resp.choices[0].message.content or ""
        return ChatResponse(content=content, usage=usage, model=model)

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
    ) -> AsyncIterator[ChatChunk]:
        stream = await self._client.chat.completions.create(
            model=model,
            messages=cast(Any, self._payload(messages, system)),
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if chunk.usage is not None:
                yield ChatChunk(
                    usage=Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )
                )
            if chunk.choices and (delta := chunk.choices[0].delta.content):
                yield ChatChunk(text=delta)

    async def embed(self, *, model: str, texts: Sequence[str]) -> list[list[float]]:
        resp = await self._client.embeddings.create(model=model, input=list(texts))
        return [item.embedding for item in resp.data]
