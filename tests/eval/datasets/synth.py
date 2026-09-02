"""Deterministic synthetic learner sequences — the CI oracle for calibration (Phase 9b).

Each learner has a hidden true ability; each item a difficulty. An item's score is the true
expected score sigmoid(true_ability - difficulty), so a correct estimator must converge to that
ability and calibrate to within tolerance. Fully reproducible from `seed`.
"""

from __future__ import annotations

import math
import random

from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step


def _sigmoid(x: float) -> float:
    if x >= 0.0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def synthetic_dataset(*, n_learners: int, n_items: int, seed: int) -> CalibrationDataset:
    """A well-calibrated synthetic dataset: score = sigmoid(true_ability - difficulty)."""
    rng = random.Random(seed)
    sequences: list[ObservationSequence] = []
    for learner in range(n_learners):
        true_ability = rng.uniform(-2.0, 2.0)
        steps: list[Step] = []
        for _ in range(n_items):
            difficulty = rng.uniform(-2.0, 2.0)
            steps.append(Step(score=_sigmoid(true_ability - difficulty), difficulty=difficulty))
        sequences.append(ObservationSequence(learner_id=f"L{learner}", kc_id="K0", steps=steps))
    return CalibrationDataset(sequences=sequences)
