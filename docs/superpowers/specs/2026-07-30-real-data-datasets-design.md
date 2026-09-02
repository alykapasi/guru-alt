# Phase 9b — Real-data mining pipeline + tracer-calibration eval (design)

**Status:** design, approved for spec-review. **Date:** 2026-07-30. **Branch:** `phase-9b-real-data-datasets`.

## 0. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Pipeline-first.** Build the real-data mining machinery now, on synthetic/seeded data, ready for real traffic. | The platform is pre-alpha; the event log is mostly dev/smoke-test rows. Building the pipeline ahead of traffic mirrors how 9a shipped the sweep foundation before real sweeps. |
| D2 | **First dataset = tracer calibration (label-free).** | The one dataset that needs **no** labeling: ground truth for "did the ability estimate predict the outcome?" is the *next observation already in the log*. Highest ROI (validates the core IRT/Elo engine) with zero judge cost/bias. |
| D3 | **Mined datasets are gitignored local artifacts** — never committed. | No learner data enters git. Reproducible within one machine's snapshot; re-mining yields a new snapshot. Removes any PII-flag machinery. |
| D4 | **Continuous calibration via prequential (predict-then-update) replay.** | Observations carry continuous partial-credit scores (0–1) and the estimator's forward model returns a continuous expected score; thresholding to binary would discard the partial-credit signal the tracer is built around. Prequential replay is the gold-standard for an online learner model. |
| D5 | **Zero app-code changes.** The scorer reuses the estimator's existing `expected()` forward model. | `GlickoEstimator.expected(prior, *, difficulty)` already exists (`app/learning/tracer.py`); reusing it means the eval cannot drift from the production model, and 9b stays purely additive under `tests/eval/`. |
| D6 | **Tracer calibration is a new *offline eval suite*, not a 9a sweep cell.** | It is model-independent (no LLM role varies), like the existing deterministic `tracer`/`grading` suites — so it never enters the sweep matrix. |
| D7 | **Judge-based and human labeling are deferred.** 9b builds only the label-free slice + the reusable mining pipeline. | Grading/KC-tagging gold need a strong-model judge or human review (cost, prompt, bias, or a review UI); none of that is needed for the label-free first slice. YAGNI until a dataset requires it. |

**Deferred to later specs (out of scope here):** strong-model-as-judge labeling for grading/KC-tagging; human-review workflow; other real-data datasets (retrieval relevance, note faithfulness); DSPy optimization (9c). See §12.

## 1. Goal & context

**Goal:** one command mines the `LearningEvent` log into a reproducible tracer-calibration dataset, and a new offline scorer answers **"does the IRT/Elo estimate predict the next real outcome?"** — proving a reusable real-data mining pipeline while shipping a genuinely valuable eval of the product's core engine.

**What already exists (this spec builds on it, does not replace it):**
- `app/models/learning.py` — `LearningEvent(learner_id, kc_id, event_type, payload: JSONB, created_at)`, append-only. Observation events carry `payload = {score, difficulty, weight, item_id, response, latency_ms, hints_used, estimator}` (written by `app/learning/mastery.py`).
- `app/learning/tracer.py` — `Estimate(ability=0.0, uncertainty=1.0)`; `GlickoEstimator` with `expected(prior: Estimate, *, difficulty: float) -> float` (the continuous forward model) and `update(prior, *, score, difficulty) -> Estimate`.
- `tests/eval/harness.py` — the data-driven eval harness: Pydantic case models, scorers returning `EvalReport(suite, results: list[CaseResult(case_id, passed, detail)], mae)`. `poe eval` runs the deterministic suites as the CI gate.
- `tests/eval/sweep/` (9a) — the sibling subpackage pattern this one mirrors (namespace package, no `__init__.py`, absolute imports, dev-only tooling under `tests/eval/`).

## 2. Architecture

A new `tests/eval/datasets/` subpackage (namespace package, mirrors `tests/eval/sweep/`), purely additive. Two data sources feed one scorer:

- **Real (manual):** DB observations → **mine** → gitignored dataset file → **calibration scorer** → MAE/RMSE/reliability report.
- **Synthetic (CI):** a deterministic **synthetic generator** → in-memory dataset → the same scorer → assert a well-calibrated estimator hits MAE ≤ tolerance.

