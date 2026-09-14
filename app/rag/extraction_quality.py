"""Indicators that extraction went wrong — measured, not asserted (S27).

Every chunk was stored with ``"confidence": 1.0``. Nothing computed it, nothing read it, and
it claimed the strongest possible thing: that this text is exactly what the document said. A
scanned page OCR'd into nonsense, a PDF whose ligatures came out as replacement characters, a
table flattened into a run of numbers with no columns — all of them were stored as perfectly
extracted, and there was no number anywhere that could have said otherwise.

**What this is.** Cheap, ground-truth-free signals that text has been *damaged*: characters a
decoder already gave up on, control bytes, the single-letter spray a failed OCR produces, words
with no vowels, tokens hundreds of characters long where the spaces were lost.

**What this is not, and the distinction is the point.** These are indicators of corruption, not
a measurement of fidelity. Finding none does not mean the extraction was correct — a PDF whose
two columns were interleaved line by line produces flawless words in meaningless order and
trips nothing here. So there is deliberately **no single confidence score**: collapsing these
into one number would recreate exactly the thing being removed, a scalar that reads as "how
good is this text" and is nothing of the sort. The indicators are stored as they are, and a
person reads the distribution.

There is also no threshold. Whether an isolated-letter ratio of 0.2 is a ruined page or a
diagram caption is not something anybody here has measured, and guessing it would put an
uncalibrated constant on the path of every chunk — see S18, which exists because that keeps
happening.

**English-leaning, and knowingly.** ``vowelless_word_ratio`` assumes a language where words
have vowels. On a Polish or Czech corpus it would read high on correct text, which is a reason
to read it beside the others rather than a reason to drop it: the product's first cohort reads
English, and the indicator is not used to make any decision on its own.
"""

from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel

REPLACEMENT_CHAR = "�"

# Long enough that no ordinary word reaches it, so this fires on lost spacing rather than on
# German compounds or chemical names. Not a tuned value and not treated as one: it changes
# which tokens are *counted*, and nothing branches on the count.
RUNAWAY_TOKEN_CHARS = 45

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_VOWELS = set("aeiouy")


class ExtractionIndicators(BaseModel):
    """Signs that a piece of extracted text is damaged. No verdict, by design."""

    chars: int
    words: int
    replacement_chars: int
    control_chars: int
    isolated_letter_ratio: float
    vowelless_word_ratio: float
    runaway_token_ratio: float

    @property
    def any_indicator(self) -> bool:
        """Whether anything at all was detected. Still not a verdict — see the module
        docstring: a clean reading here is the absence of evidence, not evidence of absence."""
        return bool(
            self.replacement_chars
            or self.control_chars
            or self.isolated_letter_ratio
            or self.vowelless_word_ratio
            or self.runaway_token_ratio
        )


def _is_control(ch: str) -> bool:
    """A control character that is not ordinary layout. Tab, newline and carriage return are
    how documents are shaped; the rest have no business surviving extraction."""
    return ch not in "\t\n\r" and unicodedata.category(ch) == "Cc"


def measure(text: str) -> ExtractionIndicators:
    """Score one piece of extracted text. Pure, and cheap enough to run on every chunk."""
    words = _WORD.findall(text)
    n_words = len(words)

    isolated = sum(1 for w in words if len(w) == 1)
    # Four characters, so "gym", "why" and "TCP" are not counted as garbled.
    long_enough = [w for w in words if len(w) >= 4]
    vowelless = sum(1 for w in long_enough if not (set(w.lower()) & _VOWELS))
    runaway = sum(1 for w in words if len(w) >= RUNAWAY_TOKEN_CHARS)

    def ratio(count: int, total: int) -> float:
        return count / total if total else 0.0

    return ExtractionIndicators(
        chars=len(text),
        words=n_words,
        replacement_chars=text.count(REPLACEMENT_CHAR),
        control_chars=sum(1 for ch in text if _is_control(ch)),
        isolated_letter_ratio=ratio(isolated, n_words),
        vowelless_word_ratio=ratio(vowelless, len(long_enough)),
        runaway_token_ratio=ratio(runaway, n_words),
    )
