"""Deciding whether a new memory is the same as, updates, or sits beside an existing one (S42)."""

import json
from typing import cast

from app.agent.untrusted import INSTRUCTION
from app.llm import LLMClient, ModelRole
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.registry import ModelSpec, fake_llm_client
from app.memory import supersession as sup

CANDIDATES = [
    sup.Candidate("Studies in the evenings now.", ["Studies in the mornings."]),
    sup.Candidate("Likes chess.", ["Likes football.", "Plays the piano."]),
    sup.Candidate("Is revising for the physics exam.", ["Is revising for the physics exam!"]),
]


def _reply(verdicts: list[dict]) -> str:
    return json.dumps({"verdicts": verdicts})


async def test_each_verdict_is_read() -> None:
    llm = fake_llm_client(
        _reply(
            [
                {"candidate": 1, "verdict": "updates", "replaces": 1},
                {"candidate": 2, "verdict": "coexists"},
                {"candidate": 3, "verdict": "same"},
            ]
        )
    )
    got, _usage = await sup.judge(llm, CANDIDATES)
    assert got == [sup.Judgement("updates", 0), sup.COEXISTS, sup.Judgement("same")]


async def test_anything_unusable_coexists() -> None:
    llm = fake_llm_client(
        _reply(
            [
                {"candidate": 1, "verdict": "updates", "replaces": 5},  # no such neighbour
                {"candidate": 2, "verdict": "merges"},  # no such verdict
                {"candidate": 9, "verdict": "same"},  # no such candidate
                {"candidate": 1, "verdict": "same"},  # a second answer for candidate 1: ignored
            ]
        )
    )
    got, _ = await sup.judge(llm, CANDIDATES)
    assert got == [sup.COEXISTS, sup.COEXISTS, sup.COEXISTS]


class _Down(FakeProvider):
    async def complete(self, *, model, messages, system=None, max_tokens=1024, tools=None):
        raise RuntimeError("provider down")


async def test_a_bad_reply_or_a_failed_call_coexists() -> None:
    got, _ = await sup.judge(fake_llm_client("not json"), CANDIDATES)
    assert got == [sup.COEXISTS] * 3

    got, _ = await sup.judge(
        LLMClient({"fake": _Down()}, {r: ModelSpec("fake", "fake-1") for r in ModelRole}),
        CANDIDATES,
    )
    assert got == [sup.COEXISTS] * 3


async def test_nothing_to_judge_makes_no_call() -> None:
    llm = fake_llm_client(script=[FakeTurn(text="should not be read")])
    got, usage = await sup.judge(llm, [])
    assert got == [] and usage.total_tokens == 0


async def test_existing_memories_are_fenced_as_untrusted() -> None:
    llm = fake_llm_client(_reply([]))
    await sup.judge(llm, CANDIDATES)
    system, messages = cast(FakeProvider, llm._providers["fake"]).prompts_sent[0]
    prompt = str(messages[0].content)
    assert "Studies in the mornings." in prompt
    assert INSTRUCTION in (system or "") or INSTRUCTION in prompt
