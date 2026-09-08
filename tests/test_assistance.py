"""Evidence credit: how much an assisted attempt is allowed to say about unaided ability."""

import pytest

from app.learning.assistance import evidence_credit


def test_an_unaided_attempt_counts_fully() -> None:
    assert evidence_credit() == 1.0
    assert evidence_credit(hints_used=0, prior_attempts=0) == 1.0


def test_each_scaffold_dilutes_the_evidence() -> None:
    assert evidence_credit(hints_used=1) == pytest.approx(0.5)
    assert evidence_credit(hints_used=2) == pytest.approx(1 / 3)
    assert evidence_credit(prior_attempts=1) == pytest.approx(0.5)


def test_hints_and_repeat_exposure_compound() -> None:
    """Guided practice supplies both at once: a hint, then the same question again."""
    assert evidence_credit(hints_used=1, prior_attempts=1) == pytest.approx(1 / 3)


def test_credit_never_reaches_zero() -> None:
    """A heavily helped attempt is weak evidence, not the absence of evidence."""
    assert evidence_credit(hints_used=50, prior_attempts=50) > 0.0


def test_unreported_or_nonsense_assistance_is_treated_as_none() -> None:
    assert evidence_credit(hints_used=None) == 1.0
    assert evidence_credit(hints_used=-3, prior_attempts=-3) == 1.0
