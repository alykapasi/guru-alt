"""The report has to carry the warning, not just the number (S59).

Metrics that are correct and a rendering that buries the caveat is the same failure as getting
the metrics wrong — the reader acts on what the page says. These cover the wiring from a real
replay through to the printed text, because the recurring defect in this codebase is a shared
function covered directly while its call site is covered not at all.
"""

from pathlib import Path

from tests.eval.datasets.calibration import replay
from tests.eval.datasets.synth import synthetic_dataset
from tests.eval.reliability import agreement, metrics
from tests.eval.reliability.report import (
    calibration_from,
    render_agreement,
    render_calibration,
)


def test_a_real_replay_reaches_the_rendered_report() -> None:
    """The wiring test: synthetic sequences -> production estimator replay -> metrics -> text."""
    dataset = synthetic_dataset(n_learners=20, n_items=15, seed=3)

    walked = replay(dataset)
    r = metrics.assess(walked.pairs)
    out = render_calibration(r, source="synthetic")

    assert r is not None and r.n == 300
    assert "Estimator calibration" in out
    assert "skill" in out
    assert "resolution" in out, "the line that stops calibration being read alone"


def test_an_uninformative_estimator_is_called_out_in_words_not_just_in_a_number() -> None:
    """A reader who does not know that a base-rate forecaster scores zero calibration error will
    read a low ECE as success. The report has to say so on the page."""
    pairs = [(0.5, 1.0), (0.5, 0.0)] * 20

    out = render_calibration(metrics.assess(pairs), source="flat")

    assert "NO BETTER THAN THE BASE RATE" in out
    assert "always predicts the base rate" in out


def test_an_informative_estimator_is_not_given_the_warning() -> None:
    pairs = [(0.95, 1.0), (0.05, 0.0)] * 20

    out = render_calibration(metrics.assess(pairs), source="sharp")

    assert "beats the base rate" in out
    assert "NO BETTER THAN THE BASE RATE" not in out


def test_the_report_says_when_it_is_scoring_todays_estimator_on_old_data() -> None:
    """Replaying without the predictions production actually made is a legitimate run and a
    different claim, so the source line has to distinguish them."""
    dataset = synthetic_dataset(n_learners=3, n_items=4, seed=1)
    path = Path("/tmp/guru-reliability-synth.json")
    dataset.to_file(path)
    try:
        r, source = calibration_from(path, binarise_at=None)
    finally:
        path.unlink(missing_ok=True)

    assert r is not None
    assert "scoring today's estimator on old data" in source


def test_a_missing_dataset_says_how_to_build_one_rather_than_failing() -> None:
    r, source = calibration_from(Path("/tmp/does-not-exist-guru.json"), binarise_at=None)

    assert r is None
    assert "build-calibration-dataset" in source
    assert "no scorable points" in render_calibration(r, source=source)


def test_undefined_kappa_is_explained_rather_than_printed_as_a_blank() -> None:
    """Two graders who passed everything. The report must not leave a reader to infer that an
    undefined kappa beside 1.000 raw agreement is good news."""
    a = agreement.compare([(1.0, 1.0)] * 12)

    out = render_agreement(a, against="itself")

    assert "undefined" in out
    assert "no judgement was exercised" in out


def test_the_agreement_report_points_at_kappa_over_raw_agreement() -> None:
    a = agreement.compare([(1.0, 1.0), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0)] * 5)

    out = render_agreement(a, against="a second grader")

    assert "inflated by the base rate" in out
    assert "chance-corrected" in out


def test_binarising_is_stated_on_the_page_because_it_changes_the_question() -> None:
    out = render_calibration(metrics.assess([(0.8, 0.7), (0.8, 0.9)], binarise_at=0.8), source="s")

    assert "binarised at 0.8" in out
