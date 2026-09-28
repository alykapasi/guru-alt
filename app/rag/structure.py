"""Where a document's structured blocks are (S27) — code, tables, display math.

Chunking kept the shape of a block but cut it wherever the window ran out, so the second half
of a table had no header and a derivation lost its first line. This finds the blocks so the
chunker can keep each one whole, or split it where every part still reads as what it is.

Line-based and deliberately literal: fenced code, pipe tables, ``$$``/``\\[``/``\\begin{…}``
math. The block that opens first wins and nothing nests, so ``$$`` inside a code fence is
code. Tab-separated tables are not detected — telling them from tab-indented prose needs a
heuristic nobody has measured.
"""

import re
from dataclasses import dataclass
from typing import Literal

_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s|:\-]+\|?\s*$")
_ENVS = "equation|align|gather|multline|eqnarray"
_BEGIN = re.compile(rf"^\s*\\begin\{{((?:{_ENVS})\*?)\}}")


@dataclass(frozen=True)
class Segment:
    kind: Literal["prose", "code", "table", "math"]
    text: str
    start: int
    # code: the opening fence line; math: the opening delimiter line.
    open: str | None = None
    # code/math: the closing line — None when the block ran to the end of the text unclosed,
    # so nothing downstream re-wraps a part in a delimiter the document never had.
    close: str | None = None
    # table: the header row, plus the separator row when there is one.
    header: str | None = None


def _math_opening(line: str) -> tuple[str, bool] | None:
    """``(closer, closed_on_this_line)`` if ``line`` opens display math, else None."""
    stripped = line.strip()
    if stripped.startswith("$$"):
        return "$$", "$$" in stripped[2:]
    if stripped.startswith("\\["):
        return "\\]", "\\]" in stripped[2:]
    begin = _BEGIN.match(line)
    if begin:
        closer = f"\\end{{{begin.group(1)}}}"
        return closer, closer in line
    return None


def _block_end(lines: list[str], i: int) -> tuple[Literal["code", "math"], int, bool] | None:
    """If ``lines[i]`` opens a code or math block: its kind, last line index, and whether closed.

    An unclosed block runs to the end of the text.
    """
    fence = _FENCE.match(lines[i])
    if fence:
        marker = fence.group(1)
        j = next(
            (j for j in range(i + 1, len(lines)) if lines[j].lstrip().startswith(marker)), None
        )
        return ("code", j, True) if j is not None else ("code", len(lines) - 1, False)
    math = _math_opening(lines[i])
    if math is None:
        return None
    closer, one_line = math
    if one_line:
        return "math", i, True
    j = next((j for j in range(i + 1, len(lines)) if closer in lines[j]), None)
    return ("math", j, True) if j is not None else ("math", len(lines) - 1, False)


def segment(text: str) -> list[Segment]:
    """Split ``text`` into contiguous prose and block segments that concatenate back to it."""
    # Only "\n" ends a line, as in the chunker: `splitlines` would also break on the form feeds
    # PDFs emit, and the two would disagree about where a block's lines are.
    lines = [line for line in re.split(r"(?<=\n)", text) if line]
    out: list[Segment] = []
    prose: list[str] = []
    prose_start = offset = 0

    def flush_prose() -> None:
        if prose:
            out.append(Segment("prose", "".join(prose), prose_start))
            prose.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        block = _block_end(lines, i)
        if block is not None:
            kind, end, closed = block
            flush_prose()
            body = "".join(lines[i : end + 1])
            opener = line.rstrip("\n")
            closer = lines[end].rstrip("\n") if closed and end > i else None
            out.append(Segment(kind, body, offset, open=opener, close=closer))
            offset += len(body)
            i = end + 1
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) and _TABLE_ROW.match(lines[i + 1]):
            flush_prose()
            j = i
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                j += 1
            body = "".join(lines[i:j])
            header = line.rstrip("\n")
            if _TABLE_SEPARATOR.match(lines[i + 1]):
                header += "\n" + lines[i + 1].rstrip("\n")
            out.append(Segment("table", body, offset, header=header))
            offset += len(body)
            i = j
            continue
        if not prose:
            prose_start = offset
        prose.append(line)
        offset += len(line)
        i += 1
    flush_prose()
    return out
