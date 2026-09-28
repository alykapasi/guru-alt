"""Keeping the shape of a document through chunking (S27).

Normalization collapsed *all* whitespace, so a chunk was one long line no matter what the
document had been. These hold the two cases that cost the most — a table and a code block —
plus the windowing that decides where a chunk ends, and then the limits that are still real,
because a module whose docstring admits a limitation and whose tests never exercise it is
making a promise nobody checks.

The assertions are on the text as stored, not on an intermediate: the flattening happened
before the chunk was written, so the embedding, the retrieval snippet and any lesson written
from it were all working from text the document did not contain.
"""

import uuid
from itertools import pairwise

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.registry import fake_llm_client
from app.models.learner import Learner
from app.models.source import Chunk, SourceKind
from app.rag import extraction_quality as eq
from app.rag.adapters.base import ExtractedUnit
from app.rag.chunking import DEFAULT_OVERLAP, DEFAULT_SIZE, chunk_units, normalize
from app.services import ingestion
from app.storage import InMemoryBlobStore

CODE = """def rate(score, weight):
    if score is None:
        return 0.0
    return score * weight
"""

TABLE = """Region\tQ1\tQ2\tQ3
North\t1200\t1450\t1380
South\t980\t1010\t1120
"""


# --- what survives ----------------------------------------------------------------------------


def test_a_code_block_keeps_its_branch_structure() -> None:
    """The failure this replaces, exactly: `return 0.0` and `return score * weight` came out
    side by side on one line, with nothing left to say that one was inside a branch and the
    other after it. Neither the embedding nor a reader could recover that."""
    out = normalize(CODE)

    assert out.splitlines() == [
        "def rate(score, weight):",
        "    if score is None:",
        "        return 0.0",
        "    return score * weight",
    ]


def test_a_table_keeps_both_its_rows_and_its_columns() -> None:
    out = normalize(TABLE)

    assert out.splitlines() == [
        "Region\tQ1\tQ2\tQ3",
        "North\t1200\t1450\t1380",
        "South\t980\t1010\t1120",
    ]


def test_a_run_of_spaces_still_collapses_because_that_half_was_right() -> None:
    assert normalize("the    quick     brown") == "the quick brown"


def test_a_run_containing_a_tab_becomes_a_tab_not_a_space() -> None:
    """A tab inside a line is a column boundary. Collapsing it left a table as a run of
    numbers even once the rows survived, which is half a fix wearing the whole one's clothes."""
    assert normalize("North \t 1200") == "North\t1200"


def test_trailing_whitespace_goes_but_indentation_stays() -> None:
    assert normalize("    indented   \nplain  ") == "    indented\nplain"


def test_a_run_of_blank_lines_becomes_one() -> None:
    """A PDF's vertical whitespace would otherwise eat the chunk budget."""
    assert normalize("a\n\n\n\n\nb") == "a\n\nb"


def test_nul_bytes_are_still_dropped() -> None:
    """Postgres rejects 0x00 outright, and one stray NUL from a PDF failed the whole chunk
    INSERT and with it the entire ingestion."""
    assert "\x00" not in normalize("clean\x00\x00text\nmore")


def test_leading_and_trailing_blank_lines_go_without_taking_the_first_indent() -> None:
    """`strip()` would have taken the indentation of the first line with it — which on a chunk
    that opens inside a code block is precisely the thing being preserved."""
    assert normalize("\n\n    indented\n\n") == "    indented"


# --- where a chunk ends -----------------------------------------------------------------------


def _one(text: str, **kwargs: int) -> list[str]:
    return [unit.text for unit in chunk_units([ExtractedUnit(text=text)], **kwargs)]


def test_a_window_ends_at_a_blank_line_when_one_is_in_reach() -> None:
    """A blank line is usually a real division in the document, so a chunk that ends there
    ends at something rather than wherever the character count ran out."""
    text = "A" * 60 + "\n\n" + "B" * 60

    pieces = _one(text, size=80, overlap=10)

    assert pieces[0] == "A" * 60


def test_a_window_ends_at_a_line_break_when_no_blank_line_is_in_reach() -> None:
    """The row-and-statement case: without this a chunk stops mid-row, and the half-row at
    each end belongs to neither the row above nor the one below."""
    text = "\n".join("row " + str(n) * 10 for n in range(1, 9))

    pieces = _one(text, size=60, overlap=5)

    assert all(not piece.startswith(" ") for piece in pieces)
    assert pieces[0].splitlines()[-1] == "row " + "4" * 10


def test_a_window_falls_back_to_a_space_when_there_is_no_line_break() -> None:
    pieces = _one(" ".join(["word"] * 40), size=60, overlap=10)

    assert len(pieces) > 1
    assert all(not piece.endswith(" ") for piece in pieces)
    assert all("word" in piece for piece in pieces)


def test_an_unbroken_token_is_cut_hard_rather_than_looping() -> None:
    """Inside a base64 blob there is no boundary to find, so a hard cut is correct — and the
    loop has to make progress anyway or ingestion never finishes."""
    pieces = _one("x" * 500, size=100, overlap=20)

    assert len(pieces) > 1
    assert "".join(pieces).count("x") >= 500


def test_indentation_survives_a_chunk_boundary() -> None:
    """The second chunk of a long indented block opens mid-block, and its first line is still
    indented. Stripping leading whitespace per chunk would have quietly undone the fix for
    every code block longer than one window."""
    block = "\n".join("    line " + str(n) for n in range(200))

    pieces = _one(block, size=DEFAULT_SIZE, overlap=DEFAULT_OVERLAP)

    assert len(pieces) > 1
    assert all(piece.startswith("    ") for piece in pieces[1:])


