"""Extraction damage, measured rather than asserted (S27).

Every chunk was stored with ``"confidence": 1.0`` — computed by nothing, read by nothing, and
claiming the strongest possible thing about a scanned page OCR'd into nonsense just as
confidently as about a born-digital paragraph.

The tests that matter most here are the two about what the measurement *cannot* see. A module
whose docstring admits a limitation and whose tests never exercise it is making a promise
nobody checks.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind
from app.rag import extraction_quality as eq
from app.services import ingestion
from app.storage import InMemoryBlobStore
from tests.eval.extraction.report import Row, render

CLEAN = (
    "Photosynthesis converts light energy into chemical energy stored in glucose. "
    "The light-dependent reactions occur in the thylakoid membranes of the chloroplast."
)


# --- what it detects ------------------------------------------------------------------------


def test_ordinary_prose_trips_nothing() -> None:
    indicators = eq.measure(CLEAN)

    assert indicators.any_indicator is False
    assert indicators.words > 10
    assert indicators.replacement_chars == 0


def test_a_decoder_that_already_gave_up_is_counted() -> None:
    """Replacement characters are not ambiguous: something upstream could not decode the bytes
    and said so. Storing that as confidence 1.0 was the clearest case of the old lie."""
    indicators = eq.measure("The coe�cient of friction is �0.3")

    assert indicators.replacement_chars == 2


def test_layout_whitespace_is_not_treated_as_a_control_character() -> None:
    """Tabs and newlines are how documents are shaped. Counting them would make every
    well-extracted table look corrupt, which is the opposite of useful."""
    assert eq.measure("a\tb\nc\r\nd").control_chars == 0
    assert eq.measure("a\x00b\x07c").control_chars == 2


def test_the_single_letter_spray_of_a_failed_ocr_shows_up() -> None:
    indicators = eq.measure("T h e q u i c k b r o w n f o x")

    assert indicators.isolated_letter_ratio == 1.0


def test_garbled_words_without_vowels_show_up() -> None:
    indicators = eq.measure("The rslt ws cmpltly grbld by th scnnr")

    assert indicators.vowelless_word_ratio > 0.5


def test_short_consonant_clusters_are_not_counted_as_garbled() -> None:
    """ "TCP", "gym" and "why" are real words and acronyms. A measure that flagged them would
    read high on any technical corpus and mean nothing."""
    assert eq.measure("Why the TCP gym why TCP").vowelless_word_ratio == 0.0


def test_lost_spacing_shows_up_as_a_runaway_token() -> None:
    indicators = eq.measure("Thequickbrownfoxjumpedoverthelazydogandkeptrunningforever done")

    assert indicators.runaway_token_ratio > 0


def test_empty_text_measures_to_zeros_rather_than_dividing_by_zero() -> None:
    indicators = eq.measure("")

    assert indicators.chars == 0 and indicators.words == 0
    assert indicators.any_indicator is False


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("The coe\ufffdcient of friction", "a replacement character"),
        ("a\x00b control", "a control character"),
        ("T h e q u i c k", "single-letter spray"),
        ("rslt ws cmpltly grbld", "vowelless words"),
        ("Thequickbrownfoxjumpedoverthelazydogandkeptrunningforever", "a runaway token"),
    ],
)
def test_each_indicator_on_its_own_is_enough_to_say_something_was_found(
    text: str, why: str
) -> None:
    """Every signal has to reach `any_indicator` by itself. Leaving one out is invisible: the
    field keeps working for the other four and the fifth silently stops counting — which is
    exactly what a mutation found here, because nothing had asserted it."""
    assert eq.measure(text).any_indicator is True, why


# --- what it cannot see, stated and then executed -------------------------------------------


def test_interleaved_columns_trip_nothing_which_is_the_documented_limit() -> None:
    """A two-column PDF read straight across produces real words in meaningless order. Every
    indicator here is clean, and the text is useless. This is why there is no single score and
    why the report says a clean reading is the absence of evidence."""
    interleaved = "The mitochondrion is In contrast the chloroplast the powerhouse of captures"

    indicators = eq.measure(interleaved)

    assert indicators.any_indicator is False


def test_nothing_produces_a_single_confidence_number() -> None:
    """Collapsing these into one score would recreate exactly what S27 removes: a scalar that
    reads as "how good is this text" and is nothing of the sort."""
    fields = set(eq.ExtractionIndicators.model_fields)

    assert "confidence" not in fields
    assert "score" not in fields
    assert "quality" not in fields


# --- what the pipeline stores ----------------------------------------------------------------


async def test_an_ingested_chunk_carries_measured_indicators_and_no_asserted_confidence(
    db_session: AsyncSession,
) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    store = InMemoryBlobStore()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="notes.txt",
        content_type="text/plain",
        data=CLEAN.encode(),
    )

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    chunks = (await db_session.scalars(select(Chunk).where(Chunk.source_id == source.id))).all()
    assert chunks
    for chunk in chunks:
        assert "confidence" not in chunk.provenance, "the asserted 1.0 is gone"
        measured = chunk.provenance["extraction"]
        assert measured["chars"] == len(chunk.text)
        assert measured["replacement_chars"] == 0


async def test_a_damaged_source_is_stored_as_damaged(db_session: AsyncSession) -> None:
    """The whole point: the same pipeline that used to call this perfect now records what is
    wrong with it."""
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    store = InMemoryBlobStore()
    damaged = ("The rslt ws cmpltly grbld by th sc�nnr and nthng cn b rd frm t nw. " * 8).strip()
    source = await ingestion.create_source(
        db_session,
        store,
        learner_id=learner.id,
        kind=SourceKind.FILE,
        origin="scan.txt",
        content_type="text/plain",
        data=damaged.encode(),
    )

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    chunk = await db_session.scalar(select(Chunk).where(Chunk.source_id == source.id))
    assert chunk is not None
    measured = chunk.provenance["extraction"]
    assert measured["replacement_chars"] > 0
    assert measured["vowelless_word_ratio"] > 0.5


# --- the report ------------------------------------------------------------------------------


def _row(text: str, method: str = "pdf") -> Row:
    return Row(chunk_id=uuid.uuid4().hex, method=method, indicators=eq.measure(text))


def test_the_report_says_it_has_nothing_rather_than_printing_an_empty_table() -> None:
    out = render([])

    assert "no chunks carry indicators" in out


def test_the_report_prints_the_distribution_and_refuses_to_set_a_threshold() -> None:
    out = render([_row(CLEAN), _row("T h e q u i c k"), _row("rslt ws grbld by th sc�nnr")])

    assert "chunks            3" in out
    assert "isolated_letter_ratio" in out
    assert "No threshold is applied and none is set" in out


def test_the_report_states_that_a_clean_reading_proves_nothing() -> None:
    """A number that reads as "97% of chunks are fine" is exactly the misreading this whole
    item exists to stop, so the page has to carry the caveat, not just the module docstring."""
    out = render([_row(CLEAN), _row(CLEAN)])

    assert "no indicator      2 of 2 chunks" in out
    assert "absence of evidence, not evidence of absence" in out


@pytest.mark.parametrize(("fraction", "expected"), [(0.0, 1.0), (0.5, 3.0), (1.0, 5.0)])
def test_percentiles_name_a_value_some_chunk_actually_had(fraction: float, expected: float) -> None:
    from tests.eval.extraction.report import _percentile

    assert _percentile([5.0, 1.0, 3.0, 2.0, 4.0], fraction) == expected
