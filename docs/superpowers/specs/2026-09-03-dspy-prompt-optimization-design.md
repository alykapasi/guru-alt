# Phase 9c — Runtime DSPy Prompt Optimization (kc_tagging) — Design

**Status:** Approved design. Feeds an implementation plan under `docs/superpowers/plans/`.

## Context & Goal

ROADMAP Phase 9 bullet #4 (the "DSPy optimization workstream"): compile internal LLM modules
against eval datasets/metrics and **report eval deltas vs. the hand-written prompts**. 9a delivered
the sweep/MLflow substrate; 9b delivered the real-data dataset pipeline. 9c introduces **DSPy as a
runtime substrate behind a thin seam** and proves the whole optimization capability **end-to-end on
one module — `kc_tagging`** — pipeline-first, the way 9b shipped machinery + one real instance.

Concretely, 9c delivers:

1. DSPy enters the codebase as a **runtime dependency confined to `app/prompts/`** (CLAUDE.md's
   named DSPy home).
2. `kc_tagging` becomes a **runtime DSPy program** whose compiled artifact (optimized instruction +
   few-shot demos) **ships in git** and loads at runtime through a **role-based DSPy-LM seam**.
3. An **offline compile step** + a **measured delta-vs-baseline report** reuse the 9a harness/MLflow.
4. An **expanded hand-authored golden `kc_tagging` set** with a deterministic train/dev split makes
   the delta honest.

## Decisions

- **D1 — Pipeline-first, one module.** Build the reusable optimization capability; prove it on
  `kc_tagging` only. Optimizing other modules is deferred to later slices. (Mirrors 9b.)
- **D2 — DSPy at runtime, behind a thin seam.** `app/prompts/` is the *only* place that imports
  `dspy`; everything else calls plain Python functions. (User choice over the offline-only
  alternative.)
- **D3 — First module = `kc_tagging` (FAST role).** Pure/testable, structured output, an existing
  golden suite + scorer as ready trainset/metric, best-effort semantics (parse failure → safe
  fallback is natural), and it runs in the ingestion background job — off the hot user path, so
  low blast radius.
- **D4 — DSPy reaches a model ONLY through our role-based `LLMClient`.** A custom
  `RoleLM(dspy.LM)` bound to `(role, client)`; never litellm, never a provider SDK. Honors "no
  provider SDK in services, code references roles."
- **D5 — Async-first adapter (A), threadpool bridge (B) as a documented fallback.** `kc_tagging`
  runs in an async taskiq worker and `LLMClient.complete` is `async`, while DSPy's call path is
  classically synchronous. Design the adapter async-first; fall back to running the compiled program
  via `asyncio.to_thread(...)` with `run_coroutine_threadsafe(...)` back to the app loop **only if**
  empirical verification (in the plan) shows the pinned DSPy version's async path for a custom LM
  isn't clean. Rejected: a fresh `asyncio.run(...)` per call (breaks httpx pooling).
- **D6 — Compiled artifact ships committed.** `app/prompts/artifacts/kc_tagging.json` is production
  config derived from **non-PII golden data**, so — unlike 9b's gitignored datasets — it belongs in
  git. Runtime falls back to an **uncompiled program** if the artifact is missing/corrupt. (A future
  compile over *mined real* data would embed learner text → that artifact gets gitignored; out of
  scope here.)
- **D7 — Optimizer = `BootstrapFewShot`; DSPy module = `dspy.Predict`.** Cheap, bootstraps few-shot
  demos, good on FAST/local models. No `ChainOfThought` (kc_tagging is classification — CoT just
  burns tokens/latency). `MIPROv2` is seam-compatible and deferred.
- **D8 — The delta is measured uncompiled-DSPy → compiled-DSPy**, both through the *same* machinery,
  so it isolates the effect of optimization rather than confounding it with the code rewrite. Plus a
  one-time **rewrite-no-regression** check that the uncompiled DSPy program ≈ the legacy hand-written
  module on the dev set. Measured on **held-out dev** cases.
- **D9 — Expanded golden `kc_tagging` set (~24 cases)** with a deterministic train/dev split. The
  existing 4 cases are far too few to both train and validate honestly.

## Architecture & Components

