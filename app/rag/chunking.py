"""Normalize + window extracted units into embeddable chunks.

Character windows with overlap, snapped to the best boundary available, so a chunk ends where
the document does something rather than wherever the count ran out. Each window keeps its
parent unit's locator plus its char offset, so provenance survives chunking.

**Line structure is preserved, and that is the point of this module (S27).** Normalization used
to collapse *all* whitespace — every newline and every indent — to a single space. A table came
out as a run of numbers with no rows and no columns; a code block came out as one line, so
``return 0.0`` and ``return score * weight`` sat side by side with nothing left to say that one
was inside a branch and the other after it. Nothing downstream could recover either, because
the information was gone before the chunk was stored: the embedding, the retrieval snippet and
the lesson written from it were all working from the flattened text. So runs of spaces *within*
a line still collapse, while line breaks and column tabs do not.

**Leading indentation is kept verbatim, deliberately.** Nothing here can tell a code block from
a paragraph a PDF happened to indent for layout, so the choice is between carrying some layout
noise into prose chunks and destroying the meaning of every code block. Noise can be read past;
structure that was flattened cannot be recovered. Blank lines are capped at one, which is what
stops a PDF's vertical whitespace from eating the chunk budget.

What this still does not do: it has no idea what any of the structure *means*. A markdown table
and a code fence are both just lines to it, a fenced block longer than the window is still cut
in half, and nothing marks a chunk as containing a table so that retrieval could treat it
differently. This keeps the shape; reading the shape is a separate job nobody has done.
"""

import re

from app.rag.adapters.base import ExtractedUnit

DEFAULT_SIZE = 1000  # characters (~250 tokens)
DEFAULT_OVERLAP = 150

# Spaces and tabs, never a newline. Belt and braces rather than the mechanism: `normalize`
# splits on newlines before this ever runs, so within a line `\s+` would behave identically
# today — a mutation proved exactly that. What preserves the lines is the split, and this class
# is what keeps the split from becoming load-bearing on its own.
_INLINE_SPACE = re.compile(r"[^\S\n]+")
_BLANK_RUN = re.compile(r"\n{3,}")
# Postgres rejects 0x00 in text/varchar outright ("invalid byte sequence for encoding UTF8"),
# and it is not whitespace, so a whitespace pass leaves it in place. Extractors do emit it — one
# stray NUL in a PDF failed the whole chunk INSERT and with it the entire ingestion. Drop it
# here, at the one point every adapter's text passes through, rather than per adapter.
_NUL = "\x00"

# A window may be cut short to reach a better boundary only while it still keeps this much of
# the window. Without a floor, a blank line just after the start would produce a sliver of a
# chunk and the next window would re-cover nearly the same text. Arbitrary and bounded: it
# decides where text is cut, and measures nothing.
_MIN_BOUNDARY_FRACTION = 0.5

# In preference order. A blank line is usually a real division in the document; a line break is
# usually a row, a statement or a bullet; a space is the last resort that only avoids splitting
# a word.
_BOUNDARIES = ("\n\n", "\n", " ")


def _collapse(run: re.Match[str]) -> str:
    """One tab if the run contained one, otherwise one space.

    A tab inside a line is a column boundary — a spreadsheet row, a table lifted out of a PDF —
    and collapsing it to a space is what left a table as a run of numbers even once the rows
    survived. A run of plain spaces carries no such claim, so it becomes one space.
    """
    return "\t" if "\t" in run.group() else " "


def normalize(text: str) -> str:
    """Tidy whitespace without flattening the document.

    Within a line: a run of spaces becomes one space, a run containing a tab becomes one tab
    (it is a column boundary), and trailing whitespace goes.
    Across lines: nothing is joined, and a run of blank lines becomes a single blank line.
    Leading indentation survives untouched — see the module docstring for why that is worth
    the layout noise it carries into prose.
    """
    lines = []
    for line in text.replace(_NUL, "").split("\n"):
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        body = _INLINE_SPACE.sub(_collapse, stripped.rstrip())
        lines.append(indent + body if body else "")
    # `strip("\n")` rather than `strip()`: a plain strip would take the first line's indentation
    # with it, which on a chunk that opens inside a code block is the thing being preserved.
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip("\n")


def chunk_units(
    units: list[ExtractedUnit], *, size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[ExtractedUnit]:
    """Subdivide each unit into overlapping windows, preserving + extending its locator."""
    out: list[ExtractedUnit] = []
    for unit in units:
        for start, piece in _windows(normalize(unit.text), size=size, overlap=overlap):
            out.append(
                ExtractedUnit(
                    text=piece,
                    locator={**unit.locator, "char_start": start},
                    method=unit.method,  # keep the unit's extraction method (e.g. OCR)
                )
            )
    return out


def _cut(text: str, start: int, end: int) -> int:
    """Where to end a window that would otherwise stop at ``end``, mid-anything.

    Falls back to ``end`` when nothing better is within reach, which is what happens inside a
    token longer than half a window — a base64 blob, say. A hard cut there is correct: there is
    no boundary to find.
    """
    floor = start + int((end - start) * _MIN_BOUNDARY_FRACTION)
    for boundary in _BOUNDARIES:
        found = text.rfind(boundary, start, end)
        if found > floor:
            return found + len(boundary)
    return end


def _windows(text: str, *, size: int, overlap: int) -> list[tuple[int, str]]:
    if not text:
        return []
    out: list[tuple[int, str]] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:
            end = _cut(text, start, end)
        # Trailing whitespace and the boundary itself go; leading whitespace stays, because on a
        # window that opens inside an indented block it is the indentation.
        piece = text[start:end].strip("\n").rstrip()
        if piece.strip():
            out.append((start, piece))
        if end >= n:
            break
        start = _next_start(text, start, end, overlap)
    return out


def _next_start(text: str, start: int, end: int, overlap: int) -> int:
    """Where the next window begins: back from ``end`` by the overlap, then back to a line start.

    The overlap exists so something spanning a boundary is still retrievable whole from one
    side of it, and it lands mid-line. In prose that is unremarkable — a chunk opening
    mid-sentence is the accepted price. In an indented block it is not: the fragment line
    arrives with its indentation cut off, so it reads as top level when it was three levels
    deep, which is the same lie this module exists to stop, reintroduced one line at a time.

    So the start is pulled back to the beginning of its line when there is one to find, which
    lengthens the overlap and never shortens it. The search floor bounds that: at most one
    extra overlap's worth, and always at least one character past the current start, so a text
    with one early newline and no others cannot stall the loop.
    """
    plain = max(end - overlap, start + 1)
    line_start = text.rfind("\n", max(start + 1, plain - overlap), plain)
    return line_start + 1 if line_start != -1 else plain
