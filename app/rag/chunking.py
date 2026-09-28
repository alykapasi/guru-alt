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

**A block longer than a window is no longer cut in half (S27).** ``app.rag.structure`` finds
fenced code, pipe tables and display math. One that fits a window stays in the prose stream,
read with the sentence that introduces it. A longer one — the case that used to lose its header
or its first line — becomes one chunk up to ``BLOCK_CAP_FACTOR`` windows, and beyond that is
split at line breaks into parts that each repeat the table header, the fence, or the math
delimiters. Its locator says ``structure`` and, when split, ``part``. Tab-separated tables are
still not detected.
"""

import re

from app.rag.adapters.base import ExtractedUnit
from app.rag.structure import Segment, segment

DEFAULT_SIZE = 1000  # characters (~250 tokens)
DEFAULT_OVERLAP = 150
# How far past one window a structured block may run and still be kept as a single chunk.
BLOCK_CAP_FACTOR = 4

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
    """Just the tabs if the run had any, otherwise one space.

    A tab inside a line is a column boundary — a spreadsheet row, a table lifted out of a
    document — and collapsing it to a space is what left a table as a run of numbers even once
    the rows survived. The count is kept rather than reduced to one, because consecutive tabs
    are *empty cells*: squeezing them would shift every column after the gap and quietly
    misalign the row, which is the same loss in a subtler form. Surrounding spaces go; a run of
    plain spaces carries no such claim and becomes one space.
    """
    tabs = run.group().count("\t")
    return "\t" * tabs if tabs else " "


def normalize(text: str) -> str:
    """Tidy whitespace without flattening the document.

    Within a line: a run of spaces becomes one space, a run containing tabs keeps just those
    tabs (they are column boundaries, and consecutive ones are empty cells), and trailing
    whitespace goes.
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
    """Subdivide each unit into chunks, preserving + extending its locator.

    Prose is windowed with overlap. A structured block longer than a window (S27) is one chunk
    up to ``BLOCK_CAP_FACTOR * size`` and beyond that is split into self-contained parts.
    """
    cap = BLOCK_CAP_FACTOR * size
    out: list[ExtractedUnit] = []
    for unit in units:
        for seg in _runs(segment(normalize(unit.text)), size):
            if seg.kind == "prose":
                pieces = [
                    (seg.start + start, piece, {})
                    for start, piece in _windows(seg.text, size=size, overlap=overlap)
                ]
            else:
                pieces = _block_pieces(seg, cap)
            for start, piece, extra in pieces:
                out.append(
                    ExtractedUnit(
                        text=piece,
                        locator={**unit.locator, "char_start": start, **extra},
                        method=unit.method,  # keep the unit's extraction method (e.g. OCR)
                    )
                )
    return out


def _runs(segments: list[Segment], size: int) -> list[Segment]:
    """Fold every block that fits a window back into the prose around it.

    Only a block longer than a window is at risk of being cut, so only that one leaves the
    prose stream. A short equation or a three-row table pulled out on its own would be a chunk
    too small to retrieve on and separated from the sentence that says what it is.
    """
    out: list[Segment] = []
    for seg in segments:
        if seg.kind != "prose" and len(seg.text.strip("\n")) > size:
            out.append(seg)
        elif out and out[-1].kind == "prose":
            out[-1] = Segment("prose", out[-1].text + seg.text, out[-1].start)
        else:
            out.append(Segment("prose", seg.text, seg.start))
    return out


def _block_pieces(seg: Segment, cap: int) -> list[tuple[int, str, dict]]:
    """A block as one piece, or as self-contained parts when it is longer than ``cap``."""
    text = seg.text.strip("\n")
    if len(text) <= cap:
        return [(seg.start, text, {"structure": seg.kind})]
    head, tail, body = _frame(seg, text)
    room = cap - len(head) - len(tail)
    if room < cap // 2:  # a frame that would crowd out the content is not worth repeating
        head, tail, body, room = "", "", text.split("\n"), cap
    # Each line with its offset; a line longer than a part is cut hard, never dropped.
    pieces: list[tuple[int, str]] = []
    offset = seg.start + len(head)
    for line in body:
        for at in range(0, max(len(line), 1), room):
            pieces.append((offset + at, line[at : at + room]))
        offset += len(line) + 1
    parts: list[list[tuple[int, str]]] = [[]]
    used = 0
    for piece in pieces:
        cost = len(piece[1]) + (1 if parts[-1] else 0)
        if parts[-1] and used + cost > room:
            parts.append([])
            cost = len(piece[1])
            used = 0
        parts[-1].append(piece)
        used += cost
    n = len(parts)
    return [
        (
            part[0][0],
            head + "\n".join(line for _, line in part) + tail,
            {"structure": seg.kind, "part": f"{i}/{n}"},
        )
        for i, part in enumerate(parts, start=1)
    ]


def _frame(seg: Segment, text: str) -> tuple[str, str, list[str]]:
    """The context every part repeats, and the lines between it.

    An unclosed code or math block has no closing line to repeat, so its parts carry only the
    opening one — a closing delimiter the document never had would misstate where it ends.
    """
    lines = text.split("\n")
    if seg.kind == "table" and seg.header:
        header_lines = seg.header.count("\n") + 1
        return seg.header + "\n", "", lines[header_lines:]
    if seg.kind in ("code", "math") and seg.open is not None and len(lines) >= 2:
        if seg.close is not None:
            return seg.open + "\n", "\n" + seg.close, lines[1:-1]
        return seg.open + "\n", "", lines[1:]
    return "", "", lines


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
