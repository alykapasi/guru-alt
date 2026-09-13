"""Two graders, and the number that flatters them (S59).

Raw agreement is the figure that looks best and means least. These pin the cases where it and
the chance-corrected figure disagree, because those are the cases a report has to survive.
"""

from tests.eval.reliability import agreement


def test_two_graders_that_always_agree_reach_full_kappa() -> None:
    pairs = [(1.0, 1.0), (0.0, 0.0), (0.5, 0.5), (0.9, 0.9)]

    a = agreement.compare(pairs)

    assert a is not None
    assert a.mean_absolute_difference == 0.0
    assert a.raw_band_agreement == 1.0
    assert a.kappa == 1.0
    assert a.interpretation == "almost perfect"


def test_two_graders_who_pass_everything_agree_completely_and_prove_nothing() -> None:
    """The trap this module exists for.

    Both graders score every answer 1.0. Raw agreement is 100%: by that number they are a
    matched pair of expert judges. They have in fact exercised no judgement at all, and a grader
    returning a constant would score identically — so kappa is undefined rather than perfect,
    and the report has to say so instead of printing 1.0.
    """
    pairs = [(1.0, 1.0)] * 30

    a = agreement.compare(pairs)

    assert a is not None
    assert a.raw_band_agreement == 1.0, "the flattering number"
    assert a.kappa is None, "and the honest one"
    assert a.correlation is None
    assert "undefined" in a.interpretation


def test_graders_agreeing_only_as_often_as_chance_score_about_zero() -> None:
    """Each uses both bands equally, and they coincide half the time — which is exactly what
    two independent coin flips would do."""
    pairs = [(1.0, 1.0), (1.0, 0.0), (0.0, 1.0), (0.0, 0.0)] * 10

    a = agreement.compare(pairs)

    assert a is not None
    assert a.raw_band_agreement == 0.5
    assert a.kappa is not None
    assert abs(a.kappa) < 1e-12
    assert a.interpretation == "slight"


def test_systematic_disagreement_scores_worse_than_chance() -> None:
    """One grader passes what the other fails, every time. Raw agreement is zero and kappa is
    negative — the graders are not merely unreliable, they are inversely related."""
    pairs = [(1.0, 0.0), (0.0, 1.0)] * 10

    a = agreement.compare(pairs)

    assert a is not None
    assert a.raw_band_agreement == 0.0
    assert a.kappa is not None and a.kappa < 0.0
    assert a.interpretation == "worse than chance"


def test_a_lenient_grader_shows_up_in_the_mean_difference_not_the_correlation() -> None:
    """A grader that marks consistently higher still ranks answers identically. Correlation
    cannot see the bias; the mean difference is what catches it, which is why both are on the
    report."""
    pairs = [(0.2, 0.4), (0.5, 0.7), (0.8, 1.0)]

    a = agreement.compare(pairs)

    assert a is not None
    assert abs(a.mean_absolute_difference - 0.2) < 1e-12
    assert a.correlation is not None and a.correlation > 0.99


def test_the_worst_single_disagreement_is_reported_not_just_the_average() -> None:
    """Nineteen close calls and one grader calling an answer perfect that the other failed
    averages to something reassuring."""
    pairs = [(0.5, 0.5)] * 19 + [(1.0, 0.0)]

    a = agreement.compare(pairs)

    assert a is not None
    assert a.mean_absolute_difference == 0.05
    assert a.max_absolute_difference == 1.0


def test_band_edges_are_exclusive_below_so_a_score_sits_above_its_cut() -> None:
    assert agreement.band_of(0.0) == 0
    assert agreement.band_of(0.2) == 0
    assert agreement.band_of(0.21) == 1
    assert agreement.band_of(1.0) == 4


def test_nothing_to_compare_is_none() -> None:
    assert agreement.compare([]) is None
