"""Tool-calling LLM layer: per-provider request/response translation (no network).

Mirrors test_llm_vision.py's approach: exercise each provider's translator directly
rather than hitting a real backend.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from structlog.testing import capture_logs

from app.llm.base import LLMProvider
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.registry import LLMClient, LLMConfigError, ModelSpec, fake_llm_client
from app.llm.types import (
    ChatMessage,
    ChatRole,
    ModelRole,
    TextPart,
    ToolCall,
    ToolDef,
    ToolResultPart,
    ToolUsePart,
)

# --- Anthropic translation --------------------------------------------------


def test_anthropic_content_translates_tool_use_and_result() -> None:
    use_msg = ChatMessage(
        role=ChatRole.ASSISTANT,
        content=[ToolUsePart(id="t1", name="search", input={"query": "x"})],
    )
    blocks = AnthropicProvider._content(use_msg.content)
    assert blocks == [{"type": "tool_use", "id": "t1", "name": "search", "input": {"query": "x"}}]

    result_msg = ChatMessage(
        role=ChatRole.TOOL,
        content=[ToolResultPart(tool_use_id="t1", content="found it")],
    )
    blocks = AnthropicProvider._content(result_msg.content)
    assert blocks == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "found it", "is_error": False}
    ]


def test_anthropic_split_coalesces_consecutive_tool_messages() -> None:
    msgs = [
        ChatMessage(role=ChatRole.USER, content="search for x"),
        ChatMessage(
            role=ChatRole.ASSISTANT,
            content=[
                ToolUsePart(id="t1", name="search", input={}),
                ToolUsePart(id="t2", name="search", input={}),
            ],
        ),
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t1", content="a")]),
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t2", content="b")]),
    ]
    _system, convo = AnthropicProvider._split(msgs, system=None)
    assert len(convo) == 3  # user, assistant (2 tool_use blocks), one coalesced user (2 results)
    assert convo[2]["role"] == "user"
    assert convo[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "a", "is_error": False},
        {"type": "tool_result", "tool_use_id": "t2", "content": "b", "is_error": False},
    ]


def test_anthropic_split_does_not_coalesce_across_a_non_tool_message() -> None:
    msgs = [
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t1", content="a")]),
        ChatMessage(role=ChatRole.USER, content="thanks"),
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t2", content="b")]),
    ]
    _system, convo = AnthropicProvider._split(msgs, system=None)
    assert len(convo) == 3


def test_anthropic_tools_payload_omitted_when_no_tools() -> None:
    assert AnthropicProvider._tools_payload(None) == {}
    assert AnthropicProvider._tools_payload([]) == {}


def test_anthropic_tools_payload_translates_tool_def() -> None:
    payload = AnthropicProvider._tools_payload(
        [ToolDef(name="search", description="Search stuff.", parameters={"type": "object"})]
    )
    assert payload == {
        "tools": [
            {"name": "search", "description": "Search stuff.", "input_schema": {"type": "object"}}
        ]
    }


# --- OpenAI-compatible translation ------------------------------------------


def test_openai_message_translates_assistant_tool_use() -> None:
    msg = ChatMessage(
        role=ChatRole.ASSISTANT,
        content=[
            TextPart(text="checking..."),
            ToolUsePart(id="t1", name="search", input={"q": "x"}),
        ],
    )
    payload = OpenAICompatProvider._message(msg)
    assert payload == {
        "role": "assistant",
        "content": "checking...",
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "search", "arguments": '{"q": "x"}'},
            }
        ],
    }


def test_openai_message_without_tool_use_is_unchanged() -> None:
    msg = ChatMessage(role=ChatRole.USER, content="hello")
    assert OpenAICompatProvider._message(msg) == {"role": "user", "content": "hello"}


def test_openai_payload_tool_results_are_not_coalesced() -> None:
    msgs = [
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t1", content="a")]),
        ChatMessage(role=ChatRole.TOOL, content=[ToolResultPart(tool_use_id="t2", content="b")]),
    ]
    payload = OpenAICompatProvider._payload(msgs, system=None)
    assert payload == [
        {"role": "tool", "tool_call_id": "t1", "content": "a"},
        {"role": "tool", "tool_call_id": "t2", "content": "b"},
    ]


def test_openai_tools_payload_omitted_when_no_tools() -> None:
    assert OpenAICompatProvider._tools_payload(None) == {}
    assert OpenAICompatProvider._tools_payload([]) == {}


def test_openai_tools_payload_translates_tool_def() -> None:
    payload = OpenAICompatProvider._tools_payload(
        [ToolDef(name="search", description="Search stuff.", parameters={"type": "object"})]
    )
    assert payload == {
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "description": "Search stuff.",
                    "parameters": {"type": "object"},
                },
            }
        ]
    }


# --- FakeProvider scripting --------------------------------------------------


async def test_fake_provider_plays_back_a_scripted_sequence() -> None:
    script = [
        FakeTurn(tool_calls=[ToolCall(id="t1", name="search", input={"query": "x"})]),
        FakeTurn(text="here is the answer"),
    ]
    provider = FakeProvider(script=script)
    messages = [ChatMessage(role=ChatRole.USER, content="go")]

    first = await provider.complete(model="fake-1", messages=messages)
    assert first.content == ""
    assert first.tool_calls == [ToolCall(id="t1", name="search", input={"query": "x"})]

    second = await provider.complete(model="fake-1", messages=messages)
    assert second.content == "here is the answer"
    assert second.tool_calls == []


async def test_fake_provider_repeats_canned_reply_past_the_end_of_the_script() -> None:
    provider = FakeProvider(reply="fallback", script=[FakeTurn(text="only turn")])
    messages = [ChatMessage(role=ChatRole.USER, content="go")]
    await provider.complete(model="fake-1", messages=messages)  # consumes the one scripted turn
    third = await provider.complete(model="fake-1", messages=messages)
    assert third.content == "fallback"


async def test_fake_provider_stream_plays_back_scripted_tool_calls() -> None:
    provider = FakeProvider(script=[FakeTurn(tool_calls=[ToolCall(id="t1", name="s", input={})])])
    chunks = [
        c
        async for c in provider.stream(
            model="fake-1", messages=[ChatMessage(role=ChatRole.USER, content="go")]
        )
    ]
    assert "".join(c.text for c in chunks) == ""
    tool_calls = next(c.tool_calls for c in chunks if c.tool_calls)
    assert tool_calls == [ToolCall(id="t1", name="s", input={})]


async def test_fake_provider_unscripted_behaviour_is_unchanged() -> None:
    provider = FakeProvider(reply="hi there friend")
    messages = [ChatMessage(role=ChatRole.USER, content="x")]
    first = await provider.complete(model="fake-1", messages=messages)
    second = await provider.complete(model="fake-1", messages=messages)
    assert first.content == second.content == "hi there friend"


# --- OpenAI-compatible streaming contract (S49) -------------------------------
#
# `stream_options={"include_usage": True}` is an OpenAI extension. A compatible endpoint is
# free to ignore it, and the adapter used to finalize its accumulated tool calls *only* on a
# usage-carrying chunk — so against such an endpoint every tool call was silently discarded.
# These drive the adapter with stubbed chunk objects shaped like the SDK's.


def _tool_delta(index: int, *, call_id: str = "", name: str = "", arguments: str = "") -> Any:
    return SimpleNamespace(
        index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
) -> Any:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _usage_chunk(input_tokens: int, output_tokens: int) -> Any:
    """The terminal chunk OpenAI sends: no choices, usage only."""
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens),
    )


class _StubStream:
    """Shaped like the SDK's ``AsyncStream``: async-iterable *and* an async context manager,
    recording whether it was closed so the disconnect path can be asserted on."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aenter__(self) -> "_StubStream":
        return self

    async def __aexit__(self, *_: object) -> bool:
        self.closed = True
        return False

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for chunk in self._chunks:
                yield chunk

        return gen()


