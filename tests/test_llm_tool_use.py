"""Tool-calling LLM layer: per-provider request/response translation (no network).

Mirrors test_llm_vision.py's approach: exercise each provider's translator directly
rather than hitting a real backend.
"""

from app.llm.providers.anthropic import AnthropicProvider
from app.llm.types import ChatMessage, ChatRole, ToolDef, ToolResultPart, ToolUsePart

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
