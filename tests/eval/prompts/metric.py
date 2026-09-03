"""Exact-set-match metric for kc_tagging, consistent with harness.score_kc_tagging (Phase 9c)."""

from __future__ import annotations


def kc_set_match(example, prediction, trace=None) -> bool:
    """True iff the predicted candidate numbers exactly match the expected set."""
    try:
        predicted = {int(t.kc) for t in prediction.tags}
    except (AttributeError, TypeError, ValueError):
        return False
    return predicted == set(example.expect)
