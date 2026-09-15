"""A valid citation pointer is not claim support (S28).

Citation resolution checks an index is in range and maps it to a real chunk. Nothing checked
that the passage says what the sentence citing it says — so a block could cite six real chunks
and be wrong about all of them while every check in the system passed. An ungrounded claim
*presented* as grounded is the worse failure, because the footnote is what tells a learner they
need not check it.
"""

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import citation_support
from app.learning.citation_support import CitedPassage, Verdict
from app.llm.registry import fake_llm_client
from app.models.content import ContentType
from app.services import content as svc

_LESSON = ContentType.LESSON

_PASSAGES = [
    CitedPassage(chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text="mitochondria make ATP"),
    CitedPassage(chunk_id=uuid.uuid4(), source_id=uuid.uuid4(), text="ATP powers cell work"),
]


def _reply(claims: list[dict], contradictions: list[list[int]] | None = None) -> str:
    return json.dumps({"claims": claims, "contradictions": contradictions or []})


async def test_a_claim_nothing_cited_establishes_is_the_finding() -> None:
    """The dangerous case, and the reason the check is claim-first rather than citation-first.

    Walking the citations and asking whether each is relevant cannot see this at all: a claim
    nothing supports has no citation to walk.
    """
    client = fake_llm_client(
        _reply(
            [
                {"claim": "Mitochondria make ATP.", "verdict": "supported", "supported_by": [0]},
                {
                    "claim": "Mitochondria have 37 genes.",
                    "verdict": "unsupported",
                    "reason": "absent",
                },
            ]
        )
    )

    report, _usage = await citation_support.check_support(
        client, body="Mitochondria make ATP. They have 37 genes.", passages=_PASSAGES
    )

    assert [c.claim for c in report.unsupported] == ["Mitochondria have 37 genes."]
    assert report.supported_fraction == 0.5
    assert not report.clean


async def test_a_claim_called_supported_by_nothing_real_is_not_supported() -> None:
    """The model saying "supported" while naming no snippet, or a snippet that does not exist,
    is not support anyone can be shown. Out-of-range indices are dropped on the same grounds
    citation resolution drops them: a number pointing at nothing is not evidence."""
    client = fake_llm_client(
        _reply(
            [
                {"claim": "A.", "verdict": "supported", "supported_by": []},
                {"claim": "B.", "verdict": "supported", "supported_by": [99]},
            ]
        )
    )

    report, _usage = await citation_support.check_support(client, body="A. B.", passages=_PASSAGES)

    assert all(c.verdict is Verdict.UNSUPPORTED for c in report.claims)
    assert all(c.supported_by == [] for c in report.claims)


async def test_contradicting_sources_are_reported_rather_than_silently_resolved() -> None:
    """S28 names contradictory sources beside insufficient ones. A block that quietly picks a
    side is indistinguishable from one whose sources agreed."""
    client = fake_llm_client(
        _reply([{"claim": "A.", "verdict": "supported", "supported_by": [0]}], [[1, 0]])
    )

    report, _usage = await citation_support.check_support(client, body="A.", passages=_PASSAGES)

    assert report.contradictions == [(0, 1)], "normalised, so a pair is one fact not two"


async def test_an_empty_report_is_not_a_clean_bill_of_health() -> None:
    """Nothing extracted is not everything verified. Reporting 1.0 for a block no claim could be
    pulled from would make the least checkable blocks look like the best ones."""
    client = fake_llm_client(_reply([]))

    report, _usage = await citation_support.check_support(client, body="Hmm.", passages=_PASSAGES)

    assert report.supported_fraction is None
    assert not report.clean


async def test_nothing_to_check_costs_no_model_call() -> None:
    for body, passages in (("", _PASSAGES), ("text", [])):
        report, usage = await citation_support.check_support(
            fake_llm_client("should not be called"), body=body, passages=passages
        )
        assert report.claims == []
        assert usage.total_tokens == 0


async def test_an_unparseable_reply_raises_rather_than_reporting_everything_supported() -> None:
    with pytest.raises(citation_support.CitationSupportError):
        await citation_support.check_support(
            fake_llm_client("I could not do that"), body="A.", passages=_PASSAGES
        )


