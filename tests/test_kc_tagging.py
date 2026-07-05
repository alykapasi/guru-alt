"""Per-chunk KC auto-tagging (Phase 4b): the pure ``tag_chunk`` classifier, exercised offline.

The FAST model labels a chunk with the KCs it teaches, chosen from a numbered candidate list.
A ``FakeProvider`` returns a canned JSON reply so mapping, thresholding, and robustness against
bad output are all deterministic. The DB-backed scoping + pipeline wiring live in test_ingestion.
"""

import uuid

from app.learning.kc_tagging import KCCandidate, KCTag, tag_chunk
from app.llm import Usage
from app.llm.registry import fake_llm_client


def _candidates(n: int) -> list[KCCandidate]:
    return [
        KCCandidate(id=uuid.uuid4(), name=f"KC {i}", description=f"about topic {i}")
        for i in range(1, n + 1)
    ]


async def test_tag_chunk_maps_model_indices_to_candidate_kcs() -> None:
    candidates = _candidates(3)
    client = fake_llm_client(
        '{"tags": [{"kc": 1, "confidence": 0.9}, {"kc": 3, "confidence": 0.7}]}'
    )

    tags, usage = await tag_chunk(client, "some passage", candidates)

    assert tags == [
        KCTag(kc_id=candidates[0].id, confidence=0.9),
        KCTag(kc_id=candidates[2].id, confidence=0.7),
    ]
    assert usage.input_tokens > 0  # a real model call was made


async def test_tag_chunk_no_candidates_makes_no_model_call() -> None:
    # A sentinel reply that would blow up if parsed proves the model is never consulted.
    tags, usage = await tag_chunk(fake_llm_client("BOOM not json"), "passage", [])
    assert tags == []
    assert usage == Usage()  # no call, no cost


async def test_tag_chunk_drops_tags_below_the_confidence_floor() -> None:
    candidates = _candidates(2)
    client = fake_llm_client(
        '{"tags": [{"kc": 1, "confidence": 0.9}, {"kc": 2, "confidence": 0.2}]}'
    )

    tags, _usage = await tag_chunk(client, "passage", candidates, min_confidence=0.5)

    assert [t.kc_id for t in tags] == [candidates[0].id]  # the 0.2 tag is dropped


async def test_tag_chunk_ignores_out_of_range_indices() -> None:
    candidates = _candidates(2)
    client = fake_llm_client(
        '{"tags": [{"kc": 99, "confidence": 0.9}, {"kc": 1, "confidence": 0.8}]}'
    )

    tags, _usage = await tag_chunk(client, "passage", candidates)

    assert [t.kc_id for t in tags] == [candidates[0].id]  # index 99 (hallucinated) is ignored


async def test_tag_chunk_dedupes_keeping_highest_confidence_in_candidate_order() -> None:
    candidates = _candidates(2)
    client = fake_llm_client(
        '{"tags": [{"kc": 2, "confidence": 0.6}, {"kc": 1, "confidence": 0.7}, '
        '{"kc": 1, "confidence": 0.95}]}'
    )

    tags, _usage = await tag_chunk(client, "passage", candidates)

    assert tags == [  # candidate order (1 then 2); KC 1 keeps its max confidence
        KCTag(kc_id=candidates[0].id, confidence=0.95),
        KCTag(kc_id=candidates[1].id, confidence=0.6),
    ]


async def test_tag_chunk_tolerates_unparseable_reply() -> None:
    # Best-effort enrichment: a malformed reply must not fail ingestion, just yield no tags.
    tags, usage = await tag_chunk(
        fake_llm_client("I think it's about biology!"), "p", _candidates(2)
    )
    assert tags == []
    assert usage.input_tokens > 0  # the call still happened (and its cost is real)


async def test_tag_chunk_empty_tag_list_yields_no_tags() -> None:
    tags, _usage = await tag_chunk(fake_llm_client('{"tags": []}'), "p", _candidates(2))
    assert tags == []