The synthetic path makes the entire pipeline testable without real data or a live DB. Tracer calibration is an **offline eval suite** — model-independent, never a 9a sweep cell.

## 3. Components

Each is a focused unit under `tests/eval/datasets/`.

### 3.1 Dataset models (`models.py`)
- `Step(BaseModel)`: `score: float`, `difficulty: float`.
- `ObservationSequence(BaseModel)`: `learner_id: str`, `kc_id: str`, `steps: list[Step]` (time-ordered).
- `CalibrationDataset(BaseModel)`: `sequences: list[ObservationSequence]`; `to_file(path)` / `from_file(path)` (JSON). Learner/KC ids are stringified UUIDs (opaque handles; numeric payload only — no free-text).

### 3.2 Miner (`mine.py`)
- `mine_observation_sequences(session: AsyncSession, *, min_length: int = 3) -> CalibrationDataset`.
- Queries `LearningEvent` where `event_type == "observation"` and `kc_id is not null`, ordered by `(learner_id, kc_id, created_at)`; groups into per-`(learner, kc)` sequences; reads `score`/`difficulty` from `payload`; drops sequences with fewer than `min_length` steps.
- Malformed payload (missing/unparseable `score` or `difficulty`) → skip that observation with a logged warning; never crash the mine.

### 3.3 Synthetic generator (`synth.py`)
- `synthetic_dataset(*, n_learners: int, n_items: int, seed: int) -> CalibrationDataset`.
- Simulated learners with known true abilities answer items of varying difficulty; each step's `score` is drawn deterministically from the forward model at the learner's true ability (well-calibrated by construction). Fully reproducible from `seed`. The CI/default data source.

