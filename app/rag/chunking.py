"""Normalize + window extracted units into embeddable chunks.

Character windows with overlap, snapped to whitespace so words aren't split. Each window
keeps its parent unit's locator plus its char offset, so provenance survives chunking.
"""

import re

from app.rag.adapters.base import ExtractedUnit

DEFAULT_SIZE = 1000  # characters (~250 tokens)
DEFAULT_OVERLAP = 150

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Collapse runs of whitespace to single spaces and trim."""
    return _WHITESPACE.sub(" ", text).strip()


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


def _windows(text: str, *, size: int, overlap: int) -> list[tuple[int, str]]:
    if not text:
        return []
    out: list[tuple[int, str]] = []
    n = len(text)
    start = 0
    while start < n:
        end = min(start + size, n)
        if end < n:  # snap back to a word boundary
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            out.append((start, piece))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out
