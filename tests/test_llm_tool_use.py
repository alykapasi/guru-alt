"""Tool-calling LLM layer: per-provider request/response translation (no network).

Mirrors test_llm_vision.py's approach: exercise each provider's translator directly
rather than hitting a real backend.
"""

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.types import (
    ChatMessage,
    ChatRole,
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
