"""A canonical form for extracted text, so the same book in two shapes hashes the same way.

A byte hash answers "is this the same file". It cannot answer "is this the same book", because
an EPUB, a DOCX and a clean PDF of one edition share almost every word and no bytes at all.
This module strips away what the *container* decided — ligatures, smart quotes, line-break
hyphenation, whitespace, case — and what the *dialect* decided, leaving text that two editions
of the same work agree on.

**Correctness is not the goal; agreement is.** Both documents pass through the same rules, so
an over-eager rule that turns "surprise" into "surprize" costs nothing — both sides get it.
The only rule that would hurt is one mapping a British spelling and its American counterpart
to *different* forms, or one collapsing genuinely different words together. That is why the
suffix families here are broad and the exception lists are short: a rule that fires too often
is safe, and a rule that fires inconsistently is not.

What this deliberately does not reach is a scan. OCR errors are per-character, so a scanned
textbook and its EPUB will not produce the same canonical text however it is normalised —
that needs a distance, not an equality, which is ``app/rag/simhash.py``'s job.
"""

import hashlib
import re
import unicodedata

# A hyphen before a line break is the typesetter's, not the author's: "under-\nstand" is one
# word that a different page width would not have split.
# The escapes are U+2010 HYPHEN and U+2011 NON-BREAKING HYPHEN, written as escapes
# because a bare one is indistinguishable from an ASCII "-" in a diff.
_LINEBREAK_HYPHEN = re.compile("[-\u2010\u2011]\\s*\\n\\s*")
_WHITESPACE = re.compile(r"\s+")
_NOT_WORDISH = re.compile(r"[^a-z0-9 ]+")

# Words ending in "our" that are not the British "-our/-or" family. Short by design: a wrong
# rewrite is harmless when both documents get it, so this only needs the words common enough
# that collapsing them into an existing "-or" word would blur real text.
_OUR_KEEP = frozenset(
    {"four", "your", "pour", "tour", "hour", "sour", "flour", "our", "devour", "contour"}
)
# "-our" carries suffixes: neighbouring, colours, favoured, humourless.
_OUR = re.compile(r"\b([a-z]+)our(s|ing|ed|er|ers|ly|less|hood|able|ite)?\b")
_ISE = re.compile(r"([a-z]{3,})is(e|es|ed|ing|ation|ations)\b")
# Its own family, not a case of "-ise": analyse, paralyse, catalyse. "analysis" is
# spelled the same either side and is deliberately not matched.
_YSE = re.compile(r"([a-z]{2,})ys(e|es|ed|ing)\b")
_RE_ENDING = re.compile(r"\b([a-z]+[bcdgkmnptv])re\b")
_OGUE = re.compile(r"\b([a-z]+)ogue\b")
_DOUBLE_L = re.compile(r"\b([a-z]+)ll(ed|ing|er|ers|or|ors)\b")

# Pairs no suffix rule reaches. Chemistry and units earn their place in a textbook corpus.
_WORDS = {
    "aluminium": "aluminum",
    "sulphur": "sulfur",
    "sulphate": "sulfate",
    "sulphide": "sulfide",
    "sulphuric": "sulfuric",
    "caesium": "cesium",
    "defence": "defense",
    "offence": "offense",
    "licence": "license",
    "practise": "practice",
    "programme": "program",
    "grey": "gray",
    "maths": "math",
    "artefact": "artifact",
    "draught": "draft",
    "plough": "plow",
    "storey": "story",
    "tyre": "tire",
    "kerb": "curb",
    "metre": "meter",
    "litre": "liter",
    "fibre": "fiber",
    "centre": "center",
}
_WORD = re.compile(r"\b[a-z]+\b")


def _fold_our(match: re.Match[str]) -> str:
    """ "colour" -> "color", but "hour" and "flour" are left alone."""
    stem, suffix = match.group(1), match.group(2) or ""
    if f"{stem}our" in _OUR_KEEP:
        return match.group(0)
    return f"{stem}or{suffix}"


def _dialect(text: str) -> str:
    """Fold British spellings onto their American counterparts (either direction would do)."""
    text = _WORD.sub(lambda m: _WORDS.get(m.group(0), m.group(0)), text)
    text = _OUR.sub(_fold_our, text)
    text = _ISE.sub(lambda m: f"{m.group(1)}iz{m.group(2)}", text)
    text = _YSE.sub(lambda m: f"{m.group(1)}yz{m.group(2)}", text)
    text = _RE_ENDING.sub(r"\1er", text)
    text = _OGUE.sub(r"\1og", text)
    return _DOUBLE_L.sub(r"\1l\2", text)


def canonical(text: str) -> str:
    """The comparison form: container quirks and dialect removed, words and digits kept.

    NFKC first, which is what folds a ligature back into its letters and a typographic quote
    into an apostrophe — differences no reader would call a difference, and the main reason
    the same text out of two extractors does not match byte for byte.
    """
    text = unicodedata.normalize("NFKC", text)
    text = _LINEBREAK_HYPHEN.sub("", text)
    text = text.casefold()
    text = _NOT_WORDISH.sub(" ", text)
    text = _dialect(text)
    return _WHITESPACE.sub(" ", text).strip()


def digest(canonical_text: str) -> str:
    """SHA-256 of already-canonical text — equal for two files that say the same thing."""
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def fingerprint(text: str) -> str:
    """:func:`canonical` then :func:`digest`, for callers with only the raw text."""
    return digest(canonical(text))
