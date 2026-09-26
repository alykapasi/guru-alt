"""The one grounding policy (S28): what the tutor is told about its passages, in all four cases."""

import uuid

from app.rag.retrieval import RetrievalHit
from app.services.grounding import format_grounding, instruction, policy_note


def _hit(text: str) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text=text, provenance={}, score=1.0
    )


def test_with_passages_the_tutor_cites_and_names_what_goes_beyond_them() -> None:
    text = instruction(sources_only=False, has_passages=True)
    assert "cite it inline" in text
    assert "general knowledge" in text
    assert "disagree" in text


def test_sources_only_with_passages_forbids_filling_gaps() -> None:
    text = instruction(sources_only=True, has_passages=True)
    assert "only from these passages" in text
    assert "do not answer it from general knowledge" in text
    assert "disagree" in text


def test_with_nothing_retrieved_the_tutor_says_so_then_answers() -> None:
    text = instruction(sources_only=False, has_passages=False)
    assert "had nothing relevant" in text
    assert "answer from general knowledge" in text


def test_sources_only_with_nothing_retrieved_declines_and_points_at_the_switch() -> None:
    text = instruction(sources_only=True, has_passages=False)
    assert "do not cover this" in text
    assert "turning off sources-only" in text
    assert "Do not answer from general knowledge" in text


def test_passages_are_numbered_and_fenced() -> None:
    section = format_grounding([_hit("alpha"), _hit("beta")], sources_only=False)
    assert "[1] alpha" in section and "[2] beta" in section
    assert "RETRIEVED PASSAGES" in section


def test_an_empty_retrieval_still_tells_the_tutor_what_happened() -> None:
    section = format_grounding([], sources_only=True)
    assert section == instruction(sources_only=True, has_passages=False)


def test_the_agent_note_carries_the_same_rule() -> None:
    assert "only from" in policy_note(sources_only=True)
    assert "general knowledge" in policy_note(sources_only=False)


def test_a_sources_only_agent_must_search_before_answering() -> None:
    """The agent decides whether to search; under sources-only, not searching would answer from
    general knowledge unannounced — the one path the policy could otherwise be skipped on."""
    note = policy_note(sources_only=True)
    assert "Before answering, search the learner's materials with search_materials" in note
    assert "Before answering" not in policy_note(sources_only=False)
