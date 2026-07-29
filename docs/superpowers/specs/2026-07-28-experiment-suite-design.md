# Phase 9a — Experiment & sweep foundation (design)

**Status:** design, approved for spec-review. **Date:** 2026-07-28. **Branch:** `phase-9-experiment-suite`.

## 0. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Phase 9 is decomposed; this spec is only 9a** (the measurement foundation). | Phase 9 as roadmapped is four subsystems with a dependency order; building the foundation first ships measurable value fast and de-risks the rest. |
| D2 | **9a = sweep/ablation runner + MLflow tracking + cost-as-metric**, run on the existing hand-authored golden cases. | The first experiment (D5) is fully served by the existing labeled cases; no new data pipeline needed to ship it. |
| D3 | **Real-data datasets from the event log → deferred to spec 9b.** | The `LearningEvent` log stores the model's *own* outputs, not ground truth; turning it into eval labels needs a labeling story that deserves its own design. |
| D4 | **DSPy optimization → deferred to spec 9c.** | DSPy needs the metrics + datasets 9a/9b produce to optimize against and to measure deltas; building it first would be measuring blind. |
| D5 | **First end-to-end experiment = model-role selection + cost.** | Directly serves the roadmap's "inform the alpha rollout with measured model choices" goal, and forces cost-as-a-first-class-metric from day one. |
| D6 | **Not a CI gate.** `poe eval` stays the deterministic offline gate; the sweep is a paid/slow/manual `poe sweep`. | Live cross-model sweeps are non-deterministic and cost real money; they are a developer tool, not a pipeline gate. |
| D7 | **Tracking behind a `Tracker` seam**, MLflow as the primary impl, a fake for tests. | Roadmap mandates a swappable tracking backend; the seam also keeps the runner CI-testable without an MLflow server. |
| D8 | **Config = declarative YAML** under `tests/eval/experiments/`; **code lives in `tests/eval/`**, run standalone (not pytest-collected). | Matches the roadmap's "extend the existing harness (`tests/eval/`, `poe eval`)" and the harness's "cases as data" ethos. |
| D9 | **Cells build a fresh `LLMClient` with an overridden role→model map**, never mutate the global registry. | Isolation: no cross-cell leakage; the alternative (monkeypatching the global registry per cell) is stateful and leak-prone. |

**Deferred to later specs (out of scope here):** real-data dataset construction from the event log (9b); DSPy-compilable modules + offline compilation (9c). See §12. The full catalog of experiments this foundation serves — and the offline/online split that decides which *machine* runs each — is mapped in §14.

## 1. Goal & context

**Goal:** one command runs a role→model × config sweep over the existing eval suites, capturing **quality and cost per configuration**, with results landing in MLflow for side-by-side comparison — enough to pick a defensible prod role→model map for the alpha rollout, and to answer "does component X earn its cost?" for any two configs.

**What already exists (this spec builds on it, does not replace it):**
- `tests/eval/harness.py` — a data-driven eval harness: Pydantic case models, JSON case files (`tests/eval/cases/*.json`), and scorers returning an `EvalReport` (with `pass_rate` and `MAE`). Six suites: `grading`/`tracer` (deterministic, no model), `rubric`/`kc_tagging` (live model), `retrieval` (DB + embeddings), `grounding` (citation contract, scripted model). Scorers that need a model take an `LLMClient` argument.
- `poe eval` → `python -m tests.eval.harness` → runs only the two deterministic suites as the CI gate.
- `app/llm/registry.py` — maps roles (`FAST`/`SMART`/`GENIUS`/`EMBED`) to `(provider, model)` per environment; `fake_llm_client(...)` builds a `FakeProvider`-backed client.
- `app/llm/pricing.py` — `cost_usd(model: str, usage: Usage) -> float` over a `_PRICES` table; `Usage` has `input_tokens`, `output_tokens`, `.total`.

## 2. Architecture

An **offline, on-demand experiment runner** layered on the existing harness. A declarative sweep config defines the axes (candidate model per role, generation config, which suites, component toggles). The runner:

1. **expands** the axes into config **cells** (a cell = one fully-resolved experiment configuration),
2. **runs** each cell's selected suites against the existing `cases/*.json` through a **cost-instrumented** `LLMClient` built with that cell's role→model overrides,
3. **logs** params + metrics + artifacts for each cell to a pluggable **`Tracker`** (MLflow, local file store), one cell = one run,
4. and a separate **report** command reads runs back to produce a leaderboard and pairwise ablation diffs.

The runner never mutates global state (fresh client per cell) and never enters the CI gate.

