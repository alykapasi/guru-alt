# Phase 9b — Real-data Mining + Tracer-Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mine the `LearningEvent` log into per-`(learner, KC)` observation sequences and score the tracer's forward model with prequential (predict-then-update) replay — a label-free eval of the core IRT/Elo engine, plus the reusable real-data mining pipeline.

**Architecture:** A new `tests/eval/datasets/` subpackage (mirrors `tests/eval/sweep/`), purely additive. A deterministic synthetic generator is the CI oracle; the real miner is a manual `poe` step writing a gitignored dataset. The scorer reuses `app.learning.tracer`'s production estimator (`expected`/`update`), so the eval can't drift from what ships — zero app-code changes.

**Tech Stack:** Python 3.13, Pydantic v2, SQLAlchemy 2 async, pytest (asyncio_mode=auto), the existing `app.learning.tracer` estimator. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-30-real-data-datasets-design.md`.

## Global Constraints

- **Python ≥ 3.13.** ruff line-length 100 (E501 ignored); `uv run poe check` (lint + type-check + test) must end green.
- **All new code lives under `tests/eval/datasets/`** — a namespace package (NO `__init__.py`, exactly like `tests/eval/sweep/`). `__main__.py` IS required (it is the `python -m` entry, not a package marker).
- **Imports are absolute** — `from tests.eval.datasets.<mod> import …`, `from app.learning.tracer import …`, `from app.models… import …`. Resolves under both pytest and `python -m tests.eval.datasets`.
- **Non-test modules must NOT match `test_*.py`** (pytest would collect them). Test files are `tests/eval/test_datasets_*.py`.
- **Zero `app/` runtime changes.** The scorer reuses `app.learning.tracer` (`GlickoEstimator`, `Estimate`, `MasteryEstimator`) as-is. Do not modify anything under `app/`.
- **Mined datasets are gitignored** — add `tests/eval/datasets/*.json` to `.gitignore`; no learner data is ever committed (D3).
- **pytest `asyncio_mode=auto`** — async tests need no decorator; DB tests take the `db_session` fixture from `tests/conftest.py` (transactional, rolled back).
- **No new dependencies.** MAE/RMSE/reliability are pure-Python arithmetic; the synthetic generator uses stdlib `random` seeded for determinism.
- **Verified facts:** `GlickoEstimator.expected(prior: Estimate, *, difficulty: float) -> float` returns `sigmoid(θ − d)`; `update(prior, *, score, difficulty, weight=1.0) -> Estimate`; `Estimate(ability=0.0, uncertainty=1.0)` is frozen. Observation events store `payload = {"score", "difficulty", …}`. `LearningEvent.created_at` is naive-UTC (use naive datetimes in tests). `Learner(handle=…)`, `Subject(slug=,name=)`, `Topic(subject_id=,slug=,name=)`, `KC(topic_id=,slug=,name=)`.
- **Git discipline:** one commit per task on top of the current HEAD; stage only that task's files (never `git add -A`); keep `.claude/settings.json` untracked; do not `reset`/`amend`/`rebase` existing commits.

## File Structure

**Created (new):**
- `tests/eval/datasets/models.py` — `Step`, `ObservationSequence`, `CalibrationDataset` (+ `to_file`/`from_file`).
- `tests/eval/datasets/synth.py` — `synthetic_dataset` (deterministic CI oracle).
- `tests/eval/datasets/calibration.py` — `ReliabilityBucket`, `CalibrationReport`, `score_tracer_calibration` (prequential).
- `tests/eval/datasets/mine.py` — `mine_observation_sequences` (DB → sequences).
- `tests/eval/datasets/__main__.py` — `poe build-calibration-dataset` CLI.
- Tests: `tests/eval/test_datasets_models.py`, `test_datasets_synth.py`, `test_datasets_calibration.py`, `test_datasets_mine.py`, `test_datasets_gitignore.py`.

**Modified:**
- `.gitignore` — add the mined-dataset rule.
- `pyproject.toml` — add the `build-calibration-dataset` poe task.

---

### Task 1: Dataset models

**Files:**
- Create: `tests/eval/datasets/models.py`
- Test: `tests/eval/test_datasets_models.py`

**Interfaces:**
- Consumes: Pydantic.
- Produces:
  - `Step(BaseModel)`: `score: float`, `difficulty: float`.
  - `ObservationSequence(BaseModel)`: `learner_id: str`, `kc_id: str`, `steps: list[Step]`.
  - `CalibrationDataset(BaseModel)`: `sequences: list[ObservationSequence]`; `to_file(path: str | Path) -> None`; classmethod `from_file(path: str | Path) -> CalibrationDataset`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_datasets_models.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_datasets_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.datasets'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/datasets/models.py`:

```python
"""Dataset models for mined real-data eval sets (Phase 9b)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class Step(BaseModel):
    """One graded observation in a learner's per-KC sequence."""

    score: float
    difficulty: float


class ObservationSequence(BaseModel):
    """Time-ordered observations for one (learner, KC) pair."""

    learner_id: str
    kc_id: str
    steps: list[Step]


class CalibrationDataset(BaseModel):
    """A mined (or synthetic) set of observation sequences."""

    sequences: list[ObservationSequence]

    def to_file(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2))

    @classmethod
    def from_file(cls, path: str | Path) -> "CalibrationDataset":
        return cls.model_validate_json(Path(path).read_text())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_datasets_models.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/datasets/models.py tests/eval/test_datasets_models.py
git commit -m "feat(9b): calibration dataset models + file round-trip"
```

---

### Task 2: Synthetic generator (the CI oracle)

**Files:**
- Create: `tests/eval/datasets/synth.py`
- Test: `tests/eval/test_datasets_synth.py`

**Interfaces:**
- Consumes: `Step`, `ObservationSequence`, `CalibrationDataset` (Task 1); stdlib `random`, `math`.
- Produces: `synthetic_dataset(*, n_learners: int, n_items: int, seed: int) -> CalibrationDataset` — well-calibrated by construction (`score = sigmoid(true_ability − difficulty)`), fully deterministic from `seed`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_datasets_synth.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_datasets_synth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.datasets.synth'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/datasets/synth.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_datasets_synth.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/datasets/synth.py tests/eval/test_datasets_synth.py
git commit -m "feat(9b): deterministic synthetic calibration dataset"
```

---

### Task 3: Prequential calibration scorer

**Files:**
- Create: `tests/eval/datasets/calibration.py`
- Test: `tests/eval/test_datasets_calibration.py`

**Interfaces:**
- Consumes: `CalibrationDataset`/`ObservationSequence`/`Step` (Task 1), `synthetic_dataset` (Task 2, tests only), and `app.learning.tracer` (`Estimate`, `GlickoEstimator`, `MasteryEstimator`).
- Produces:
  - `ReliabilityBucket(BaseModel)`: `lo, hi, mean_predicted, mean_actual: float`, `n: int`.
  - `CalibrationReport(BaseModel)`: `mae: float | None`, `rmse: float | None`, `n_points: int`, `reliability: list[ReliabilityBucket]`, `sequences_passed: int`, `sequences_total: int`.
  - `score_tracer_calibration(dataset, *, estimator: MasteryEstimator = GlickoEstimator(), tolerance: float = 0.15) -> CalibrationReport`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_datasets_calibration.py`:

```python
"""Prequential tracer-calibration scorer (Phase 9b)."""

import pytest

from tests.eval.datasets.calibration import score_tracer_calibration
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step
from tests.eval.datasets.synth import synthetic_dataset


def test_prequential_predicts_before_updating() -> None:
    # First step is predicted from the cold-start prior (ability 0): expected(0, d=0) = 0.5.
    seq = ObservationSequence(learner_id="L", kc_id="K", steps=[Step(score=1.0, difficulty=0.0)])
    report = score_tracer_calibration(CalibrationDataset(sequences=[seq]))
    assert report.n_points == 1
    # predicted 0.5 vs actual 1.0 -> MAE 0.5 (proves it predicts BEFORE updating, no peeking)
    assert report.mae == pytest.approx(0.5)


def test_well_calibrated_synthetic_data_scores_within_tolerance() -> None:
    # The CI gate: a correct estimator recovers the synthetic learners' abilities. The 0.2
    # aggregate bound has margin over the estimator's cold-start convergence while staying far
    # below the ~0.5 noise floor the discrimination test relies on. (If this fails just over
    # 0.2, that's cold-start convergence, not a bug: raise n_items for more convergence per
    # learner until it passes with margin — never weaken the discrimination test to compensate.)
    report = score_tracer_calibration(synthetic_dataset(n_learners=8, n_items=25, seed=3))
    assert report.mae is not None and report.mae <= 0.2
    assert report.reliability  # populated
    assert report.sequences_passed >= 1


def test_scorer_discriminates_calibrated_from_noise() -> None:
    good = score_tracer_calibration(synthetic_dataset(n_learners=8, n_items=20, seed=3))
    # Unpredictable noise at fixed difficulty: alternating 0/1 keeps the estimate near 0.5.
    noisy = CalibrationDataset(
        sequences=[
            ObservationSequence(
                learner_id="L",
                kc_id="K",
                steps=[Step(score=float(i % 2), difficulty=0.0) for i in range(20)],
            )
        ]
    )
    noisy_report = score_tracer_calibration(noisy)
    assert good.mae is not None and noisy_report.mae is not None
    assert good.mae < noisy_report.mae  # calibrated data scores strictly better


def test_empty_dataset_yields_no_points_and_no_crash() -> None:
    report = score_tracer_calibration(CalibrationDataset(sequences=[]))
    assert report.n_points == 0
    assert report.mae is None and report.rmse is None
    assert report.reliability == []
    assert report.sequences_total == 0


def test_reliability_buckets_partition_all_points() -> None:
    report = score_tracer_calibration(synthetic_dataset(n_learners=6, n_items=15, seed=9))
    assert report.reliability
    assert all(b.n > 0 for b in report.reliability)
    assert all(0.0 <= b.mean_actual <= 1.0 for b in report.reliability)
    assert report.n_points == sum(b.n for b in report.reliability)  # every point in one bucket
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_datasets_calibration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.datasets.calibration'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/datasets/calibration.py`:

```python
"""Prequential tracer-calibration scorer (Phase 9b).

Replays each sequence through the production estimator, predicting each step's expected score
from prior steps only, then updating on the actual — the gold-standard one-step-ahead eval of an
online learner model. Reuses app.learning.tracer so the eval can't drift from what ships.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.learning.tracer import Estimate, GlickoEstimator, MasteryEstimator
from tests.eval.datasets.models import CalibrationDataset

_N_BUCKETS = 5


class ReliabilityBucket(BaseModel):
    lo: float
    hi: float
    n: int
    mean_predicted: float
    mean_actual: float


class CalibrationReport(BaseModel):
    mae: float | None  # None when the dataset has no scorable points
    rmse: float | None
    n_points: int
    reliability: list[ReliabilityBucket]
    sequences_passed: int
    sequences_total: int


def score_tracer_calibration(
    dataset: CalibrationDataset,
    *,
    estimator: MasteryEstimator = GlickoEstimator(),
    tolerance: float = 0.15,
) -> CalibrationReport:
    """Prequential (predict-then-update) replay; MAE/RMSE + a reliability table over all steps."""
    pairs: list[tuple[float, float]] = []  # (predicted, actual) across all sequences
    sequences_passed = 0
    for seq in dataset.sequences:
        estimate = Estimate()
        seq_abs_err = 0.0
        for step in seq.steps:
            predicted = estimator.expected(estimate, difficulty=step.difficulty)
            pairs.append((predicted, step.score))
            seq_abs_err += abs(predicted - step.score)
            estimate = estimator.update(estimate, score=step.score, difficulty=step.difficulty)
        if seq.steps and seq_abs_err / len(seq.steps) <= tolerance:
            sequences_passed += 1
    return CalibrationReport(
        mae=_mae(pairs),
        rmse=_rmse(pairs),
        n_points=len(pairs),
        reliability=_reliability(pairs),
        sequences_passed=sequences_passed,
        sequences_total=len(dataset.sequences),
    )


def _mae(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return sum(abs(p - a) for p, a in pairs) / len(pairs)


def _rmse(pairs: list[tuple[float, float]]) -> float | None:
    if not pairs:
        return None
    return (sum((p - a) ** 2 for p, a in pairs) / len(pairs)) ** 0.5


def _reliability(pairs: list[tuple[float, float]]) -> list[ReliabilityBucket]:
    buckets: list[ReliabilityBucket] = []
    for i in range(_N_BUCKETS):
        lo = i / _N_BUCKETS
        hi = (i + 1) / _N_BUCKETS
        # buckets are [lo, hi); the last one also captures predicted == 1.0
        in_bucket = [
            (p, a) for p, a in pairs if lo <= p < hi or (i == _N_BUCKETS - 1 and p == hi)
        ]
        if not in_bucket:
            continue
        n = len(in_bucket)
        buckets.append(
            ReliabilityBucket(
                lo=lo,
                hi=hi,
                n=n,
                mean_predicted=sum(p for p, _ in in_bucket) / n,
                mean_actual=sum(a for _, a in in_bucket) / n,
            )
        )
    return buckets
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_datasets_calibration.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/datasets/calibration.py tests/eval/test_datasets_calibration.py
git commit -m "feat(9b): prequential tracer-calibration scorer (MAE/RMSE/reliability)"
```

---

### Task 4: Observation-sequence miner

**Files:**
- Create: `tests/eval/datasets/mine.py`
- Test: `tests/eval/test_datasets_mine.py`

**Interfaces:**
- Consumes: `CalibrationDataset`/`ObservationSequence`/`Step` (Task 1), `app.models.learning.LearningEvent`, SQLAlchemy, `structlog`.
- Produces: `async mine_observation_sequences(session: AsyncSession, *, min_length: int = 3) -> CalibrationDataset`.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_datasets_mine.py`:

```python
"""Mining observation sequences from the LearningEvent log (Phase 9b)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearningEvent
from tests.eval.datasets.mine import mine_observation_sequences


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"mine-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _kc(session: AsyncSession, name: str) -> KC:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name=name)
    session.add(kc)
    await session.flush()
    return kc


def _obs(learner_id, kc_id, score, difficulty, when, *, payload=None) -> LearningEvent:
    return LearningEvent(
        learner_id=learner_id,
        kc_id=kc_id,
        event_type="observation",
        payload=payload if payload is not None else {"score": score, "difficulty": difficulty},
        created_at=when,
    )


async def test_mine_groups_orders_and_filters(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc_long = await _kc(db_session, "long")
    kc_short = await _kc(db_session, "short")
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    # kc_long: 3 observations inserted OUT of order -> must return time-ordered
    db_session.add(_obs(learner.id, kc_long.id, 0.5, 0.0, t0 + timedelta(minutes=2)))
    db_session.add(_obs(learner.id, kc_long.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc_long.id, 0.0, 1.0, t0 + timedelta(minutes=1)))
    # kc_short: only 2 -> filtered out at min_length=3
    db_session.add(_obs(learner.id, kc_short.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc_short.id, 1.0, 0.0, t0 + timedelta(minutes=1)))
    await db_session.flush()

    dataset = await mine_observation_sequences(db_session, min_length=3)

    assert len(dataset.sequences) == 1
    seq = dataset.sequences[0]
    assert seq.kc_id == str(kc_long.id)
    assert [s.score for s in seq.steps] == [1.0, 0.0, 0.5]  # time-ordered


async def test_mine_skips_malformed_payload(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    kc = await _kc(db_session, "kc")
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    db_session.add(_obs(learner.id, kc.id, 1.0, 0.0, t0))
    db_session.add(_obs(learner.id, kc.id, 0.5, 0.5, t0 + timedelta(minutes=1)))
    db_session.add(_obs(learner.id, kc.id, None, None, t0 + timedelta(minutes=2), payload={"x": 1}))
    db_session.add(_obs(learner.id, kc.id, 0.0, 1.0, t0 + timedelta(minutes=3)))
    await db_session.flush()

    dataset = await mine_observation_sequences(db_session, min_length=3)

    assert len(dataset.sequences) == 1
    # the malformed observation is skipped; the 3 valid ones remain, in order
    assert [s.score for s in dataset.sequences[0].steps] == [1.0, 0.5, 0.0]


async def test_mine_empty_log_returns_empty(db_session: AsyncSession) -> None:
    assert (await mine_observation_sequences(db_session)).sequences == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_datasets_mine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.eval.datasets.mine'`

- [ ] **Step 3: Write the implementation**

Create `tests/eval/datasets/mine.py`:

```python
"""Mine the LearningEvent log into per-(learner, KC) observation sequences (Phase 9b)."""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learning import LearningEvent
from tests.eval.datasets.models import CalibrationDataset, ObservationSequence, Step

log = structlog.get_logger(__name__)


async def mine_observation_sequences(
    session: AsyncSession, *, min_length: int = 3
) -> CalibrationDataset:
    """Group `observation` events into time-ordered (learner, KC) sequences of >= min_length steps.

    Rows are pulled ordered by (learner, KC, created_at), so each group's steps are already in
    time order and groups are contiguous. A malformed payload (missing/non-numeric score or
    difficulty) is skipped with a warning — never fatal.
    """
    stmt = (
        select(LearningEvent)
        .where(LearningEvent.event_type == "observation", LearningEvent.kc_id.isnot(None))
        .order_by(LearningEvent.learner_id, LearningEvent.kc_id, LearningEvent.created_at)
    )
    rows = (await session.scalars(stmt)).all()

    grouped: dict[tuple[str, str], list[Step]] = {}
    for event in rows:
        step = _step_of(event)
        if step is None:
            continue
        grouped.setdefault((str(event.learner_id), str(event.kc_id)), []).append(step)

    sequences = [
        ObservationSequence(learner_id=learner_id, kc_id=kc_id, steps=steps)
        for (learner_id, kc_id), steps in grouped.items()
        if len(steps) >= min_length
    ]
    return CalibrationDataset(sequences=sequences)


def _step_of(event: LearningEvent) -> Step | None:
    try:
        return Step(
            score=float(event.payload["score"]),
            difficulty=float(event.payload["difficulty"]),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("mine.skip_malformed_observation", event_id=str(event.id))
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_datasets_mine.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/eval/datasets/mine.py tests/eval/test_datasets_mine.py
git commit -m "feat(9b): mine observation sequences from the LearningEvent log"
```

---

### Task 5: Build CLI + gitignore + poe wiring

**Files:**
- Create: `tests/eval/datasets/__main__.py`
- Test: `tests/eval/test_datasets_gitignore.py`
- Modify: `.gitignore`, `pyproject.toml` (`[tool.poe.tasks]`)

**Interfaces:**
- Consumes: `mine_observation_sequences` (Task 4), `CalibrationDataset` (Task 1), `app.core.db.SessionFactory`.
- Produces: `poe build-calibration-dataset` → `python -m tests.eval.datasets`; the gitignore rule for mined datasets.

- [ ] **Step 1: Write the failing test**

Create `tests/eval/test_datasets_gitignore.py`:

```python
"""Governance (D3): mined datasets must never be committable."""

import subprocess


def test_mined_datasets_are_gitignored() -> None:
    target = "tests/eval/datasets/tracer-calibration.json"
    result = subprocess.run(["git", "check-ignore", target], capture_output=True, text=True)
    assert result.returncode == 0, f"{target} is NOT gitignored (governance D3)"
    assert target in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/eval/test_datasets_gitignore.py -v`
Expected: FAIL — `git check-ignore` returns nonzero (no rule yet) → assertion error.

- [ ] **Step 3: Add the gitignore rule**

Append to `.gitignore`:

```
# Mined real-data eval datasets (Phase 9b) — never committed
tests/eval/datasets/*.json
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/eval/test_datasets_gitignore.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Write the CLI**

Create `tests/eval/datasets/__main__.py`:

```python
"""`poe build-calibration-dataset` — mine the live DB into a gitignored calibration dataset.

Manual; needs a DB. Writes tests/eval/datasets/tracer-calibration.json (gitignored) and prints a
summary. Not part of `poe check` / `poe eval`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.core.db import SessionFactory
from tests.eval.datasets.mine import mine_observation_sequences

_OUT = Path(__file__).parent / "tracer-calibration.json"


async def _build() -> None:
    async with SessionFactory() as session:
        dataset = await mine_observation_sequences(session)
    dataset.to_file(_OUT)
    total_steps = sum(len(s.steps) for s in dataset.sequences)
    if dataset.sequences:
        print(f"wrote {_OUT}: {len(dataset.sequences)} sequences, {total_steps} steps")
    else:
        print(f"wrote {_OUT}: no data yet (0 sequences) — run some sessions first")


def main() -> int:
    asyncio.run(_build())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Wire the poe task**

In `pyproject.toml`, under `[tool.poe.tasks]`, add (near the other `tests.eval` tasks like `sweep`):

```toml
build-calibration-dataset = "python -m tests.eval.datasets"
```

Verify the module imports cleanly (no live DB needed to import):

Run: `uv run python -c "import tests.eval.datasets.__main__"`
Expected: no output, exit 0.

- [ ] **Step 7: Full gate**

Run: `uv run poe check`
Expected: lint clean, ty clean, all tests pass (the new `tests/eval/test_datasets_*` plus the existing suite). `poe eval` is untouched.

- [ ] **Step 8: Commit**

```bash
git add tests/eval/datasets/__main__.py tests/eval/test_datasets_gitignore.py .gitignore pyproject.toml
git commit -m "feat(9b): build-calibration-dataset CLI + gitignored dataset store"
```

---

### Manual verification (real data — not CI)

Not automatable (needs a live DB with real observations); do it once to prove the real path:

- [ ] Run `uv run poe build-calibration-dataset`. Expected: it writes `tests/eval/datasets/tracer-calibration.json` and prints either a sequence/step count or "no data yet (0 sequences)". Either is a pass — the pipeline ran end to end.
- [ ] If sequences exist, in a `uv run python` shell: `from tests.eval.datasets.calibration import score_tracer_calibration; from tests.eval.datasets.models import CalibrationDataset; print(score_tracer_calibration(CalibrationDataset.from_file("tests/eval/datasets/tracer-calibration.json")))`. Expected: a `CalibrationReport` with MAE/RMSE and a reliability table over the real sequences.
- [ ] Confirm `git status` does NOT show the written `.json` (gitignored).

---

## Notes for the implementer

- **Namespace package:** do NOT add `tests/eval/datasets/__init__.py`. The repo relies on implicit namespace packages (that's why `python -m tests.eval.harness` and `python -m tests.eval.sweep` work). `__main__.py` is required for the `-m` entry and is not a package marker.
- **Import direction:** sweep/datasets modules import via absolute paths (`from tests.eval.datasets.models import …`), never bare `import models` — the bare form only resolves under pytest, not under `python -m`.
- **Zero app changes:** the scorer reuses `app.learning.tracer` exactly as-is. If you find yourself editing anything under `app/`, stop — the design (D5) forbids it.
- **`db_session` fixture** is transactional (rolled back after each test); insert rows with `session.add(...)` + `await session.flush()`, no commit needed. Use naive datetimes for `created_at` (the column is naive-UTC).
