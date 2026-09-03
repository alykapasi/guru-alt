"""RoleLM: the only bridge between DSPy and our role-based LLMClient (Phase 9c, D4).

DSPy reaches a model exclusively through this adapter — never litellm, never a provider SDK.
Bound to a ModelRole; the concrete model is resolved by the registry. Async-first (aforward) for
the runtime ingestion path; a sync forward() supports the offline BootstrapFewShot compile.
"""

from __future__ import annotations

import asyncio
import types

import dspy

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage


class RoleLM(dspy.BaseLM):
    forward_contract = "legacy"

    def __init__(self, role: ModelRole, client: LLMClient, *, max_tokens: int = 1024) -> None:
        super().__init__(model=f"role:{role.value}", max_tokens=max_tokens)
        self._role = role
        self._client = client
        self._max_tokens = max_tokens
        self.usage_sum = Usage()

    async def aforward(self, prompt=None, messages=None, **kwargs):
        system, chat = _split_messages(messages, prompt)
        resp = await self._client.complete(
            self._role, chat, system=system, max_tokens=self._max_tokens
        )
        self.usage_sum = Usage(
            input_tokens=self.usage_sum.input_tokens + resp.usage.input_tokens,
            output_tokens=self.usage_sum.output_tokens + resp.usage.output_tokens,
        )
        return _openai_response(resp.content, resp.model, resp.usage)

    def forward(self, prompt=None, messages=None, **kwargs):
        # Offline compile is synchronous with no running loop; drive the async client directly.
        return asyncio.run(self.aforward(prompt=prompt, messages=messages, **kwargs))


def _split_messages(
    messages: list[dict] | None, prompt: str | None
) -> tuple[str | None, list[ChatMessage]]:
    if not messages:
        return None, [ChatMessage(role=ChatRole.USER, content=prompt or "")]
    system: str | None = None
    chat: list[ChatMessage] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if role == "system":
            system = content if system is None else f"{system}\n{content}"
        else:
            chat.append(ChatMessage(role=ChatRole(role), content=content))
    return system, chat


class _UsageDict(dict):
    """A ``dict`` that also exposes its keys as attributes.

    dspy 3.3.1 internally does ``dict(response.usage)`` for cost tracking (fails on a
    ``types.SimpleNamespace``) — so this must be a real ``dict``. Callers that hold the raw
    ``forward()``/``aforward()`` return value (e.g. our own tests) still want the familiar
    ``resp.usage.total_tokens`` attribute read, so attribute access falls back to item access.
    """

    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _openai_response(content: str, model: str, usage: Usage):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=None))
        ],
        usage=_UsageDict(
            prompt_tokens=usage.input_tokens,
            completion_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
        ),
        model=model,
    )