DSPy lives only under `app/prompts/` (a normal beartype-claw'd package with `__init__.py`). The
offline compile/report tooling lives under `tests/eval/prompts/` (namespace package, mirroring
`tests/eval/sweep/` and `tests/eval/datasets/`: no `__init__.py`, absolute imports, `__main__.py`
for the CLI entries).

### `app/prompts/lm.py` — `RoleLM(dspy.LM)` (the seam)

A custom DSPy LM bound to a `ModelRole` and our `LLMClient`. Responsibilities:

- Translate DSPy's prompt/messages into our `list[ChatMessage]` (+ optional `system`), call
  `await client.complete(role, messages, …)`, and return the reply text in the shape DSPy expects
  from an LM call.
- Never import or invoke litellm/a provider SDK — the model is reached purely via the registry, by
  role. `RoleLM(ModelRole.FAST, client)` for `kc_tagging`.
- Surface token `Usage` from each `complete` so the caller can still cost-log the call (preserving
  the per-call token/cost logging the codebase guarantees).
- **Async-first (D5).** The exact `dspy.LM` method(s) to override (`forward`/`acall`/`__call__`) and
  whether DSPy's async path is clean are pinned-version specifics the **plan verifies empirically
  before building**; if not clean, the plan uses the threadpool bridge (B) instead, with the same
  external behavior.

### `app/prompts/kc_tagging_program.py` — the DSPy program

- A `dspy.Signature` with inputs `passage: str` and a numbered `candidates` list, output
  `tags` — a structured list of `{kc: int, confidence: float}` (candidate index + confidence),
  matching what `kc_tagging` already parses by hand today.
- A `dspy.Module` wrapping `dspy.Predict(signature)`.
- `load_kc_tagging_program() -> Module`: loads the compiled artifact
  (`app/prompts/artifacts/kc_tagging.json`) and caches it. **Missing/corrupt → an uncompiled
  program seeded with today's hand-written instruction and zero demos → never raises.** This is the
  runtime fallback that keeps ingestion working before/without a compiled artifact.

### `app/prompts/artifacts/kc_tagging.json` — the compiled program

The serialized DSPy program (optimized instruction + bootstrapped few-shot demos), **committed**
(D6). Produced by the offline compile step; absent until the first manual compile, in which case the
uncompiled fallback runs.

### `app/learning/kc_tagging.py` — rewired to the DSPy program

`tag_chunk(client, text, candidates, *, min_confidence=0.5, max_tokens=256) -> tuple[list[KCTag],
Usage]` keeps its **exact signature, return type, and best-effort contract**. Internally it now:

1. builds/loads the DSPy program (via `RoleLM(FAST, client)`),
2. runs it on `(passage=text, candidates=[c.name …])`,
3. maps the program's typed `tags` back to `list[KCTag]` applying the **existing** confidence
   threshold + de-dup (highest confidence) + candidate-order logic, and
4. returns the accumulated `Usage`.

Any DSPy exec/parse failure → **empty tags** (unchanged best-effort semantics; never fails the
ingest). The `_parse_tags`/`_extract_json`/threshold/dedup helpers that survive move with the
mapping; the hand-written `_SYSTEM_PROMPT` becomes the **uncompiled program's default instruction**
(so the uncompiled program is a faithful stand-in for the legacy behavior — the D8 baseline).

### `tests/eval/prompts/` — offline compile + report (namespace package)

- `trainset.py` — `load_kc_tagging_examples() -> tuple[list[dspy.Example], list[dspy.Example]]`:
  loads the expanded golden cases and produces a **deterministic** train/dev split.
- `metric.py` — the DSPy metric: **exact-set-match** of predicted vs. expected candidate indices,
  consistent with `harness.score_kc_tagging`'s pass criterion.
- `compile.py` — `poe compile-prompt kc_tagging` (paid/slow/manual, needs a model): build the
  program, run `BootstrapFewShot(metric)` over the trainset, save
  `app/prompts/artifacts/kc_tagging.json`. Not part of `poe check`/`poe eval`.
- `report.py` — `poe prompt-report kc_tagging` (paid/manual): evaluate **baseline (uncompiled) vs.
  candidate (compiled)** on the held-out dev set, log both runs to MLflow via 9a's `Tracker` seam,
  and print the delta (pass-rate/MAE). Reuses the 9a harness + tracking.

### Expanded golden dataset

`tests/eval/cases/kc_tagging.json` grown from 4 to ~24 hand-authored cases (short: `text` +
candidate names + expected 1-based indices). Still drives the existing live `kc_tagging` eval suite;
`trainset.py` splits it train/dev deterministically for compile + report.

## Data Flow

- **Runtime:** ingestion → `tag_chunk(client, text, candidates)` → compiled DSPy program via
  `RoleLM(FAST)` → `client.complete` → typed tags → `list[KCTag]` (threshold/dedup/order) + `Usage`.
  Any failure → empty tags.
- **Offline compile:** `poe compile-prompt kc_tagging` → load train examples →
  `BootstrapFewShot(metric)` → save `app/prompts/artifacts/kc_tagging.json` (commit if it wins).
- **Offline report:** `poe prompt-report kc_tagging` → evaluate baseline vs. compiled on dev →
  MLflow runs + printed delta.

## Error Handling

- Compiled artifact missing/corrupt at runtime → warn, use the uncompiled program → ingestion keeps
  tagging.
- DSPy program exec/parse failure at runtime → empty tags (unchanged best-effort contract).
- DSPy async path not clean at the pinned version → threadpool bridge (B), same external behavior.
- Compile with no model / failure → clear error; **never** clobbers the committed artifact.

## Testing

**CI (offline, deterministic, `FakeProvider` — no live model, no compile run):**

- `RoleLM` translates roles/messages correctly and captures `Usage` (fake client).
- `tag_chunk` through the DSPy path preserves tags + threshold + de-dup + candidate order, and falls
  back to empty tags on garbage output (mirrors the existing kc_tagging tests, now via DSPy).
- Artifact loader: loads the committed artifact; missing/corrupt → uncompiled fallback, no crash.
- Metric: exact-set-match correctness.
- Trainset: deterministic train/dev split (counts + membership stable across calls).
- The committed artifact loads and the program runs on the fake client.

For the happy-path offline tests, the `FakeProvider`/fake client must emit output in **DSPy's
adapter-parseable shape** (the format DSPy expects to parse into the signature's `tags` field), not
`kc_tagging`'s legacy raw-JSON reply — the plan pins the exact canned reply once the DSPy adapter is
verified. The garbage-input fallback test just returns unparseable text.

**Manual (paid/live, not CI):** `poe compile-prompt kc_tagging` then `poe prompt-report kc_tagging`
→ observe the delta on held-out dev; commit the artifact if it's an improvement.

## Dependencies

- Add `dspy` (DSPy) as a **runtime** dependency, pinned. Heavy transitive tree (litellm, etc.) is
  the accepted cost of the runtime-DSPy choice (D2); `RoleLM` bypasses litellm at runtime.
- No other new deps — the metric/report reuse 9a (MLflow already present) and the harness.

## File Structure

**Created:**
- `app/prompts/__init__.py`, `app/prompts/lm.py`, `app/prompts/kc_tagging_program.py`
- `app/prompts/artifacts/kc_tagging.json` (produced by the manual compile; committed)
- `tests/eval/prompts/trainset.py`, `metric.py`, `compile.py`, `report.py` — `compile.py`/`report.py`
  each expose a `main()` invoked directly as `python -m tests.eval.prompts.compile|report` (two entry
  modules, so no package-level `__main__.py` is needed, unlike the single-entry `datasets` subpackage)
- Tests: `tests/test_prompts_lm.py`, `tests/test_kc_tagging_dspy.py` (app side); `tests/eval/test_prompts_*.py` (offline side)

**Modified:**
- `app/learning/kc_tagging.py` (rewire to the DSPy program; public API unchanged)
- `tests/eval/cases/kc_tagging.json` (expanded to ~24 cases)
- `pyproject.toml` (dspy dep; `compile-prompt` + `prompt-report` poe tasks)

## Scope Boundary / Non-Goals

- Only `kc_tagging`. No other module is converted this slice.
- `BootstrapFewShot` only; `dspy.Predict` only. `MIPROv2`/`ChainOfThought` deferred (seam-compatible).
- No mined-real-data compilation (that's the future gitignored-artifact case).
- No auto-selection of compiled vs. hand-written at runtime beyond "compiled if present, else
  uncompiled fallback."

## Open Risk (verify in the plan)

DSPy's async support for a **custom** `dspy.LM` at the pinned version (D5). The plan must verify it
empirically first (as the codebase did for the Phase 6 LangGraph checkpointer) and pick Approach A
or B accordingly — the external behavior of `tag_chunk` is identical either way.
