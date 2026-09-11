"""Near-duplicates land close; different books land far; nothing is decided by the number.

Equality reaches a re-uploaded file (``content_sha256``) and a different container of the same
clean text (``text_sha256``). Neither reaches a scan: OCR errors are per-character, so a
photographed textbook differs from its own EPUB in thousands of places. A distance does.

What a given distance *means* is not settled here, and ``poe simhash-separation`` shows it may
not be settleable: a badly scanned copy of a book and a document half of which is a different
book sit at the same distance. So these tests assert the ordering — same book nearer than
different book — and that the API reports rather than acts.
"""

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.models.source import Source, SourceKind, SourceStatus
from app.rag.simhash import distance, from_hex, simhash, to_hex
from app.rag.textnorm import canonical
from app.services import ingestion as svc

API = "/api/v1"

BOOK = (
    "Photosynthesis is the process by which green plants transform light energy into "
    "chemical energy. During photosynthesis light energy is captured and used to convert "
    "water, carbon dioxide and minerals into oxygen and energy-rich organic compounds. "
    "It would be impossible to overestimate the importance of photosynthesis in the "
    "maintenance of life on Earth."
)
SCANNED = (
    "Photosynthes1s is the process by which green p1ants transform light energy into "
    "chemical energy. During photosynthesis light energy is captured and used to comvert "
    "water, carbon d10xide and minerals into oxygen and emergy-rich organic compounds. "
    "It would be impossible to 0verestimate the importance of photosynthesis in the "
    "maintenance of 1ife on Earth."
)
OTHER = (
    "Mitosis is a process of cell duplication during which one cell gives rise to two "
    "genetically identical daughter cells. In a typical animal cell mitosis can be divided "
    "into four principal stages: prophase, metaphase, anaphase and telophase. Before "
    "entering mitosis a cell spends most of its life in interphase."
)


def _hash(text: str) -> int:
    return simhash(canonical(text))


# --- the property that matters ---------------------------------------------------------------


def test_a_scan_is_nearer_than_a_different_book() -> None:
    """The whole reason a distance exists alongside the two equality checks."""
    book = _hash(BOOK)
    assert distance(book, _hash(SCANNED)) < distance(book, _hash(OTHER))


def test_identical_text_has_no_distance() -> None:
    assert distance(_hash(BOOK), _hash(BOOK)) == 0


def test_dialect_is_the_normalisers_job_not_this_ones() -> None:
    """A British and an American printing should be *identical*, not merely similar — which
    is why this hashes canonical text rather than raw."""
    british = "The colour of the fibre was analysed in the laboratory."
    american = "The color of the fiber was analyzed in the laboratory."
    assert distance(_hash(british), _hash(american)) == 0


def test_hex_round_trips() -> None:
    value = _hash(BOOK)
    assert from_hex(to_hex(value)) == value
    assert len(to_hex(value)) == 16


def test_text_too_short_to_shingle_still_hashes() -> None:
    """Fewer words than a shingle: the whole thing is the only feature it has."""
    assert _hash("two words") == _hash("two words")
    assert _hash("two words") != _hash("other words")


# --- and what the API does with it -------------------------------------------------------------


async def _source(session: AsyncSession, learner: Learner, text: str, origin: str) -> Source:
    source = Source(
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=origin,
        status=SourceStatus.DONE,
        simhash=to_hex(_hash(text)),
    )
    session.add(source)
    await session.flush()
    return source


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_the_nearest_source_is_reported_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    subject = await _source(db_session, learner, BOOK, "book.pdf")
    scan = await _source(db_session, learner, SCANNED, "scan.pdf")
    await _source(db_session, learner, OTHER, "other.pdf")
    await db_session.commit()

    hits = await svc.similar_sources(db_session, subject)

    assert hits[0].source.id == scan.id
    assert hits[0].distance < hits[1].distance
    assert 0.0 <= hits[0].agreement <= 1.0


async def test_another_learners_sources_are_never_candidates(db_session: AsyncSession) -> None:
    """Cross-learner similarity is not something a learner may observe."""
    mine = await _source(db_session, await _learner(db_session), BOOK, "book.pdf")
    await _source(db_session, await _learner(db_session), SCANNED, "scan.pdf")
    await db_session.commit()

    assert await svc.similar_sources(db_session, mine) == []


async def test_the_endpoint_reports_and_changes_nothing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    from app.api.deps import get_current_learner

    learner = await get_current_learner(db_session)
    subject = await _source(db_session, learner, BOOK, "book.pdf")
    scan = await _source(db_session, learner, SCANNED, "scan.pdf")
    await db_session.commit()

    r = await api_client.get(f"{API}/sources/{subject.id}/similar")

    assert r.status_code == 200
    body = r.json()
    assert body[0]["source"]["id"] == str(scan.id)
    assert body[0]["distance"] > 0  # similar, not identical — the evidence is shown
    # Nothing was suppressed: both sources are still exactly as they were.
    await db_session.refresh(scan)
    assert scan.status == SourceStatus.DONE
    assert "duplicate_of" not in scan.meta
