"""A conversational check gets a standard and a level before it is graded (S56)."""

import json

import pytest

from app.learning import difficulty, item_generation
from app.llm.providers import FakeProvider
from app.llm.providers.fake import FakeTurn
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole

CRITERIA = json.dumps({"criteria": ["names both parts", "gives a reason"], "level": "challenging"})


def _client(*replies: str) -> tuple[LLMClient, FakeProvider]:
    provider = FakeProvider(reply="", script=[FakeTurn(text=r) for r in replies])
    specs = {r: ModelSpec(provider="fake", model="fake-1") for r in ModelRole}
    return LLMClient({"fake": provider}, specs), provider


@pytest.mark.parametrize("level", [name for name, _gloss in difficulty.LEVELS])
def test_each_level_has_a_midpoint_inside_its_own_band(level: str) -> None:
    assert difficulty.band(difficulty.midpoint(level)) == level


def test_the_midpoints_are_one_logit_apart() -> None:
    assert [difficulty.midpoint(n) for n, _ in difficulty.LEVELS] == [-2.0, -1.0, 0.0, 1.0, 2.0]


def test_an_unknown_level_has_no_midpoint() -> None:
    with pytest.raises(ValueError):
        difficulty.midpoint("impossible")


async def test_criteria_and_level_are_read_from_the_reply() -> None:
    llm, provider = _client(CRITERIA)
    criteria, level, _ = await item_generation.write_criteria(
        llm, stem="Why is momentum a vector?", component_name="Momentum"
    )
    assert criteria == ["names both parts", "gives a reason"]
    assert level == "challenging"
    [(system, messages)] = provider.prompts_sent
    assert system == item_generation.CRITERIA_SYSTEM_PROMPT
    assert "Why is momentum a vector?" in str(messages[0].content)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("not json", ([], None)),
        (json.dumps({"criteria": ["a"], "level": "impossible"}), (["a"], None)),
        (json.dumps({"criteria": [" ", "b"], "level": " Moderate "}), (["b"], "moderate")),
        (json.dumps({"level": "moderate"}), ([], "moderate")),
    ],
)
async def test_a_partial_reply_keeps_what_it_can(reply: str, expected: tuple) -> None:
    llm, _ = _client(reply)
    criteria, level, _ = await item_generation.write_criteria(llm, stem="Q", component_name="K")
    assert (criteria, level) == expected
