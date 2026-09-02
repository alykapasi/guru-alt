"""Deterministic synthetic dataset (Phase 9b)."""

from tests.eval.datasets.synth import synthetic_dataset


def test_synthetic_dataset_is_deterministic_and_well_shaped() -> None:
    a = synthetic_dataset(n_learners=4, n_items=10, seed=7)
    b = synthetic_dataset(n_learners=4, n_items=10, seed=7)
    assert a == b  # deterministic from seed
    assert len(a.sequences) == 4
    assert all(len(s.steps) == 10 for s in a.sequences)
    assert all(0.0 <= step.score <= 1.0 for s in a.sequences for step in s.steps)


def test_synthetic_dataset_differs_by_seed() -> None:
    assert synthetic_dataset(n_learners=2, n_items=5, seed=1) != synthetic_dataset(
        n_learners=2, n_items=5, seed=2
    )
