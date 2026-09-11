"""The same book in two shapes is recognised as the same book.

A byte hash answers "is this the same file" and nothing more: an EPUB, a DOCX and a clean PDF
of one edition share almost every word and not one byte. So does a British printing and its
American one. ``text_sha256`` is a digest of the canonical extracted text — container quirks
and dialect folded away — which is what makes those cases equal.

What this deliberately does *not* reach is a scan; OCR errors are per-character, and no amount
of normalising makes a scanned page equal a clean one. That needs a distance, not an equality.
"""

from app.rag.textnorm import canonical, fingerprint


def _same(left: str, right: str) -> bool:
    return fingerprint(left) == fingerprint(right)


# --- the container decided this, not the author -------------------------------------------


def test_a_ligature_and_a_smart_quote_are_not_a_difference() -> None:
    """The main reason the same text out of two extractors does not match byte for byte."""
    assert _same("The “ﬁrst” law of thermodynamics", 'The "first" law of thermodynamics')


def test_a_hyphen_at_a_line_break_is_the_typesetters() -> None:
    """ "under-\\nstand" is one word a different page width would not have split."""
    assert _same("thermo-\ndynamics is conserved", "thermodynamics is conserved")


def test_whitespace_and_case_are_not_a_difference() -> None:
    assert _same("Energy   is\n\nCONSERVED", "energy is conserved")


def test_an_em_dash_and_a_hyphen_are_not_a_difference() -> None:
    assert _same("thermodynamics—energy is conserved", "thermodynamics-energy is conserved")


# --- the dialect decided this ---------------------------------------------------------------


def test_a_british_printing_matches_its_american_one() -> None:
    british = (
        "The colour of the neighbouring metre-long fibre was analysed in the laboratory. "
        "We recognise the behaviour of sulphur and aluminium."
    )
    american = (
        "The color of the neighboring meter-long fiber was analyzed in the laboratory. "
        "We recognize the behavior of sulfur and aluminum."
    )
    assert _same(british, american)


def test_words_that_merely_end_in_our_are_left_alone() -> None:
    """A rule that fires too often is safe — both documents get it — but one that turns
    "hour" into "hor" blurs text that was never a spelling difference."""
    assert "hour of four flours" in canonical("The hour of four flours")


def test_folding_is_consistent_rather_than_correct() -> None:
    """Both sides pass through the same rules, so an over-eager rewrite costs nothing. This
    records the trade rather than pretending it does not happen."""
    assert canonical("surprise") == canonical("surprize")


# --- and this is a different book ------------------------------------------------------------


def test_two_different_texts_do_not_collide() -> None:
    assert not _same(
        "Photosynthesis converts light energy into chemical energy.",
        "Mitosis divides a cell nucleus into two identical nuclei.",
    )


def test_a_scanned_page_is_not_claimed_to_match() -> None:
    """OCR noise is per-character. An equality test cannot reach this, and saying so is the
    reason a distance measure exists alongside it."""
    clean = "Photosynthesis converts light energy into chemical energy."
    scanned = "Photosynthes1s converts l1ght energy into chemica1 energy."
    assert not _same(clean, scanned)


# --- and what that means for a real ingestion -------------------------------------------------

import uuid  # noqa: E402

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.llm.registry import fake_llm_client  # noqa: E402
from app.models.learner import Learner  # noqa: E402
from app.models.source import Chunk, Source, SourceKind, SourceStatus  # noqa: E402
from app.services import ingestion  # noqa: E402
from app.storage import InMemoryBlobStore  # noqa: E402

BRITISH = b"The colour of the neighbouring fibre was analysed. We recognise its behaviour."
AMERICAN = b"The color of the neighboring fiber was analyzed. We recognize its behavior."


async def _ingest(
    session: AsyncSession, store: InMemoryBlobStore, learner: Learner, data: bytes, name: str
) -> Source:
    source = await ingestion.create_source(
        session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin=name,
        content_type="text/plain",
        data=data,
    )
    result = await ingestion.ingest_source(session, store, fake_llm_client(), source.id)
    assert result is not None
    return result


async def _chunks(session: AsyncSession, source_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.source_id == source_id)
        )
    ) or 0


async def test_the_same_book_in_two_dialects_is_embedded_once(db_session: AsyncSession) -> None:
    """Two files with no byte in common, saying the same thing. The second is not chunked, so
    it costs no embeddings and cannot crowd the first out of a grounding window."""
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    first = await _ingest(db_session, store, learner, BRITISH, "uk.txt")
    second = await _ingest(db_session, store, learner, AMERICAN, "us.txt")

    assert first.content_sha256 != second.content_sha256  # not the same file
    assert first.text_sha256 == second.text_sha256  # the same book
    assert await _chunks(db_session, first.id) >= 1
    assert await _chunks(db_session, second.id) == 0
    assert second.meta["duplicate_of"] == str(first.id)
    assert second.status == SourceStatus.DONE  # finished, not failed


async def test_a_different_book_is_still_embedded(db_session: AsyncSession) -> None:
    store = InMemoryBlobStore()
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()

    first = await _ingest(db_session, store, learner, BRITISH, "uk.txt")
    other = await _ingest(db_session, store, learner, b"Mitosis divides a cell nucleus.", "b.txt")

    assert first.text_sha256 != other.text_sha256
    assert await _chunks(db_session, other.id) >= 1
    assert "duplicate_of" not in other.meta


async def test_another_learners_matching_book_is_not_suppressed(
    db_session: AsyncSession,
) -> None:
    """Dedup is within a learner's own library. Suppressing one learner's source because a
    different learner has the same book would leave them with nothing to retrieve."""
    store = InMemoryBlobStore()
    learners = []
    for _ in range(2):
        learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        learners.append(learner)
    await db_session.flush()

    first = await _ingest(db_session, store, learners[0], BRITISH, "uk.txt")
    second = await _ingest(db_session, store, learners[1], BRITISH, "uk.txt")

    assert first.text_sha256 == second.text_sha256
    assert await _chunks(db_session, second.id) >= 1
