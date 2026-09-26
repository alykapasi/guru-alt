"""Machine-read passages say so — to the tutor and to the learner (S27)."""

import uuid

from app.rag.extraction_quality import reading_note
from app.rag.retrieval import RetrievalHit
from app.schemas.source import ChunkRead
from app.services.grounding import READING_NOTE_RULE, format_grounding

CLEAN = {"replacement_chars": 0, "control_chars": 0}


def _hit(text: str, provenance: dict) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text=text, provenance=provenance, score=1.0
    )


def test_ocr_and_asr_are_named() -> None:
    assert (
        reading_note({"method": "ocr"}) == "read from a scan or image; wording may contain errors"
    )
    assert reading_note({"method": "asr"}) == "transcribed from audio; wording may contain errors"


def test_undecodable_characters_are_named() -> None:
    note = reading_note({"method": "text", "extraction": {**CLEAN, "replacement_chars": 3}})
    assert note == "some characters could not be read"
    assert reading_note({"extraction": {**CLEAN, "control_chars": 1}}) == note


def test_several_facts_are_joined() -> None:
    note = reading_note({"method": "ocr", "extraction": {**CLEAN, "replacement_chars": 1}})
    assert note == (
        "read from a scan or image; wording may contain errors; some characters could not be read"
    )


def test_clean_born_digital_text_has_no_note() -> None:
    assert reading_note({"method": "text", "extraction": CLEAN}) is None
    assert reading_note({}) is None


def test_a_noted_passage_is_marked_and_the_rule_is_added() -> None:
    section = format_grounding(
        [_hit("clean words", {"method": "text"}), _hit("scanned words", {"method": "ocr"})],
        sources_only=False,
    )
    assert "[1] clean words" in section
    assert "[2] (read from a scan or image; wording may contain errors) scanned words" in section
    assert READING_NOTE_RULE in section


def test_no_rule_when_nothing_is_noted() -> None:
    section = format_grounding([_hit("clean", {"method": "text"})], sources_only=False)
    assert READING_NOTE_RULE not in section


def test_the_citation_api_carries_the_note() -> None:
    read = ChunkRead(id=uuid.uuid4(), ordinal=0, text="x", provenance={"method": "asr"})
    assert read.model_dump()["reading_note"] == (
        "transcribed from audio; wording may contain errors"
    )


def test_a_form_feed_page_break_is_layout_not_damage() -> None:
    """Plain-text documents break pages with a form feed; that is not a character the reader
    failed to decode, and saying so would be a false note."""
    from app.rag.chunking import normalize
    from app.rag.extraction_quality import measure

    indicators = measure(normalize("end of page one.\n\x0cChapter 2\x0b"))
    assert indicators.control_chars == 0