## 3. Components

Each is a focused unit with a well-defined interface.

### 3.1 `SweepConfig` + expansion
- **Does:** parse a YAML sweep file into a validated `SweepConfig`, then expand its axes into `list[Cell]` (cartesian product).
- **`Cell`:** a frozen record = `{ id, role_overrides: dict[role, (provider, model)], gen_config, toggles: dict[str, bool], suites: list[str] }`.
- **Depends on:** the role registry (to resolve a model name + provider into a usable mapping), PyYAML.

### 3.2 Cost-instrumented client
- **Does:** wrap an `LLMClient`; on every `complete`/`embed`, accumulate the returned `Usage` keyed by `(role, model)`; expose a `cost_summary()` that prices each bucket via `pricing.cost_usd(model, usage)` and totals cost + tokens for the cell.
- **Why:** makes "earn its cost" answerable — every cell carries `$` and token totals alongside `pass_rate`/`MAE`.
- **Depends on:** `app/llm/pricing.py`, `Usage`. Wraps, does not modify, the underlying client.

### 3.3 Cell runner
- **Does:** for one `Cell` — build a fresh `LLMClient` whose role→model map is the cell's `role_overrides` (falling back to the ambient env map for roles the cell doesn't override), wrap it in the cost-instrumented client, run the cell's selected harness scorers over the existing cases, and return `(list[EvalReport], CostSummary)`.
- **Suites in scope:** the genuinely model-driven scorers — `rubric` (SMART-role grading), `kc_tagging` (tagging model), `retrieval` (EMBED-role embeddings + retrieval-time LLM use). A sweep config selects the subset that exercises the role it varies. **Excluded:** `grading`/`tracer` (deterministic, no model) and `grounding` (its scorer builds a *scripted* fake client internally — it's a citation-**contract** test, constant across models, not a quality target).
- **Depends on:** the harness scorers (reused as-is), the registry's client-construction path, a DB session for `retrieval`.

### 3.4 `Tracker` protocol + impls
- **Protocol:** `start_run(name) / log_params(dict) / log_metrics(dict) / log_artifact(path) / end_run()` (context-manager friendly).
- **`MLflowTracker`:** logs to a **local file-backed store** (`mlruns/`, gitignored) — tracking subset only (params/metrics/artifacts + the run-comparison UI via `mlflow ui`); **no** model registry / serving / deployment surface.
- **`FakeTracker`:** in-memory capture for tests (records the calls; asserts what was logged) — lets the whole runner be unit-tested without an MLflow install/server.
- **One cell = one run.** Params = the cell config (role→model map, gen config, toggles, suite set). Metrics = per-suite `pass_rate` + `MAE`, plus `cost_usd` and `total_tokens`. The leaderboard sorts on **quality and on cost independently** — both always well-defined, including `$0` local/Ollama models — so no quality-per-dollar ratio (which would divide by zero for free models) is load-bearing; an optional cost-adjusted composite, if added, uses a cost floor, and its formula is pinned in the plan. Artifacts = the per-suite `EvalReport` JSONs.

### 3.5 Reporting
- **Does:** `poe sweep-report` reads runs from the tracking store and prints (a) a **leaderboard** — cells ranked by quality and by cost — and (b) a **pairwise ablation diff** — given two run ids/names, the per-suite quality delta and the cost delta.
- **Why one unit:** the role-selection leaderboard and the ablation view are the same diff machinery over the same logged metrics.

## 4. Config schema (YAML)

A sweep file declares axes; the runner takes their cartesian product. Illustrative shape (final field names pinned in the plan):

```yaml
name: role-selection-v1
suites: [rubric, kc_tagging]   # the suites that exercise the swept role (SMART here)
axes:
  SMART:            # candidate models for the SMART role
    - {provider: openrouter, model: claude-sonnet-5}
    - {provider: openrouter, model: claude-opus-4-8}
    - {provider: ollama,     model: <a-cheap-oss-model>}
  # roles not listed here stay at the ambient env mapping
gen_config:          # optional, swept if multiple listed
    - {temperature: 0.0}
toggles: {}          # ablation toggles (see §7); empty for a pure model sweep
```

Cells = product(axes × gen_config × toggles). A pure single-role sweep with N candidates and one gen_config yields N cells.

## 5. Cost capture

The platform already prices calls (`pricing.cost_usd`). The cost-instrumented client (§3.2) sums `Usage` per `(role, model)` across a cell; `cost_summary()` prices each bucket and returns `{cost_usd, total_tokens, per_role: {...}}`. This is logged as run metrics. Cost is captured **per cell**, from the actual calls that cell made — not estimated. Ollama-served models price to `$0` (or an explicit local rate) via the `_PRICES` table, which is itself the honest answer for the OSS-on-own-hardware case.

## 6. MLflow tracking

- **Local, file-backed:** `MLFLOW_TRACKING_URI` defaults to a repo-local `mlruns/` directory (gitignored). No server required to *log*; `mlflow ui` serves the comparison UI on demand.
- **Tracking subset only:** params, metrics, artifacts, and the comparison UI. Explicitly **not** the model registry, serving, or deployment surfaces (the "overkill" parts the roadmap calls out).
- Behind the `Tracker` seam (D7), so the store is swappable and tests never touch MLflow.

## 7. Ablations (S4, folded in)

An ablation is a sweep whose cells differ by exactly one **toggle**, plus the pairwise-diff report (§3.5). Toggles are boolean config axes that flip an app component for the duration of a cell's run:
- Toggles are supported **where a config seam already exists** (e.g. a retrieval hybrid-vs-vector setting, if one is exposed on the retrieval path or `Settings`).
- Toggles that would need a **new** seam in app code (e.g. refinement-gate on/off, memory on/off) are **not pre-built** — the seam is added when that ablation is first actually run (YAGNI). The plan wires **one** concrete toggle end-to-end to prove the path; the rest are additive later.

The model-role sweep already exercises the ablation machinery (cell A vs cell B is a pairwise diff), so ablation reporting ships value even before any component toggle exists.

## 8. First experiment (the end-to-end proof)

**Model-role selection + cost.** A `role-selection-v1.yaml` sweeps the `SMART` role across a small candidate set (a frontier model, a mid model, a cheap OSS model), runs the suites that exercise SMART (`rubric`, `kc_tagging`) over the existing cases, logs quality + cost per cell to MLflow. **Output:** a leaderboard that supports statements like *"SMART=sonnet-5 reaches 96% of opus's rubric/kc-tagging quality at 18% of the cost."* Running this end-to-end (config → cells → priced runs → leaderboard) is the definition of done for the foundation.

## 9. Execution & `poe` wiring

- `poe sweep <config.yaml>` — runs a sweep, logs to the tracking store. Paid/slow/manual; requires provider credentials (OpenRouter for frontier models, Ollama for OSS). **Not** in `poe check`/`poe eval`.
- `poe sweep-report [--compare RUN_A RUN_B]` — leaderboard, or a pairwise ablation diff.
- `poe eval` — **unchanged**; the deterministic offline gate stays exactly as-is.

## 10. Error handling & determinism

- Harness scorers already **record** (not raise) on unparseable model output — a weak candidate scores poorly rather than crashing the suite.
- The sweep additionally **survives a whole cell failing** (provider error/timeout/auth): it logs the run with status `failed`, keeps whatever cost the cell already incurred, and continues the remaining matrix — one bad model never aborts the sweep.
- Sweeps are **non-deterministic** by nature (live models). Reproducibility is "same config → a new, comparable run," not bit-identical output. Each run records its config + timestamp; MLflow compares runs across time.

## 11. Testing

The runner is **fully CI-testable with fakes**, even though real sweeps are not run in CI:
- **Config expansion** — YAML → cells, cartesian product, override resolution — pure unit tests.
- **Cost accumulation + pricing** — feed known `Usage` through the cost-instrumented client, assert `cost_summary()` against `pricing.cost_usd` — pure unit tests.
- **Cell runner** — run against `FakeProvider` (scripted replies) over a tiny case set; assert `EvalReport`s + a `CostSummary` come back; no live model.
- **Tracker** — `FakeTracker` asserts the exact params/metrics/artifacts a cell logs.
- **Reporting** — build known runs in a `FakeTracker`, assert the leaderboard ordering and the pairwise diff math.
- **Pricing table** — a smoke test that every model named in the shipped example configs is present in `_PRICES` (a missing price is a silent-zero-cost bug).

The **live** sweep stays a manual tool, validated by actually running `role-selection-v1` once against real providers during implementation (a live-walkthrough, not a CI test).

## 12. Scope boundaries

**In scope (9a):** sweep config + expansion, cost-instrumented client, cell runner over the model-driven suites (`rubric`, `kc_tagging`, `retrieval`) on existing cases, the `Tracker` seam + `MLflowTracker` + `FakeTracker`, leaderboard + pairwise-ablation reporting, one concrete component toggle wired end-to-end, `poe sweep`/`poe sweep-report`, the `role-selection-v1` experiment.

**Explicitly deferred:**
- **9b — real-data datasets** from the `LearningEvent` log (mining real inputs + the ground-truth labeling story: strong-model-as-judge / human review / downstream-outcome signal).
- **9c — DSPy optimization** (introduce DSPy-compilable modules behind a **prompt seam that spans the whole prompt surface** — user-facing tutoring prompts *and* system prompts, agentic/LangGraph node instructions, and the pipeline's guiding prompts for grading/refinement-gate/content-generation/distillation; compile offline against 9b datasets + 9a metrics; report deltas vs hand-written prompts).
- **Latency / speed capture** — deliberately out of 9a (see §14). Dev hardware is severely suboptimal; moving a role to a real serving path (OpenRouter/Groq/self-hosted on proper infra) improves tok/s for free, so latency only becomes a real signal once roles run on representative infra. The cost-instrumented client (§3.2) is where a per-call timer slots in when that time comes.
- **YAGNI now:** additional ablation toggles beyond the one proof toggle; prompt-variant sweeping at scale (no prompt seam exists yet — that arrives with 9c); any MLflow surface beyond tracking (registry/serving/deployment); making the sweep a CI gate.

## 13. New dependencies

- `mlflow` (tracking + local UI). Added as a dev/experiment dependency, not a runtime app dependency.
- `PyYAML` promoted to a direct dependency (already present transitively in the lock).
- `app/` runtime code is **not** touched except possibly to expose **one** ablation-toggle config seam (§7); the sweep is otherwise additive under `tests/eval/`.

## 14. Experiment landscape (the governing map)

9a is the *measurement foundation*; this section records the full catalog of experiments it is the foundation **for**, so 9b, 9c, and the future online-experimentation phase each have a named home instead of being re-derived every time. It is a map, not a backlog commitment — each item lands when its phase does.

**The load-bearing split — offline vs online.** Every experiment falls on one side of a hard line, and the line decides which *machine* runs it:

- **Offline / reference-scored** — hold a config fixed, run it against a fixed dataset, score against gold. Cheap, repeatable, fast, no real users. This is the 9a sweep runner (and everything 9b/9c layer onto it).
- **Online / behavior-scored** — assign *real learners* to variants and measure behavior/outcomes over time (retention, completion, mastery velocity). Needs learner bucketing + event instrumentation + statistical significance — a separate substrate 9a cannot provide, and therefore a distinct future phase, not a 9a/9b/9c item.

| Experiment | Optimizes | Machine | Home |
|---|---|---|---|
| Model-role selection (per-role model choice) | cost · quality | offline sweep | **9a** |
| Generation config (temperature, thinking budget, max_tokens) | quality · cost | offline sweep | **9a** |
| Retrieval config (hybrid vs vector, HNSW ef/m, chunk size, top-k) | quality · cost | offline sweep | **9a** (toggle/axis) |
| Prompt caching / batching on non-interactive grading & distillation | cost | offline sweep | **9a** (toggle) |
| Embedding model + dimensionality | quality · cost | offline sweep | **9a** |
| Grading / tracer / KC-tagging accuracy vs *expanded* gold | quality | offline sweep | **9b** (needs labeled data) |
| Note-distillation faithfulness / coverage | quality | offline sweep | **9b** (needs scorer + data) |
| Prompt composition — user-facing *and* system/agentic/pipeline prompts | quality · cost | offline sweep | **9c** (needs prompt seam) |
| Lesson-plan policy (does it accelerate mastery?) | learning outcome | simulated learners | closed-loop sim (future) |
| Note format · gamification · scaffolding/pacing | UX · outcome | online A/B | online-experimentation phase (future) |
| Durable-learning north star (1-wk / 1-mo retention) | outcome | online A/B | online-experimentation phase (future) |

**Latency / speed — explicit placeholder, deliberately out of 9a.** Latency is offline-measurable through the same cost-instrumentation seam (§3.2), but is *not* captured now: dev hardware is severely suboptimal (Ollama on a workstation), and moving any role to a real serving path (OpenRouter, Groq, a self-hosted model on proper infra) improves tok/s automatically. Measuring latency against today's hardware would optimize the wrong variable. Revisit once roles run on representative infra.

**Prompt composition (9c) scope note.** The 9c prompt seam is not only the user-facing tutoring prompt — it spans the whole prompt surface: system prompts, agentic/LangGraph node instructions, and the pipeline's guiding prompts (distillation, grading, refinement gate, content assembly). DSPy compiles against the same seam. The 9a cell model already has room for a plain `prompt_variant` axis, so 9c adds the seam, not new runner machinery.
