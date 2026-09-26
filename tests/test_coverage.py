"""Coverage is derived from what the server recorded, never from the model's say-so (S28)."""

import uuid
from datetime import UTC, datetime

from app.rag.coverage import Coverage, cited_source_count, coverage
from app.schemas.chat import MessageRead

_CITE = {"marker": 1, "chunk_id": "c1", "source_id": "s1"}


def _message(**fields: object) -> MessageRead:
    return MessageRead.model_validate(
        {
            "id": uuid.uuid4(),
            "role": "assistant",
            "content": "x",
            "model": None,
            "created_at": datetime.now(UTC),
            **fields,
        }
    )


def test_no_scope_has_no_label() -> None:
    assert coverage(None, []) is None


def test_a_cited_passage_is_cited() -> None:
    assert coverage(3, [_CITE]) is Coverage.CITED


def test_passages_offered_but_not_cited() -> None:
    assert coverage(3, []) is Coverage.RETRIEVED_NOT_CITED


def test_nothing_offered() -> None:
    assert coverage(0, []) is Coverage.NONE


def test_sources_are_counted_once_each() -> None:
    twice = [_CITE, {**_CITE, "marker": 2, "chunk_id": "c2"}, {**_CITE, "source_id": "s2"}]
    assert cited_source_count(twice) == 2


def test_the_message_schema_exposes_the_derived_label() -> None:
    dumped = _message(citations=[_CITE], grounding_count=2).model_dump()
    assert dumped["coverage"] == "cited"
    assert dumped["cited_source_count"] == 1


def test_a_message_from_before_s28_has_no_label() -> None:
    assert _message(citations=[]).model_dump()["coverage"] is None
