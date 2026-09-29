# Guru — Suggestions Tracker

Last reviewed: 2026-09-29 · Branch state: `feat/workstream-2` (PR #44)

What is still to do, and what is done. [V0_DECISIONS.md](V0_DECISIONS.md) owns v0 scope and the
[delivery sequence](V0_DECISIONS.md#delivery-sequence); [MASTERPLAN.md](MASTERPLAN.md) and
[ROADMAP.md](ROADMAP.md) own mission and phases. History (original findings, superseded rows,
older verification runs) lives in Git.

### How to read it

- Each ID appears in exactly one table. Update that row; don't append narrative.
- **Open**: nothing built yet. **Partial**: a foundation exists and the named remainder is left.
  **Proposed**: suggested, not accepted. **Implemented / Completed**: the bounded software scope
  is closed. **Merged**: tracked under another ID. **Deferred**: outside v0.
- "Implemented" means the code exists and is tested — not that it is educationally validated or
  deployed. Thresholds are uncalibrated until S18/S59 say otherwise.
- Small known defects found in reviews are listed under [Deferred minors](#deferred-minors), not
  in the rows.

## At a glance

| | Count | IDs |
| --- | --- | --- |
| Live, v0 | 19 | S17 S18 S20 S27 S28 S31 S37 S47 S49 S50 S53 S58 S59 S60 S62 S65 S66 S76 S77 |
| Live, proposed (not v0 gates) | 4 | S64 S80 S84 S85 |
| Open questions | 3 | O02 O05 O07 |
| Done | 46 | [Completed](#completed) |

**Next up: workstream 5** — S47 deadlines and cancellation, then S49, S37, S17, S62, S53.

## Live work

Ordered by the delivery sequence. "Remaining" is only what is left; what exists is in the
evidence links and in [Completed](#completed).

### Workstream 5 — Reliability and resource limits (next)

| ID | Item | Status | Remaining | Evidence |
| --- | --- | --- | --- | --- |
| S47 | Whole-request and spend budgets | Partial | Whole-request deadlines and cancellation. (Spend caps are done: exact per-learner admission, deployment ceiling, background pause at 90%, 429 `budget_exceeded`.) Actual alpha caps are an operating decision. | [Spend guard](../app/services/spend_guard.py), [guard tests](../tests/test_spend_guard.py), [RUNBOOK §18](RUNBOOK.md#18-spend-limits-s47-s48) |
| S49 | Provider failure experience | Partial | Predictable learner-facing responses for provider rate limits and outages, with safe retry. Don't reimplement the compatibility contract. | [Registry](../app/llm/registry.py), [turn lifecycle](../app/services/turn.py) |
| S37 | Bounded, resumable ingestion | Partial | Strict concurrency ceilings; a per-job spend ceiling if daily caps prove too coarse; split long extraction/model work into short resumable stages. | [Ingestion service](../app/services/ingestion.py), [job tests](../tests/test_ingestion_jobs.py) |
| S17 | Durable practice lifecycle and restart verification | Partial | Checkpoint schema setup under migrations; verify real process restart/resume races; reconcile expiry with V12; safe onboarding cleanup and graph-state compatibility. | [Checkpoints](../app/services/checkpoints.py), [lifecycle tests](../tests/test_checkpoint_lifecycle.py) |
| S62 | Measured long-history performance | Partial | Measure long-history latency and message-page growth; aggregate further only where measurements justify it. | [Query budgets](../tests/test_query_budgets.py) |
| S53 | Rendering edge cases and accessibility | Partial | Indented code vs LaTeX normalization; keyboard/screen-reader use and panel layout in the browser. | [Rich text](../frontend/src/components/content/RichText.tsx), [browser tests](../frontend/e2e/) |

### Workstream 6 — Evaluation and release gates

| ID | Item | Status | Remaining | Evidence |
| --- | --- | --- | --- | --- |
| S58 | End-to-end and release gates | Partial | Browser coverage for broad sudo, v0 web restriction and durable note editing; verify changed boundaries together and required-check enforcement before release. Two order-dependent failures unfixed (see [notes](#s58-notes)). | [CI](../.github/workflows/ci.yml), [browser journeys](../frontend/e2e/) |
| S59 | Reliability, model quality, and learning evaluation | Partial | Select permitted fixtures/founder examples, add independent grading comparisons and founder labels, measure before setting thresholds. Covers diagnosis, intent, source support, injection, extraction, difficulty. Owns S03/S67/S71/S72. Paid runs wait for data and budget. | [Reliability report](../tests/eval/reliability/report.py), [V14](V0_DECISIONS.md#accepted-product-decisions) |
| S18 | Calibrate estimates and heuristics | Partial | With S59 data: calibrate placement, mastery bar, assistance, detour disproval, scaffolding, transfer seeds, goal freshness, retention spacing and probe days; measure requested vs delivered difficulty; set thresholds for the three stored-but-unread extraction ratios. | [Constants inventory](../tests/eval/reliability/knobs.py), [difficulty report](../tests/eval/reliability/difficulty.py) |
| S20 | Synchronize project documentation | Partial | README's private-ownership claim, CLAUDE's stub-auth/frontend descriptions, roadmap status, OPERATIONS' polling/history gaps. Keep historical milestones historical. | [README](../README.md), [CLAUDE.md](../CLAUDE.md), [ROADMAP](ROADMAP.md), [OPERATIONS](OPERATIONS.md) |

### Workstream 7 — Operated invited alpha

| ID | Item | Status | Remaining | Evidence |
| --- | --- | --- | --- | --- |
| S60 | Deployment, monitoring, backup, recovery | Partial | Choose/configure hosting, TLS, secrets, mail, inference; deliver alerts; database and object backups with 30-day retention and a way to re-erase accounts erased since a backup; demonstrate restore/rollback against the 24-hour loss / 4-hour recovery targets. Also: Clerk application name and `GURU_CLERK_AUTHORIZED_PARTIES` unset; production mail and recovery unverified. | [Release guard](../app/core/release.py), [restore drill](../scripts/backup-drill.sh), [V16](V0_DECISIONS.md#accepted-product-decisions) |
| S65 | Observed first-use sessions | Open | Founder use → observed sessions → independent invited use; record obstacles and repeat use. | [Release objective](V0_DECISIONS.md#release-objective) |
| S66 | Separate learner, reviewer, and sponsor feedback | Open | Keep usability, teaching correctness, learning and commercial interest separate in early-cohort feedback. | S59, S65 |
| S64 | Stable founder-testing configuration | Proposed | Keep one model configuration stable during founder use; distinguish model, product/state and serving failures. | S48, S59 |

### Workstream 3 remainders — Source and teaching quality

The workstream is otherwise closed; these need evaluation data or a real corpus, so they wait for
the testing phase.

| ID | Item | Status | Remaining | Evidence |
| --- | --- | --- | --- | --- |
| S76 | Retrieval correctness at corpus scale | Partial | Gate failure fixed (6c9d51d). Compare exact vs index retrieval on representative permitted corpora before choosing a performance tradeoff (see [notes](#s76-notes)). | [Exact distance](../app/llm/embedding_space.py), [regression tests](../tests/test_retrieval.py), [RUNBOOK §6.5](RUNBOOK.md#65-retrieval-recall--plan-s76--needs-a-live-db-no-model) |
| S27 | Technical extraction quality | Partial | Evaluate equations, tables, code and derivations against V14 fixtures before claiming quality; calibrate ratio indicators (S18). | [Chunking](../app/rag/chunking.py), [extraction report](../tests/eval/extraction/report.py) |
| S28 | Explain source support and insufficiency | Partial | Checker accuracy (S59); lesson coverage once a lesson-block viewer exists. | [Grounding policy](../app/services/grounding.py), [coverage](../app/rag/coverage.py) |
| S31 | Untrusted source and memory boundaries | Partial | Held-out uploaded-document and memory-poisoning cases evaluated under S59. Text filters are not a guarantee. | [Untrusted content](../app/agent/untrusted.py), [injection tests](../tests/test_prompt_injection.py) |
| S50 | Versioned reindexing | Partial | Run `poe reindex` against a real corpus; bump `PIPELINE_VERSION` when S27's extraction work lands. | [Reindex](../app/services/reindex.py), [RUNBOOK §15](RUNBOOK.md) |
| S77 | Duplicate-source recovery | Partial | Evaluate selected scanned/digital pairs. Near matches stay advisory by design. | [Recovery tests](../tests/test_duplicate_recovery.py) |

### Jev integration (proposed, not a v0 prerequisite)

S78, S81, S82 and S83 are implemented with every question **off** by default; none is live and no
shadow results are recorded yet. Sources: [plan](jev-implementation-plan.md),
[architecture](jev-architecture.md), [RUNBOOK §14](RUNBOOK.md).

| ID | Item | Status | Remaining |
| --- | --- | --- | --- |
| S80 | Learner-data eligibility and provider controls | Proposed | Resolve data handling before any learner other than the founder is invited. Not needed for shadow traffic. |
| S84 | Tutoring-move study | Proposed | Phase 6, only after useful intent results. Guru builds the eligible action set; revalidate and fall back to current policy; compare cost and outcomes. Never alters mastery, FSRS or achievement. |
| S85 | Additional decision studies | Proposed | Grounding sufficiency, concept mapping, preference suggestions, per-turn learner signals; candidate `fully_sourced` (shadow first). Optional: a shadow question rating declared-check difficulty (S56). |

### Open questions

| ID | Question |
| --- | --- |
| O07 | Raised 2026-09-27 (S43): "Remember things from my conversations" stops new memories only; profile refresh still reads recent answers and messages (the Account copy says so). Should pausing also stop the profile reading messages? |
| O02 | Cohort payment/pricing arrangement — later operating/business choice. |
| O05 | Funding for broad free access — no amount, source or runway committed. |

## Completed

One line each; the design docs, CLAUDE.md and the RUNBOOK carry the detail. Anything left over is
named in the "Hand-off" column and tracked under that ID.

| ID | Item | What closed it | Hand-off | Evidence |
| --- | --- | --- | --- | --- |
| S01 | Goal-specific achievement | Mastery = ability − 2·uncertainty ≥ 0.5 on measured evidence; achievement recorded per component and kept; goal status reports closed / meets target / achieved / stale. | S18 | [Goal-policy design](superpowers/specs/2026-09-22-goal-policy-design.md) |
| S02 | Explicit learner preferences | Five settings, global + subject overrides, pin their parameter in every mode from the next turn; inferred values shown beside them. | — | [Preferences](../app/services/preferences.py) |
| S09 | Per-component diagnosis | Diagnoses, recurrent patterns, feedback moves, failed-review escalation. | S18, S59 | [Diagnosis](../app/learning/diagnosis.py) |
| S10 | Component evidence and rubrics | Per-component grading, generated rubrics, component scores. | — | [Rubric grading](../app/learning/rubric_grading.py) |
| S11 | Learner-controlled detours | Exploration proposes, guided may take; ends on mastery or disproval (three unassisted passes). | — | [Guidance design](superpowers/specs/2026-09-24-guidance-design.md) |
| S12 | Difficulty targeting | Ability-conditioned targets; difficulty reports. | S18 | [Difficulty](../app/learning/difficulty.py) |
| S13 | Discount assisted/repeated attempts | Assistance and repeat exposure reduce evidence credit. | — | [Assistance](../app/learning/assistance.py) |
| S14 | Delayed retention and transfer evidence | Retention needs two unaided answers `retention_min_days` apart; a taught-first answer isn't unaided; due components get cold retention checks, then transfer checks in the next unpractised catalogue setting; dashboard shows "applied in `<setting>`". Transfer is evidence only. | — | [Retention design](superpowers/specs/2026-09-29-retention-checks-design.md), [transfer design](superpowers/specs/2026-09-29-transfer-evidence-design.md) |
| S15 | Deliberate conversational assessment | Explicit checks, intent routing, help counts, persisted results. | S59 | [Conversation evidence](../app/learning/conversation_evidence.py) |
| S16 | Shared learner context across modes | Shared context composition and memory controls. | — | [Context](../app/services/learner_context.py) |
| S21 | Invited access and account recovery | Hosted identity (Clerk) exchanged once for Guru's session; Guru keeps invitations, admin tier, suspension, audited visits. | S60 (mail, app name, authorized parties) | [RUNBOOK §11](RUNBOOK.md#11-identity-s21) |
| S22 | Persist generated prerequisites | Stable keys, validation, commit revalidation. | — | [Curriculum](../app/learning/curriculum.py) |
| S23 | Graph integrity under concurrent edits | Cycle check + insert under one advisory lock; Curriculum issues panel. | — | [Integrity design §3](superpowers/specs/2026-09-25-integrity-transfer-design.md) |
| S24 | Confirmed cross-subject transfer | Endorsement + learner acceptance; provisional head start confirmed by real answers. | See [deferred minors](#deferred-minors) | [RUNBOOK §13](RUNBOOK.md#13-concept-links-s24) |
| S25 | Private ownership and reviewed publication | One visibility gate with a sweep guard; admin-reviewed frozen snapshots; upload-derived material never publishable. | Residual: pasted source text evades the flag — admin review is the control | [RUNBOOK §12](RUNBOOK.md#12-publication-review-s25b) |
| S26 | Consistent source scope, sources-only | One `resolve_scope`; opt-in untagged sources; General chat reads none. | — | [Scope](../app/rag/scope.py) |
| S29 | Citations survive re-ingest | Supersede instead of delete; confirmed re-processing. | — | [Pipeline](../app/rag/pipeline.py) |
| S30 | Disable URL intake for v0 | URL import/retry rejected; tutor searches stored material only. | S58 (browser) | [Web-policy tests](../tests/test_v0_web_policy.py) |
| S32 | Bind onboarding state to learner | Durable ownership checked against identity. | S17 (cleanup) | [Onboarding](../app/services/onboarding_sessions.py) |
| S33 | Assessment authorship and privacy | Generated items/rubrics private with owner checks. | — | [Privacy tests](../tests/test_assessment_privacy.py) |
| S34 | Concurrency-safe evidence and retries | State rows locked in sorted order; retried turns reuse the attempt id. | — | [Integrity design §2](superpowers/specs/2026-09-25-integrity-transfer-design.md) |
| S35 | Repair plans after assessments | Pending revision state. | — | [Plan service](../app/services/lesson_plan.py) |
| S36 | Recover stranded ingestion | Reconciliation, leases, finite attempts. | S60 (alert delivery) | [Reconciler](../app/workers/reconcile.py) |
| S38 | Note catch-up cursors | Separate bounded message/event cursors. | — | [Notes](../app/services/notes.py) |
| S39 | Topic/concept provenance in notes | Topic-scoped distillation, validated references. | — | [Distillation](../app/learning/note_distill.py) |
| S40 | Preserve exact note edits | Authored Markdown kept; revisions and guarded restore. | S58 (browser) | [Notes](../app/services/notes.py) |
| S41 | Bounded note rewrites | Tail rewriting; deterministic fallback render. | — | [Distillation](../app/learning/note_distill.py) |
| S42 | Memory correction and supersession | Explicit same/updates/coexists judgement, shown with Undo; durable forgetting. | S18 (distance threshold) | [Supersession](../app/memory/supersession.py) |
| S43 | Refresh scheduling | Quiet-period worker sweep; recency window; estimators pay only on change; pause memory. | S18 (windows), O07 | [RUNBOOK §17](RUNBOOK.md#17-refresh-scheduling-s43) |
| S44 | Honest profile proxies | Proxy naming, narrower inference. | S59 | [Estimators](../app/learning/profile_estimators.py) |
| S45 | Count attempts separately | Attempt identity in aggregation. | — | [Analytics](../app/services/analytics.py) |
| S46 | Honest mastery displays | Ability, uncertainty and outcomes shown separately. | — | [Dashboard](../frontend/src/pages/Dashboard.tsx) |
| S48 | Call accounting and attribution | Every call recorded pending → settled; `@metered` attribution; cost by feature. | S60 (reconcile with provider billing) | [Meter](../app/llm/meter.py) |
| S51 | Interrupted-turn lifecycle | Turn states, safe retries, stale-turn recovery. | S17 (restart) | [Turn](../app/services/turn.py) |
| S52 | Pause, resume, skip practice | Intent gate on every reply; explicit resume/skip. | — | [Guidance design §4](superpowers/specs/2026-09-24-guidance-design.md) |
| S54 | Flashcard reveal and self-rating | Think, reveal, rate; rating schedules review only. | — | [Flashcard panel](../frontend/src/components/lessons/FlashcardPanel.tsx) |
| S55 | Validate source scope, refresh tags | Reassignment validated, tags refreshed. | — | [Source API tests](../tests/test_sources_api.py) |
| S56 | Evidence kinds and reproducible grading | Server-derived judged vs self-rated; `grading` block + frozen snapshots on every graded event; `poe regrade` reports agreement; declared checks get criteria and a difficulty band. Legacy events stay unreplayable. | S85 (optional band question) | [RUNBOOK §19](RUNBOOK.md), [provenance design](superpowers/specs/2026-09-28-grading-provenance-design.md), [criteria design](superpowers/specs/2026-09-28-declared-check-criteria-design.md) |
| S57 | Effective sweep settings | Supported knobs apply; unsupported fail; settings recorded. | S59 | [Sweep tests](../tests/eval/test_sweep_runner.py) |
| S61 | Archive, delete, forget, export, retention | Three removal actions; account deletion with 7-day recovery then full erase; diagnostic rows expire at 30 days. | S60 (backups) | [RUNBOOK §16](RUNBOOK.md) |
| S63 | Show the whole goal | Lessons page shows window, deferred objectives and status. | — | [Status bar](../frontend/src/components/lessons/GoalStatusBar.tsx) |
| S78 | Jev decision contract and adapter | Sole SDK importer, off by default, fake client. | — | [Decisions](../app/llm/decisions.py) |
| S81 | Shadow turn read | `intent` + `fully_correct` per check, never changes the outcome. | — | [Turn read](../app/learning/turn_read.py) |
| S82 | Jev accounting and report | `decision_calls`, pricing, `poe decision-report`. | — | [Report](../app/services/decision_report.py) |
| S83 | Live switch and rollback | Per-question live switch with threshold and deadline fallback; never fails an answer. | — | [Decisions service](../app/services/decisions.py) |
| P10 | Admin portal and audited sudo | Reason-required short visits, durable audit, live revocation; off by default. | S58 (browser) | [Impersonation](../app/services/impersonation.py) |

**Merged or superseded:** S03 → S59 · S08 (shared acceptance criterion: diagnose, teach, apply
independently, revisit) · S19 (standing principle: keep the modular app, role seam, continuous
estimator, FSRS) · S67 → S59/S65 · S79 → superseded by `poe decision-report` on real traffic.

## Deferred and not accepted

Outside v0; none is an engineering gate. URL ingestion, DKT, billing, younger tiers, LMS work,
native clients and offline use are also out of v0.

| ID | Item | Status |
| --- | --- | --- |
| S04 | Value, continued use and economics before expansion | Deferred (revisit with usage evidence) |
| S05 | Revalidate teaching for new populations | Deferred |
| S06 | Institutional integration vs full LMS (O06) | Deferred |
| S07 | Evaluate the flipped-classroom arrangement | Deferred (institutional pilot) |
| S68 | Position on observed comparative value | Proposed |
| S69 | Compare alternatives during founder use | Proposed |
| S70 | Learning habits and switching reasons | Proposed |
| S71 | Reusable reviewed learning cases (→ S59) | Proposed |
| S72 | Compare learning, effort, retention, return (→ S59/S65) | Proposed |
| S73 | Full value to a reachable audience | Proposed |
| S74 | Separate sustainability, expansion, acquisition criteria | Proposed |
| S75 | Keep acquisition optional | Proposed |

**Resolved decisions:** O01 invited experienced adults, subjects unrestricted · O03 direction by
V02 (thresholds are S18/S59) · O04 calibration and grading reliability first · O06 deferred under
S06.

**Standing direction (D01–D12):** durable, independently usable understanding (D01); experienced
adults first (D02); knowledge relative to domain and goal, no global score (D03); learner-led with
adjustable guidance (D04); address forgotten prerequisites without disrespecting capability (D05);
sustainability serves access (D06); institutions are a later route (D07); large gains need
Guru-specific evidence (D08); evolve through use (D09); coordination process note (D10); founder
use first (D11); founder time available, funding unspecified (D12).

## Deferred minors

Small defects found in final reviews and left on purpose. Pick them up when working nearby or in
the testing phase.

**S02** — a database error reading settings still fails the turn; a subject following global
"Adapt to me" doesn't show what adaptation chose; a failed save isn't shown; downgrading 0068
loses later guidance changes; no query-budget test on the tutor turn.

**S14** — every plan revision reads all component states and evidence (add a `kc_ids` filter);
`passed_since` repeats the taught-first condition; no `revise_steps` test that a flagged step
closes, nor an end-to-end check answered after a side discussion; checks have no queue cap (every
component answered once becomes a SMART-graded check within a week); practice can serve a question
generated for a transfer check, using up its setting; publishing doesn't copy `items.setting`;
`correct` is item-level, so on a multi-component item a weak component can show transfer; no test
of `/reviews/due` precedence when both checks are due, nor of the session-surface transfer branch.

**S24** — the judge runs only when a subject is committed; candidates re-scan on each Lessons load;
an external step doesn't link to the other subject's plan; check-first doesn't apply in tutor chat;
differently named equivalents are never found; a foreign prerequisite's own prerequisites aren't
traversed.

**S27** — a stray `$$` or info-string fence line can open a block; a heading alone can become a
tiny chunk.

**S42** — the candidate text isn't fenced as untrusted (only neighbours are); after Undo the
restored memory reads "Replaced: …" naming the rejected one; Undo's error copy blames a changed memory for
any failure and a 409 doesn't refresh; `replaced_by` orders by creation, not replacement time; the
cross-learner test doesn't cover the undo route.

**S43** — claims commit before queuing, so a broker error leaves the rest claimed but unqueued; a
manual write-back can overlap a scheduled one; fingerprints carry no prompt version; an empty paid
estimator result is re-paid; the event window counts non-observation events; the sweep's GROUP BYs
want an index at scale; the write-back cursor can skip messages at a window boundary inside one
transaction; the Account switch shows on while loading; missing tests for a failed write-back
keeping its claim and two-session claim races.

**S47** — the report calls over-budget at `>` while the guard refuses at `>=`; a failed
deployment-total read lets that one call through unrecorded; reservations ignore images and tool
schemas; admission locks and sums even with learner limits off; no two-connection lock test.

**S48** — the extraction adapters' `usage_log` is dead code; the per-request attribution test
orders by a timestamp that can tie.

**S56** — `poe regrade` counts pre-v5 events toward `--limit` and loads the whole window first; an
admin's flashcard answer counts as not re-gradable instead of skipped; a grading path that forgets
`provenance` records `auto`; failures aren't broken down by kind; `--model` isn't validated; no
tests for a rubric on one KC of a two-KC item, the re-graded user message, or the race loser
writing no snapshots; the Jev turn read starts before a first attempt's criteria exist;
`ensure_criteria` takes the first of unordered `kc_links`; the item row stays locked across the
criteria and grading calls; the refusal test raises from the provider, not the spend guard.

**S61** — slice A: Delete is allowed when the impact report failed; `kc_coverage` counts archived
chunks; the message box can flash before the archived banner; guided practice has no archived
banner. Slice B: two workers can take the same pending erasure (needs `SKIP LOCKED`); a restore
racing an erase answers 500; Clerk sessions on other devices survive deletion; only `/auth/me`
routes to recovery; `DELETE /me?now=true` omits the report; downloads read whole files into memory
and a store outage answers 404; delete-with-forget still shows "memories stay".

**S77** — the stranded-duplicate sweep has no batch limit; `requeued` includes `recovered`.

## Notes

### S58 notes

- Order-dependent failures (unfixed): the admin journey asserts "Not measured" for completion
  latency, true only on an e2e database with no non-streamed call in 24 hours; a mastery assertion
  is clock-skew sensitive.
- "Green locally, red in CI" is the symptom to look for: two suites once depended on the developer
  machine (a real object store; the Clerk key in `.env.local`) and now default to CI's environment.
- Dev and CI run RustFS 1.0.0 (MinIO stopped publishing public images on 2026-09-26).
- Any new LLM prompt a browser journey reaches needs a shape in `app/llm/providers/shaped.py`.

### S76 notes

Historical synthetic measurements (2026-09-08, hash-derived near-uniform vectors — not production
benchmarks). Exact search cost about 4 µs per owned chunk (185 ms at 45,000). For 40,050 chunks,
top 50:

| Query shape | Recall vs exact | Latency |
| --- | --- | --- |
| Scoped production query, exact | 50/50 | 162 ms |
| Index, `ef_search=40` | 44% | 1.4 ms |
| Index, `ef_search=1000` | 86% | 15.0 ms |

They justify measuring the tradeoff on a real corpus, not changing retrieval.

### Latest verification — 2026-09-29

On `feat/workstream-2` (PR #44), locally: `poe check` 2574 passed; format check, `db-check` and API
contract clean; frontend 206 tests, build and lint pass. Each piece ended with a whole-branch
review; Critical/Important findings were fixed test-first. Not run: browser journeys, real
providers, worker processes. Educational effect is not established by any of this (S18, S59).
