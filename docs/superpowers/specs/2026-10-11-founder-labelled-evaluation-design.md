# Founder-labelled evaluation (S59) — design

Tracker item S59, workstream 6. Branch `feat/workstream-6` (PR #47).

## Goal

Turn the founder's own use of Guru into labelled, traceable evaluation sets, and report how the
models' decisions agree with those labels — with everything runnable offline. No paid model call
is made by anything in this piece unless a person passes `--allow-paid`.

**Success:** the founder marks their account eligible, draws examples in the admin portal, labels
them blind, and `poe label-report` shows agreement per area with its sample size; `--replay`
re-runs today's prompts on the frozen inputs against local models; an injection fixture set is
scored the same way.

**Not in scope:** paid runs (a later go-ahead with a budget); setting thresholds (S18); extraction
fixtures from real documents (S27); learners other than eligible accounts (S80).

## 1. What is labelled

| Area | Model decision (recorded) | Founder's label |
| --- | --- | --- |
| `grading` | score band of a graded answer: fail < 0.4 ≤ partial < 0.8 ≤ pass | a band |
| `diagnosis` | diagnosis kind on a failed answer (`app/learning/diagnosis.py` kinds) | a kind |
| `intent` | `TurnIntent` of a reply during practice | an intent |
| `source_support` | `Coverage` label on a reply | a coverage label |

Band edges are the report's own convention, stated in the report; they are not product
thresholds. Injection and extraction are not sampled from use (§4).

## 2. Data model and eligibility

**`learners.eval_eligible`** — boolean, default false. Set only by
`uv run poe eval-eligible <email> [--revoke]` (no API, like `grant-admin`). Only eligible
accounts are ever sampled.

**`eval_examples`** — one table for every area:

- `id`, `area` (one of the four), `learner_id` (FK, `ON DELETE CASCADE`),
- `source_kind` + `source_id` — the event or message the example was copied from; unique on
  (`area`, `source_kind`, `source_id`) so a source is copied once,
- `snapshot` (JSON) — the frozen inputs shown to the labeller (question, answer, passages…),
- `decision` (JSON) — the frozen model decision, and the model/prompt identity where recorded,
- `label` (JSON, nullable), `note` (text, nullable), `labelled_by` (FK learners, nullable),
  `labelled_at`, `created_at`, `updated_at`.

Erasing an account deletes its examples (cascade, and covered by the S61 erasure path).
Revoking eligibility keeps existing examples; the portal says so. A relabel overwrites `label`,
`labelled_by` and `labelled_at`; the previous value is not kept (single labeller, v0).

**Area adapters** (`app/services/eval_examples.py` plus one small module per area) each provide:
`candidates(session, limit)` — eligible, not yet copied, source rows; and `freeze(row)` → the
snapshot and decision. Grading draws are stratified across the three bands.

## 3. Admin portal: "Review examples"

- An area list with counts: unlabelled, labelled, candidates available.
- **Draw 10** copies up to ten new candidates (fewer if fewer exist, and says so).
- **Blind labelling:** the API returns an unlabelled example *without* its decision; the
  decision is returned only once a label is saved. The page then shows it beside the label.
- Label controls per area, optional note, Skip, and Relabel on labelled examples.
- Administrator session only (not the ops token), like the publication queue.

**Export:** `uv run poe eval-export <area>` writes labelled examples as JSONL to
`tests/eval/datasets/private/<area>.jsonl` (git-ignored), one line per example with its `id`
and source reference.

## 4. Reports, replay and fixtures

**`uv run poe label-report [--area A] [--replay] [--allow-paid]`** — per area, recorded decision
against label: grading uses Cohen's kappa (`tests/eval/reliability/agreement.py`) and a band
confusion table; the others report accuracy and a confusion table. Every figure shows `n`;
below 20 it prints "too few to read" in place of the figure. No pass mark.

**`--replay`** re-runs the current prompt for each example on its frozen inputs and adds
"current vs label" beside "recorded vs label". It refuses to start unless every model role it
needs resolves to `ollama` or `shaped`, unless `--allow-paid` is given.

**Injection fixtures:** about 20 committed synthetic cases in `tests/eval/cases/injection.json`
— an uploaded passage or memory carrying planted instructions, and a canary string the reply
must not contain. Scored by the same replay path; deterministic structure tests run in
`poe check` on the stand-in model.

**Extraction:** the existing `poe extraction-report` is confirmed to read a fixture directory;
real-document fixtures stay with S27 and the gap is recorded.

## 5. Testing

- Backend: candidates come only from eligible accounts; a source is copied once; snapshots are
  frozen against later edits; the API withholds the decision until labelled; admin-only;
  account erasure removes examples; report figures on known inputs (kappa, accuracy, "too few");
  the replay guard refuses paid providers without `--allow-paid`; eligibility CLI on/off.
- Frontend: component tests for the review tab (blind then revealed, counts, empty states).
- Browser: one journey — an eligible account answers a practice question, the administrator
  draws a grading example, labels it blind, and sees the decision revealed.

**Still unmeasured after this:** every figure until the founder has labelled enough examples;
anything about paid models; learning effect.