def _provider_streaming(chunks: list[Any]) -> tuple[OpenAICompatProvider, _StubStream]:
    stub = _StubStream(chunks)

    async def create(**_: object) -> _StubStream:
        return stub

    provider = OpenAICompatProvider(
        name="stub", base_url="http://stub", api_key="", timeout=1.0, max_retries=0
    )
    provider._client = SimpleNamespace(  # ty: ignore[invalid-assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, stub


async def _collect(provider: OpenAICompatProvider) -> list[Any]:
    return [
        c
        async for c in provider.stream(
            model="m", messages=[ChatMessage(role=ChatRole.USER, content="go")]
        )
    ]


async def test_openai_stream_keeps_tool_calls_when_the_endpoint_reports_no_usage() -> None:
    provider, _stub = _provider_streaming(
        [
            _chunk(tool_calls=[_tool_delta(0, call_id="call_1", name="fetch_webpage")]),
            _chunk(tool_calls=[_tool_delta(0, arguments='{"url":')]),
            _chunk(tool_calls=[_tool_delta(0, arguments='"https://x"}')]),
        ]
    )
    chunks = await _collect(provider)

    tool_calls = [tc for c in chunks for tc in c.tool_calls]
    assert tool_calls == [ToolCall(id="call_1", name="fetch_webpage", input={"url": "https://x"})]


async def test_openai_stream_reports_usage_with_the_tool_calls_when_it_is_sent() -> None:
    provider, _stub = _provider_streaming(
        [
            _chunk(tool_calls=[_tool_delta(0, call_id="c1", name="a", arguments="{}")]),
            _chunk(tool_calls=[_tool_delta(1, call_id="c2", name="b", arguments="{}")]),
            _usage_chunk(11, 3),
        ]
    )
    chunks = await _collect(provider)

    terminal = [c for c in chunks if c.usage is not None]
    assert len(terminal) == 1  # exactly one terminal chunk, never two
    assert terminal[0].usage.input_tokens == 11
    assert terminal[0].usage.output_tokens == 3
    assert [tc.name for tc in terminal[0].tool_calls] == ["a", "b"]


async def test_openai_stream_of_plain_text_is_unchanged() -> None:
    """No tool calls and no usage: nothing extra is emitted."""
    provider, _stub = _provider_streaming([_chunk(content="hel"), _chunk(content="lo")])
    chunks = await _collect(provider)
    assert "".join(c.text or "" for c in chunks) == "hello"
    assert all(c.usage is None for c in chunks)


async def test_openai_stream_closes_the_response_when_the_consumer_hangs_up() -> None:
    """Every SSE client that navigates away closes this generator mid-stream."""
    provider, stub = _provider_streaming([_chunk(content="a"), _chunk(content="b")])
    # The Protocol promises an AsyncIterator; every implementation is a generator, and closing
    # one is exactly what Starlette does to an abandoned SSE response.
    turn = cast(
        AsyncGenerator[Any],
        provider.stream(model="m", messages=[ChatMessage(role=ChatRole.USER, content="go")]),
    )
    async for _ in turn:
        break  # consumer stops caring after the first token
    await turn.aclose()

    assert stub.closed is True


async def test_openai_stream_records_a_truncated_answer() -> None:
    """A max_tokens cutoff is not an SDK error; downstream it looks like malformed output."""
    provider, _stub = _provider_streaming([_chunk(content="half an ans", finish_reason="length")])
    with capture_logs() as logs:
        await _collect(provider)
    assert any(entry["event"] == "llm.response_truncated" for entry in logs)


async def test_openai_stream_survives_tool_arguments_that_are_not_json() -> None:
    """A model can emit invalid JSON. The tool gets no arguments — and someone gets told."""
    provider, _stub = _provider_streaming(
        [
            _chunk(tool_calls=[_tool_delta(0, call_id="c1", name="fetch", arguments="{not json")]),
            _usage_chunk(1, 1),
        ]
    )
    with capture_logs() as logs:
        chunks = await _collect(provider)

    tool_calls = [tc for c in chunks for tc in c.tool_calls]
    assert tool_calls == [ToolCall(id="c1", name="fetch", input={})]
    assert any(entry["event"] == "llm.tool_arguments_unparseable" for entry in logs)


# --- registry configuration is validated up front (S49) -----------------------


def _roles(overrides: dict[ModelRole, ModelSpec] | None = None) -> dict[ModelRole, ModelSpec]:
    base = {role: ModelSpec(provider="fake", model="fake-1") for role in ModelRole}
    return {**base, **(overrides or {})}


def test_an_unknown_provider_name_is_refused_when_the_registry_is_built() -> None:
    """Previously this was a KeyError raised inside a learner's turn."""
    providers: dict[str, LLMProvider] = {"fake": FakeProvider()}
    with pytest.raises(LLMConfigError) as exc:
        LLMClient(providers, _roles({ModelRole.SMART: ModelSpec("openrouterr", "x")}))
    assert "GURU_MODEL_SMART" in str(exc.value) and "fake" in str(exc.value)


def test_embeddings_cannot_be_routed_at_a_provider_that_has_none() -> None:
    providers: dict[str, LLMProvider] = {
        "fake": FakeProvider(),
        "anthropic": AnthropicProvider(api_key="k", timeout=1.0, max_retries=0),
    }
    with pytest.raises(LLMConfigError) as exc:
        LLMClient(providers, _roles({ModelRole.EMBED: ModelSpec("anthropic", "claude")}))
    assert "GURU_MODEL_EMBED" in str(exc.value)


def test_a_chat_role_on_a_provider_without_embeddings_is_fine() -> None:
    """Only EMBED needs the embeddings API — SMART on Anthropic is the intended prod map."""
    providers: dict[str, LLMProvider] = {
        "fake": FakeProvider(),
        "anthropic": AnthropicProvider(api_key="k", timeout=1.0, max_retries=0),
    }
    client = LLMClient(providers, _roles({ModelRole.SMART: ModelSpec("anthropic", "claude")}))
    assert client.spec(ModelRole.SMART).provider == "anthropic"


def test_swapping_roles_revalidates() -> None:
    """The sweep builds clients with with_roles; a bad cell should fail before it runs."""
    client = fake_llm_client()
    with pytest.raises(LLMConfigError):
        client.with_roles({ModelRole.FAST: ModelSpec("nope", "m")})
