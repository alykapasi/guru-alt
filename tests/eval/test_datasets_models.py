"""Dataset model round-trip (Phase 9b)."""

from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step


def test_calibration_dataset_round_trips_through_file(tmp_path) -> None:
    dataset = CalibrationDataset(
        sequences=[
            ObservationSequence(
                learner_id="L1",
                kc_id="K1",
                steps=[Step(score=1.0, difficulty=0.0), Step(score=0.0, difficulty=1.5)],
            )
        ]
    )
    path = tmp_path / "d.json"
    dataset.to_file(path)
    loaded = CalibrationDataset.from_file(path)
    assert loaded == dataset
    assert loaded.sequences[0].steps[1].difficulty == 1.5
