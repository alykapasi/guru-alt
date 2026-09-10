"""A similarity hash: near-duplicate documents land close together instead of far apart.

Cryptographic hashes are built to avalanche — one changed byte gives an unrelated digest — so
they answer "identical?" and nothing else. That is enough for a re-uploaded file, and enough
for two containers of the same clean text once ``app/rag/textnorm.py`` has folded away what
the container and the dialect decided. It is not enough for a scan: OCR errors are
per-character, so a photographed textbook and its EPUB differ in thousands of places and no
normalisation makes them equal.

SimHash is the other kind. Each overlapping run of words votes on every bit of a 64-bit value,
weighted by how often it occurs; the sign of each column becomes that bit. Change a few
percent of the words and only a few columns flip their sign, so the *distance* between two
hashes tracks how much of the text the documents share.

**This suggests; it does not decide.** How many differing bits means "the same book" depends
on the corpus — scan quality, edition drift, how much front matter an extractor keeps — and
nothing here has been measured against real scanned-versus-digital pairs. So callers surface
candidates and their distances for a person to judge. A wrong threshold then costs a wasted
suggestion instead of a rejected upload, and the number stops being load-bearing.
"""

import hashlib
from collections.abc import Iterator

BITS = 64
SHINGLE_WORDS = 4
"""Words per shingle. Long enough that ordinary sentences do not collide, short enough that one
OCR error spoils only a handful of the document's runs."""


def _shingles(text: str) -> Iterator[str]:
    words = text.split()
    if len(words) < SHINGLE_WORDS:
        # Too short to shingle: the whole thing is the only feature it has.
        if words:
            yield " ".join(words)
        return
    for i in range(len(words) - SHINGLE_WORDS + 1):
        yield " ".join(words[i : i + SHINGLE_WORDS])


def simhash(canonical_text: str) -> int:
    """The 64-bit similarity hash of already-canonical text (see ``textnorm.canonical``).

    Takes canonical text rather than raw so the two fingerprints agree about what a difference
    is: a British and an American printing should not be "similar", they should be identical,
    and that is the normaliser's job rather than this one's.
    """
    counts: dict[str, int] = {}
    for shingle in _shingles(canonical_text):
        counts[shingle] = counts.get(shingle, 0) + 1
    if not counts:
        return 0

    columns = [0] * BITS
    for shingle, weight in counts.items():
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(BITS):
            columns[bit] += weight if digest >> bit & 1 else -weight
    value = 0
    for bit, column in enumerate(columns):
        if column > 0:
            value |= 1 << bit
    return value


def distance(left: int, right: int) -> int:
    """Differing bits — 0 for texts with the same shingle profile, ~32 for unrelated ones."""
    return (left ^ right).bit_count()


def to_hex(value: int) -> str:
    """Storage form: 16 hex characters, unsigned, so no BIGINT sign games."""
    return f"{value:016x}"


def from_hex(value: str) -> int:
    return int(value, 16)
