"""OpenAI-compatible provider — serves both Ollama (local) and OpenRouter (cloud).

Both speak the OpenAI Chat Completions + Embeddings API, so a single implementation
configured with a different ``base_url`` covers both.
"""

from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from openai import AsyncOpenAI

from app.llm.types import ChatChunk, ChatMessage, ChatResponse, Usage


class OpenAICompatProvider:
    def __init__(self, *, name: str, base_url: str, api_key: str) -> None:
        self.name = name
        # Ollama ignores the key but the SDK requires a non-empty string.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")

    @staticmethod
    def _payload(messages: Sequence[ChatMessage], system: str | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        if system:
            out.append({"role": "system", "content": system})
        out.extend({"role": m.role.value, "content": m.content} for m in messages)
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
