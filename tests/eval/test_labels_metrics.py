"""Agreement between a model's recorded decisions and the founder's labels (S59)."""

import pytest

from tests.eval.labels.metrics import MIN_READABLE, categorical


def test_perfect_agreement() -> None:
    result = categorical([("a", "a"), ("b", "b"), ("a", "a")])
    assert result is not None
    assert result.accuracy == 1.0 and result.kappa == pytest.approx(1.0)


def test_a_known_table() -> None:
    # model\\label: a→a 20, a→b 5, b→a 10, b→b 15 (n=50). po=0.7; pe=0.5*0.6+0.5*0.4=0.5 → κ=0.4
    pairs = [("a", "a")] * 20 + [("a", "b")] * 5 + [("b", "a")] * 10 + [("b", "b")] * 15
    result = categorical(pairs)
    assert result is not None
    assert result.n == 50
    assert result.accuracy == pytest.approx(0.7)
    assert result.kappa == pytest.approx(0.4)
    assert result.confusion[("b", "a")] == 10


def test_one_category_everywhere_has_no_kappa() -> None:
    result = categorical([("pass", "pass")] * 5)
    assert result is not None and result.kappa is None


def test_nothing_to_compare() -> None:
    assert categorical([]) is None


def test_the_readable_minimum() -> None:
    assert MIN_READABLE == 20
