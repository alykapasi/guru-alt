"""How much of a reply the learner's sources carried, as far as the server can know (S28).

Derived only from what was recorded — how many passages were offered and which markers the
reply actually cited — so the label cannot be talked into anything by the model. "Partly from
your sources" is deliberately absent: telling partial from full coverage is a judgement about
the text, and the tutor's own sentence carries it.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum


class Coverage(StrEnum):
    CITED = "cited"
    RETRIEVED_NOT_CITED = "retrieved_not_cited"
    NONE = "none"


def coverage(grounding_count: int | None, citations: Sequence[Mapping]) -> Coverage | None:
    """``None`` when there was no library scope, or the row predates the count."""
    if grounding_count is None:
        return None
    if citations:
        return Coverage.CITED
    return Coverage.RETRIEVED_NOT_CITED if grounding_count > 0 else Coverage.NONE


def cited_source_count(citations: Sequence[Mapping]) -> int:
    """Distinct sources cited — two passages from one book are one source to the learner."""
    return len({c["source_id"] for c in citations if "source_id" in c})
