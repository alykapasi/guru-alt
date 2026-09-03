"""Per-chunk KC auto-tagging (Phase 4b; rewired to the DSPy program in Phase 9c): the pure
``tag_chunk`` classifier, exercised offline.

The FAST model labels a chunk with the KCs it teaches, chosen from a numbered candidate list,
via the kc_tagging DSPy program (app/prompts/kc_tagging_program.py). A ``FakeProvider`` returns
a canned adapter-format reply (field markers + a JSON ``tags`` list) so mapping, thresholding,
and robustness against bad output are all deterministic. The DB-backed scoping + pipeline wiring
live in test_ingestion.
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


async def test_tag_chunk_parses_and_thresholds_via_dspy() -> None:
    reply = (
        '[[ ## tags ## ]]\n[{"kc": 1, "confidence": 0.9}, {"kc": 2, "confidence": 0.3}]'
        "\n\n[[ ## completed ## ]]"
    )
    candidates = _candidates(2)

    tags, usage = await tag_chunk(
        fake_llm_client(reply=reply), "text", candidates, min_confidence=0.5
    )

    assert [t.kc_id for t in tags] == [candidates[0].id]  # 0.3 dropped by threshold
    assert usage.total_tokens > 0


async def test_tag_chunk_no_candidates_makes_no_call() -> None:
    tags, usage = await tag_chunk(fake_llm_client(reply="unused"), "text", [])
    assert tags == [] and usage.total_tokens == 0
    assert usage == Usage()  # no call, no cost


async def test_tag_chunk_garbage_reply_yields_no_tags() -> None:
    tags, usage = await tag_chunk(
        fake_llm_client(reply="not valid adapter output"),
        "text",
        _candidates(1),
    )
    assert tags == []
    assert usage.total_tokens > 0  # D8: a failed parse still accounts usage for cost logging


async def test_tag_chunk_maps_model_indices_to_candidate_kcs() -> None:
    reply = (
        '[[ ## tags ## ]]\n[{"kc": 1, "confidence": 0.9}, {"kc": 3, "confidence": 0.7}]'
        "\n\n[[ ## completed ## ]]"
    )
    candidates = _candidates(3)

    tags, usage = await tag_chunk(fake_llm_client(reply=reply), "some passage", candidates)

    assert tags == [
        KCTag(kc_id=candidates[0].id, confidence=0.9),
        KCTag(kc_id=candidates[2].id, confidence=0.7),
    ]
    assert usage.input_tokens > 0  # a real model call was made


async def test_tag_chunk_ignores_out_of_range_indices() -> None:
    reply = (
        '[[ ## tags ## ]]\n[{"kc": 99, "confidence": 0.9}, {"kc": 1, "confidence": 0.8}]'
        "\n\n[[ ## completed ## ]]"
    )
    candidates = _candidates(2)

    tags, _usage = await tag_chunk(fake_llm_client(reply=reply), "passage", candidates)

    assert [t.kc_id for t in tags] == [candidates[0].id]  # index 99 (hallucinated) is ignored


async def test_tag_chunk_dedupes_keeping_highest_confidence_in_candidate_order() -> None:
    reply = (
        "[[ ## tags ## ]]\n"
        '[{"kc": 2, "confidence": 0.6}, {"kc": 1, "confidence": 0.7}, '
        '{"kc": 1, "confidence": 0.95}]'
        "\n\n[[ ## completed ## ]]"
    )
    candidates = _candidates(2)

    tags, _usage = await tag_chunk(fake_llm_client(reply=reply), "passage", candidates)

    assert tags == [  # candidate order (1 then 2); KC 1 keeps its max confidence
        KCTag(kc_id=candidates[0].id, confidence=0.95),
        KCTag(kc_id=candidates[1].id, confidence=0.6),
    ]


async def test_tag_chunk_empty_tag_list_yields_no_tags() -> None:
    reply = "[[ ## tags ## ]]\n[]\n\n[[ ## completed ## ]]"

    tags, _usage = await tag_chunk(fake_llm_client(reply=reply), "p", _candidates(2))

    assert tags == []