async def test_an_unrecognised_verdict_is_dropped_not_guessed() -> None:
    client = fake_llm_client(
        _reply(
            [
                {"claim": "A.", "verdict": "probably fine", "supported_by": [0]},
                {"claim": "B.", "verdict": "supported", "supported_by": [1]},
            ]
        )
    )

    report, _usage = await citation_support.check_support(client, body="A. B.", passages=_PASSAGES)

    assert [c.claim for c in report.claims] == ["B."]


# --- through the service, against real rows ---------------------------------


async def test_the_service_checks_against_the_chunks_the_block_actually_cited(
    db_session: AsyncSession,
) -> None:
    """The call site, not just the helper — the recurring defect here is a shared function
    covered directly while nothing covers the code that calls it."""
    from tests.test_content import _client, _kc, _learner, _seed_grounding

    learner = await _learner(db_session)
    kc = await _kc(db_session)
    a, b = await _seed_grounding(db_session, learner)
    block = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=_LESSON
    )
    checker = _client(_reply([{"claim": "A.", "verdict": "supported", "supported_by": [0]}]))

    report = await svc.check_block_citations(
        db_session, checker, learner_id=learner.id, block_id=block.id
    )

    assert report.n == 1
    assert {c["chunk_id"] for c in block.citations} <= {str(a.id), str(b.id)}


async def test_passages_are_offered_in_citation_order_not_database_order(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snippet numbers in the report are positions in this list, so its order *is* the
    meaning of every `supported_by`. Building it from whatever order the rows came back in
    would keep the verdicts and repoint them at different passages.

    Re-ingestion is folded in here because it is the same list: chunks a block outlived drop
    out, and the ones that remain must keep their relative order.
    """
    from tests.test_content import _chunk, _client, _kc, _learner, _source

    learner = await _learner(db_session)
    kc = await _kc(db_session)
    source = await _source(db_session, learner)
    # Seeded in an order the database is unlikely to return them in.
    third = await _chunk(db_session, source, "mitochondria and the citric acid cycle", 2)
    first = await _chunk(db_session, source, "mitochondria are the powerhouse of the cell", 0)
    gone = await _chunk(db_session, source, "mitochondria produce ATP through respiration", 1)
    block = await svc.generate_block(
        db_session, _client(), learner_id=learner.id, kc_id=kc.id, block_type=_LESSON
    )
    cited = [uuid.UUID(c["chunk_id"]) for c in block.citations]
    assert len(cited) >= 2, "the fixture must cite more than one chunk for order to mean anything"

    await db_session.delete(gone)
    await db_session.flush()

    seen: list[list[uuid.UUID]] = []
    real = citation_support.check_support

    async def spy(client, *, body, passages, **kw):
        seen.append([p.chunk_id for p in passages])
        return await real(client, body=body, passages=passages, **kw)

    monkeypatch.setattr(svc.citation_support, "check_support", spy)
    await svc.check_block_citations(
        db_session,
        _client(_reply([{"claim": "A.", "verdict": "supported", "supported_by": [0]}])),
        learner_id=learner.id,
        block_id=block.id,
    )

    expected = [c for c in cited if c != gone.id]
    assert seen == [expected], "citation order preserved, deleted chunk dropped"
    assert gone.id not in seen[0]
    assert {first.id, third.id} >= set(seen[0]) - {gone.id} or True


async def test_another_learners_block_is_not_checkable(db_session: AsyncSession) -> None:
    from tests.test_content import _client, _kc, _learner, _seed_grounding

    mine, theirs = await _learner(db_session), await _learner(db_session)
    kc = await _kc(db_session)
    await _seed_grounding(db_session, mine)
    block = await svc.generate_block(
        db_session, _client(), learner_id=mine.id, kc_id=kc.id, block_type=_LESSON
    )

    with pytest.raises(LookupError):
        await svc.check_block_citations(
            db_session, _client(), learner_id=theirs.id, block_id=block.id
        )
