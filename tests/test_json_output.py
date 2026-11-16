"""A caller that parses a JSON object can require one, and a provider that can enforce it does.

A small local model asked in prose for "a JSON object only" sometimes writes something else, and
a curriculum, a question or a grade is then lost to a parse error. Ollama and OpenRouter can
constrain the reply to valid JSON; asking for that is the caller's decision, enforcing it the
provider's, and a provider without the feature is simply not asked.
"""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

from app.learning.curriculum import CURRICULUM_SCHEMA, generate_curriculum
from app.learning.rubric_grading import grade_open
from app.llm.providers import FakeProvider, OpenAICompatProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatResponse, ChatRole, JsonSchema, ModelRole, ToolDef

CURRICULUM = (
    '{"subject_name": "S", "subject_description": "", "topics": [{"name": "T", '
    '"description": "", "kcs": [{"name": "K", "description": "", "requires": []}]}]}'
)


def _stub_openai() -> tuple[OpenAICompatProvider, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> SimpleNamespace:
        sent.append(kwargs)
        message = SimpleNamespace(content="{}", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )

    provider = OpenAICompatProvider(
        name="stub", base_url="http://stub", api_key="", timeout=1.0, max_retries=0
    )
    provider._client = SimpleNamespace(  # ty: ignore[invalid-assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, sent


async def test_the_openai_compatible_provider_asks_for_a_json_object_when_told() -> None:
    provider, sent = _stub_openai()
    messages = [ChatMessage(role=ChatRole.USER, content="go")]

    await provider.complete(model="m", messages=messages, json_output=True)
    await provider.complete(model="m", messages=messages)

    assert sent[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in sent[1]


async def test_the_openai_compatible_provider_sends_a_schema_when_given_one() -> None:
    provider, sent = _stub_openai()
    schema = JsonSchema(name="thing", schema={"type": "object"})

    await provider.complete(
        model="m", messages=[ChatMessage(role=ChatRole.USER, content="go")], json_output=schema
    )

    assert sent[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "thing", "schema": {"type": "object"}, "strict": True},
    }


class _Recording(FakeProvider):
    """A fake that can enforce JSON, and records whether each call asked it to."""

    supports_json_output = True

    def __init__(self, reply: str) -> None:
        super().__init__(reply=reply)
        self.json_requested: list[bool | JsonSchema] = []

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
        json_output: bool | JsonSchema = False,
    ) -> ChatResponse:
        self.json_requested.append(json_output)
        return await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )


def _client(provider: FakeProvider) -> LLMClient:
    roles = {role: ModelSpec(provider="fake", model="fake-1") for role in ModelRole}
    return LLMClient({"fake": provider}, roles)


async def test_a_provider_without_json_enforcement_is_not_asked_for_it() -> None:
    # The plain fake's `complete` takes no such argument: passing it would raise.
    response = await _client(FakeProvider(reply="{}")).complete(
        ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content="go")], json_output=True
    )
    assert response.content == "{}"


async def test_curriculum_design_requires_its_own_shape() -> None:
    """Valid JSON was not enough: a 3B model still left out a topic's concepts or name."""
    provider = _Recording(CURRICULUM)

    proposal, _ = await generate_curriculum(_client(provider), "learn S", None)

    assert proposal is not None
    assert provider.json_requested == [CURRICULUM_SCHEMA]


def test_the_curriculum_schema_requires_what_the_parser_rejects_without() -> None:
    """Every field whose absence fails the parse is required, and can't be empty, in the
    schema the model is held to — so a reply the model is allowed to write is one we accept."""
    root = CURRICULUM_SCHEMA.schema
    topic = root["properties"]["topics"]["items"]
    kc = topic["properties"]["kcs"]["items"]
    assert {"subject_name", "topics"} <= set(root["required"])
    assert {"name", "kcs"} <= set(topic["required"])
    assert "name" in kc["required"]
    assert root["properties"]["topics"]["minItems"] == 1
    assert topic["properties"]["kcs"]["minItems"] == 1
    for name in (root["properties"]["subject_name"], topic["properties"]["name"]):
        assert name["minLength"] == 1
    assert kc["properties"]["name"]["minLength"] == 1


async def test_grading_requires_a_json_reply() -> None:
    provider = _Recording('{"score": 0.8, "rationale": "good"}')

    await grade_open(_client(provider), stem="Q", response={"text": "an answer"}, rubric=None)

    assert provider.json_requested == [True]