def test_char_start_still_points_into_the_normalized_text() -> None:
    """Provenance is the reason the offset is stored at all: a citation resolves through it."""
    text = "\n".join(f"line {n}" for n in range(200))
    normalized = normalize(text)

    units = chunk_units([ExtractedUnit(text=text)], size=200, overlap=40)

    for unit in units:
        start = unit.locator["char_start"]
        assert normalized[start : start + 10].lstrip().startswith(unit.text[:5].lstrip())


def test_empty_and_whitespace_only_text_produce_no_chunks() -> None:
    assert _one("") == []
    assert _one("   \n\n  \t \n") == []


@pytest.mark.parametrize(
    "text",
    [
        "\n" + "x" * 5000,
        "x" * 5000 + "\n",
        "a\n" + "x" * 5000,
        "\n".join("x" * 200 for _ in range(50)),
        "\n" * 500,
        " " * 5000,
        "word " * 2000,
    ],
    ids=[
        "newline-first",
        "newline-last",
        "one-early-newline",
        "long-lines",
        "blanks",
        "spaces",
        "words",
    ],
)
def test_chunking_always_terminates_and_never_drops_text(text: str) -> None:
    """Two invariants that a boundary change can break silently. The window start is now pulled
    back to a line boundary, and "back" plus a loop is how a chunker stops making progress —
    a text with one early newline and none after it is the shape that would do it. And a
    dropped span is invisible: ingestion reports success, the document is simply missing a
    piece nobody can retrieve."""
    size, overlap = 200, 40
    normalized = normalize(text)

    units = chunk_units([ExtractedUnit(text=text)], size=size, overlap=overlap)

    if not normalized:
        assert units == []
        return
    starts = [unit.locator["char_start"] for unit in units]
    # Starts at the beginning, advances every time, and reaches the end. Those three together
    # are what "terminates and drops nothing" means for a windowing loop: a stall shows up as a
    # repeated start, and a truncation as a last window that never reaches the final character.
    assert starts[0] == 0
    assert all(later > earlier for earlier, later in pairwise(starts))
    # Slack of one for the boundary character the window strips off its own tail.
    assert starts[-1] + len(units[-1].text) >= len(normalized) - 1
    # And it advances at a sane rate. Progress alone can be one character at a time, which
    # terminates and would still take a 5,000-character document to 5,000 chunks.
    assert len(units) <= 2 * (len(normalized) // (size - overlap) + 2)


def test_the_overlap_does_not_balloon_to_reach_a_distant_line_start() -> None:
    """Pulling the next window back to a line start is bounded, and the bound is load-bearing
    rather than decorative — a mutation that removed it survived every other test here.

    One line break near the very beginning and then a single long line: without the bound the
    second window restarts two characters in, re-reading almost everything the first one
    covered, and every later window pays for it.
    """
    pieces = chunk_units([ExtractedUnit(text="a\n" + "x" * 400)], size=200, overlap=40)

    assert [unit.locator["char_start"] for unit in pieces] == [0, 160, 320]


def test_a_window_start_that_moves_back_still_moves_forward() -> None:
    """The bound on the line-start search, exercised directly: one newline near the beginning
    and nothing after it. Without the floor the next window would restart just past that
    newline every time and the loop would never reach the end."""
    text = "a\n" + "x" * 3000

    pieces = _one(text, size=300, overlap=100)

    assert len(pieces) < 40, "each window has to advance by roughly size minus overlap"
    assert pieces[-1].endswith("x")


# --- the limits this section used to exercise, now lifted (S27) ------------------------------


def test_a_fenced_block_longer_than_a_window_is_no_longer_cut_in_half() -> None:
    """A code block longer than the window used to be cut like anything else, orphaning its
    opening fence in the first chunk. It is now one chunk."""
    fenced = "```\n" + "\n".join(f"    step_{n}()" for n in range(150)) + "\n```"

    [piece] = chunk_units([ExtractedUnit(text=fenced)])

    assert piece.text.count("```") == 2


def test_a_table_chunk_says_it_is_a_table() -> None:
    """Pipe tables only: the tab-separated ``TABLE`` above is still not detected."""
    table = "| region | q1 |\n|---|---|\n" + "\n".join(f"| r{n} | {n} |" for n in range(120))

    units = chunk_units([ExtractedUnit(text=table)])

    assert any(unit.locator.get("structure") == "table" for unit in units)


def test_preserved_structure_does_not_read_as_extraction_damage() -> None:
    """The two halves of S27 have to agree. Indentation and tabs are now kept, and the damage
    indicators must not count them: a well-extracted code block reading as corrupt would be a
    false alarm on exactly the documents this change exists to serve."""
    indicators = eq.measure(normalize(CODE))

    assert indicators.control_chars == 0
    assert indicators.replacement_chars == 0


# --- through the pipeline ---------------------------------------------------------------------


@pytest.mark.parametrize(("name", "text"), [("code", CODE), ("table", TABLE)])
async def test_an_ingested_document_is_stored_with_its_shape(
    db_session: AsyncSession, name: str, text: str
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
        origin=f"{name}.txt",
        content_type="text/plain",
        data=text.encode(),
    )

    await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)

    chunk = await db_session.scalar(select(Chunk).where(Chunk.source_id == source.id))
    assert chunk is not None
    assert "\n" in chunk.text, "the stored chunk is what everything downstream reads"