### 3.4 Calibration scorer (`calibration.py`)
- `score_tracer_calibration(dataset: CalibrationDataset, *, estimator: MasteryEstimator = GlickoEstimator(), tolerance: float = 0.15) -> CalibrationReport`.
- `MasteryEstimator` is the existing `@runtime_checkable` protocol in `app/learning/tracer.py` (`expected`/`update`/`decay`); `GlickoEstimator.expected(prior, difficulty) = sigmoid(θ − d)` is the continuous expected score.
- **Prequential replay** per sequence, starting from a default `Estimate()`: at each step, `predicted = estimator.expected(estimate, difficulty=step.difficulty)` **before** the outcome → record `(predicted, step.score)` → `estimate = estimator.update(estimate, score=step.score, difficulty=step.difficulty)`.
- **`CalibrationReport(BaseModel)`** (purpose-built — not `EvalReport`, since tracer calibration isn't a swept suite (D6) and `EvalReport` has no field for a reliability table): `mae: float`, `rmse: float`, `n_points: int`, `reliability: list[ReliabilityBucket]` (each `{lo, hi, n, mean_predicted, mean_actual}`), `sequences_passed: int`, `sequences_total: int` (a sequence passes if its own prequential MAE ≤ `tolerance`). MAE/RMSE are aggregated over all `(predicted, actual)` pairs.
- Every sequence is scored from the cold-start prior forward; `min_length ≥ 3` ensures each sequence actually exercises adaptation, not just the prior.

### 3.5 CLI / `poe` wiring (`__main__.py` or a build module)
- `poe build-calibration-dataset` → mine the live DB → write gitignored `tests/eval/datasets/tracer-calibration.json`. Manual; needs a DB + real data. Prints a summary (sequences, total steps, or "no data yet").

## 4. Dataset store & governance (D3)

- Mined datasets live under `tests/eval/datasets/*.json` and are **gitignored** (add `tests/eval/datasets/*.json` to `.gitignore`). The subpackage's `.py` modules are tracked; the mined `.json` snapshots are not.
- A snapshot is stable once written, so re-scoring it across runs is reproducible; re-mining produces a new snapshot. No learner data is ever committed.

## 5. Data flow & CI

- **Real (manual):** `poe build-calibration-dataset` → gitignored dataset → `score_tracer_calibration` → report. Not gated (real data is non-deterministic and currently absent).
- **Synthetic (CI gate):** a `seed`-fixed `synthetic_dataset` → scorer → **assert MAE ≤ tolerance** for a well-calibrated estimator — a real regression guard on the tracer's forward model. This is the only calibration path that gates.
- `poe eval` — unchanged; the existing deterministic gate stays exactly as-is. The synthetic-calibration assertion lives in the pytest suite (like the other harness tests), not in the `poe eval` standalone runner.

## 6. Calibration methodology (D4)

Prequential (a.k.a. predict-then-update / one-step-ahead) evaluation: for each `(learner, kc)` sequence in time order, the estimator predicts the current item's expected score from **only prior** steps, then updates on the actual. This scores every observation after the cold-start as a genuine forecast and reuses the production `expected()`/`update()` pair, so the eval measures exactly what ships.

- **Metrics:** MAE and RMSE of predicted-vs-actual expected score; a reliability table (bucket by predicted score, compare mean actual) to expose systematic over/under-confidence.
- **Continuous, not binary:** no correctness threshold — partial-credit scores are compared directly to the continuous expected score. (A binary log-loss/Brier/ECE variant is a possible later addition; it is not built here.)

## 7. Synthetic generator details

A well-calibrated synthetic learner is the CI oracle: because outcomes are generated from the same forward model the estimator inverts, a correct estimator must calibrate to within tolerance — and a deliberately corrupted scorer (shuffled predictions) must not. This gives both a positive gate and a discrimination test (§8/§11).

## 8. First deliverable (the end-to-end proof)

Running the pipeline end to end is the definition of done: `synthetic_dataset(seed=…)` → `score_tracer_calibration` → a `CalibrationReport` with a bounded MAE and a populated reliability table, asserted green in CI; **and** `poe build-calibration-dataset` runs against the live DB and either writes a real dataset or reports "no data yet," then `score_tracer_calibration` prints its MAE/RMSE/reliability for whatever real sequences exist. **Output supports statements like** *"on N real sequences the Glicko estimate predicts next-item score at MAE 0.xx"* — and the same machinery is reusable for every future mined dataset.

## 9. Execution & `poe` wiring

- `poe build-calibration-dataset` — mine the DB → gitignored dataset. Manual; needs a DB.
- The synthetic calibration check runs inside `uv run poe test` (a pytest, deterministic).
- `poe eval` and `poe check` — unchanged.

## 10. Error handling & determinism

- Empty log / all-short sequences → miner returns an empty `CalibrationDataset`; the builder writes it and warns; the scorer returns a report with 0 cases (`pass_rate` vacuously 1.0, `mae=None`) — an honest "no data yet," never a crash.
- Malformed observation payload → the observation is skipped with a logged warning; the sequence continues.
- The synthetic path is deterministic from `seed`; the real path is a point-in-time snapshot (stable once written, non-deterministic across re-mines as the DB grows).

## 11. Testing

- **Miner** — seed a transactional-fixture DB with observation events across several `(learner, kc)` pairs (varying counts + timestamps); assert correct grouping, time-ordering, and short-sequence filtering; assert a malformed-payload observation is skipped, not fatal.
- **Synthetic generator** — deterministic given `seed`; produces sequences of the declared shape and length.
- **Scorer** — (a) on a well-calibrated synthetic dataset → MAE ≤ tolerance (the gate); (b) on shuffled/mis-ordered predictions → MAE high (proves the scorer *discriminates*, not just runs); (c) prequential predict-before-update ordering verified on a tiny hand-built sequence; (d) reliability-table bucketing correct; (e) empty dataset → 0-case report, no crash.

## 12. Scope boundaries

**In scope (9b):** the `tests/eval/datasets/` subpackage — dataset models, the observation-sequence miner, the synthetic generator, the prequential tracer-calibration scorer (MAE/RMSE/reliability), the gitignore rule, `poe build-calibration-dataset`, and the synthetic CI gate.

**Explicitly deferred:**
- **Judge-based labeling** — strong-model-as-judge gold for grading/KC-tagging accuracy (its own prompt, cost, confidence/agreement story).
- **Human-review workflow** — a labeling UI/queue for gold sets.
- **Other real-data datasets** — retrieval relevance, note-distillation faithfulness.
- **Binary calibration** (log-loss/Brier/ECE) as an alternative metric surface.
- **9c — DSPy optimization**, which will consume these datasets + 9a metrics.

## 13. New dependencies

None. Mining is SQLAlchemy over the existing `LearningEvent`; MAE/RMSE/reliability are trivial pure-Python arithmetic; the synthetic generator uses the stdlib `random` seeded for determinism. App runtime code is **not** touched — 9b is entirely additive under `tests/eval/` plus one `.gitignore` line.
