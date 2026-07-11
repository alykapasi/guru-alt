"""Placement inference: parse the LLM's per-KC starting-level reply, tolerantly."""

import json
import uuid

from app.learning.placement_inference import KCCandidate, infer_levels
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole


def _client_with_reply(reply: str) -> LLMClient:
    fake = FakeProvider(reply=reply)
    return LLMClient({"fake": fake}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


def _candidates(n: int = 2) -> list[KCCandidate]:
    return [KCCandidate(id=uuid.uuid4(), name=f"KC {i}") for i in range(1, n + 1)]


async def test_infer_levels_maps_indices_to_candidates() -> None:
    candidates = _candidates(2)
    reply = json.dumps({"levels": [{"kc": 2, "level": "strong", "confidence": 0.9}]})
    levels, usage = await infer_levels(
        _client_with_reply(reply), "I've done a lot of X", candidates
    )

    assert len(levels) == 1
    assert levels[0].kc_id == candidates[1].id
    assert levels[0].level == "strong"
    assert levels[0].confidence == 0.9
    assert usage.output_tokens > 0


async def test_infer_levels_no_candidates_makes_no_call() -> None:
    levels, usage = await infer_levels(_client_with_reply("irrelevant"), "background", [])
    assert levels == []
    assert usage.output_tokens == 0


async def test_infer_levels_tolerates_unparseable_reply() -> None:
    levels, _ = await infer_levels(_client_with_reply("not json"), "background", _candidates())
    assert levels == []


async def test_infer_levels_ignores_out_of_range_index() -> None:
    reply = json.dumps({"levels": [{"kc": 99, "level": "some", "confidence": 0.8}]})
    levels, _ = await infer_levels(_client_with_reply(reply), "background", _candidates(2))
    assert levels == []


async def test_infer_levels_ignores_invalid_level_string() -> None:
    reply = json.dumps({"levels": [{"kc": 1, "level": "expert", "confidence": 0.8}]})
    levels, _ = await infer_levels(_client_with_reply(reply), "background", _candidates(2))
    assert levels == []


async def test_infer_levels_dedupes_keeping_highest_confidence() -> None:
    candidates = _candidates(1)
    reply = json.dumps(
        {
            "levels": [
                {"kc": 1, "level": "some", "confidence": 0.3},
                {"kc": 1, "level": "strong", "confidence": 0.9},
            ]
        }
    )
    levels, _ = await infer_levels(_client_with_reply(reply), "background", candidates)
    assert len(levels) == 1
    assert levels[0].level == "strong"
    assert levels[0].confidence == 0.9
