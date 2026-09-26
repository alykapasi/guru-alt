# Guru — Suggestions Tracker

Last reviewed: 2026-09-26

Repository snapshot: `a318a2f` (branch `fix/auth-ui`, PR #40, not yet merged to `main`)

## Purpose and maintenance

Track remaining work against the current repository. [V0_DECISIONS.md](V0_DECISIONS.md) is the
source of accepted v0 scope and delivery order; it overrides conflicting historical proposals.
[MASTERPLAN.md](MASTERPLAN.md) retains the mission and architecture, and
[ROADMAP.md](ROADMAP.md) retains the phased plan.

- Keep each suggestion ID in exactly one register row. Update that row instead of appending a
  second narrative. Cross-reference another ID when it owns the remaining work.
- Use the same fields throughout: **ID, item, status, next step or closure, evidence**.
- **Open** means work remains without an implemented solution. **Partial** means a foundation
  exists but the stated follow-up remains. **Implemented** closes the bounded software scope,
  with any separate follow-up named. **Merged** points to the authoritative entry or decision.
  **Deferred** is outside v0; **Proposed** remains an unaccepted suggestion.
- Status describes delivery, not approval or educational effectiveness. Acceptance comes from
  the decision record; grouping a proposal does not approve it. No item here is educationally
  validated merely because code or tests exist.
- Evidence links identify current code/test sources or explicitly proposed designs. This review
  inspected those sources; it did not rerun application tests, use paid models, or verify a deployed
  environment. Linked vendor research retains its recorded date and was not refreshed here.
- Original findings, branch histories, mutation anecdotes, and superseded requirements remain
  in Git history. They are omitted here so fixed problems do not get scheduled again.

## Current scope and priorities

Build for independent use by invited adults after founder testing, with unrestricted subjects.
Generated material is private by default. URL imports and tutor web access are disabled for v0.
Alpha administrators have broad, authenticated, audited access when enabled; learner opt-in and
read-only support access are superseded. Durable learner work must survive routine cleanup.

Follow the seven workstreams below in the [accepted delivery sequence](V0_DECISIONS.md#delivery-sequence).
The retrieval gate failure (S76) is explained and fixed; its exact-versus-index performance
tradeoff and the evidence semantics (S54/S56) must stay visible even where their surrounding
infrastructure is already implemented.
Provider choices, paid-evaluation budgets, alpha caps, hosting, domain, and mail configuration remain
deferred operating choices; local implementation and deterministic verification can continue.

The [Jev integration sequence](#jev-integration) is tracked separately as proposed work. It does
not replace the accepted v0 sequence or make Jev a prerequisite for delivering it.

## Active v0 work

### 1. Identity, private ownership, and publication

The tracked software items (S21, S25) are closed and recorded under
[completed work](#completed-and-consolidated-work); S33 and P10 closed earlier.

A real Clerk application is now configured and exercised from this checkout: the secret key is
accepted, a malformed token is refused without a network call, and a well-formed token with an
unknown signing key is rejected as a bad token with the signing key resolved locally. That first
live run found a defect nineteen fake-backed tests had not — see S21 — which is the argument for
doing it before the remaining provider work rather than after.

Still open, and operating configuration rather than code: production mail and account recovery are
unverified in a real deployment (S60), the provider's application name is unset so its emails are
sent from "My Application", and `GURU_CLERK_AUTHORIZED_PARTIES` is unset, which is fine against a
development instance and must be explicit before production.

### 2. Learning evidence, goals, and guidance

Delivered in four slices, each with its own design and plan under
[docs/superpowers/](superpowers/) and a whole-branch review: evidence kinds (S56/S54), goal policy
(S01/S14/S63), guidance (S11/S52), and integrity and transfer (S34/S23/S24). Eight rows are closed
and recorded under [completed work](#completed-and-consolidated-work). The two below were split
deliberately and keep their remaining halves. All new thresholds are uncalibrated and listed
under S18.

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S14 | Independent delayed retention and transfer evidence | Partial | Consumed half done: retention now needs two unaided demonstrations rather than a time span, and goal status reads that evidence. The former `transfer_shown` claim (two item ids) was removed, not repaired. Remaining: schedule delayed independent probes at a chosen interval instead of waiting for FSRS to surface one ("slice 2b"), and reintroduce transfer only once items can be shown to differ in a way the system can point at. Cross-subject reuse (S24) is a different thing and does not close this. | [Goal-policy design §4, §11](superpowers/specs/2026-09-22-goal-policy-design.md), [evidence summaries](../app/learning/mastery.py), [exposure tests](../tests/test_item_exposure.py) |
| S56 | Evidence kinds and reproducible grading history | Partial | Evidence-kind half done: the server derives whether a score was judged or self-reported, self-ratings update retention scheduling only, and analytics, the profile's effort/help questions and notes read the distinction. Remaining, each its own slice: immutable item/rubric/prompt versions threaded through grading and replay, and explicit rubrics and difficulty targets for declared conversational checks. Legacy events without sufficient data stay explicitly unreplayable. | [Evidence-kinds design](superpowers/specs/2026-09-21-s56-evidence-kinds-design.md), [assessment service](../app/services/assessment.py), [evidence-kind tests](../tests/test_evidence_kinds.py), [replay tests](../tests/eval/test_datasets_replay.py) |

### 3. Source scope, teaching quality, and retrieval

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S26 | Consistent source scope and sources-only mode | Completed | One scope rule (`resolve_scope`, `app/rag/scope.py`) for lessons, chat, practice, agent search, onboarding and debug retrieval. Untagged sources are opt-in per subject; sources-only is a per-subject switch (`PATCH /subjects/{id}/source-settings`, Sources panel on Lessons). A General chat reaches no library at all, closing the agent-search leak. Lessons in sources-only with nothing retrieved are refused (422) before any model call. | [Scope](../app/rag/scope.py), [scope tests](../tests/test_scope.py), [settings tests](../tests/test_source_settings.py), [content tests](../tests/test_content.py) |
| S27 | Technical extraction quality | Partial | Structure preservation and damage indicators exist. Evaluate known-correct equations, tables, code, and derivations; handle long structured blocks and surface uncertainty downstream. Select suitable fixtures under V14 before making extraction-quality claims. | [Chunking](../app/rag/chunking.py), [quality indicators](../app/rag/extraction_quality.py), [adapter tests](../tests/test_doc_adapters.py), [extraction report](../tests/eval/extraction/report.py) |
| S28 | Explain source support and insufficiency | Partial | One grounding policy (`app/services/grounding.py`) for chat, practice and agent turns covers passages vs nothing retrieved and normal vs sources-only, including disagreeing passages and naming what goes beyond the sources. Each reply stores `grounding_count`; the API derives `coverage` (cited / searched but not used / not from your materials) and the chat shows it. Remaining: checker accuracy (S59), and lesson coverage once a lesson-block viewer exists. | [Grounding policy](../app/services/grounding.py), [coverage](../app/rag/coverage.py), [support checker](../app/learning/citation_support.py), [grounding tests](../tests/test_grounding.py), [coverage tests](../tests/test_coverage.py) |
| S29 | Preserve historical citations across source revisions | Partial | Cache keys include exact prompts, model specification, and grounding IDs. Reingestion still replaces chunks: preserve usable historical references or explicitly represent their unavailability. New cache keys alone do not repair old citations. Do not delete durable learning artifacts merely because they are old. | [Cache identity](../app/services/content.py), [reingestion](../app/rag/pipeline.py), [content tests](../tests/test_content.py) |
| S31 | Untrusted source and memory boundaries | Partial | Untrusted-content fencing and v0 web restrictions exist. Add held-out uploaded-document and memory-poisoning cases and evaluate the remaining model boundary under S59. Do not retain active-web exfiltration requirements as v0 work or mistake text filters for a guarantee. | [Untrusted-content handling](../app/agent/untrusted.py), [memory extraction](../app/memory/extraction.py), [injection tests](../tests/test_prompt_injection.py), [web-policy tests](../tests/test_v0_web_policy.py) |
| S50 | Versioned reindexing and embedding migration | Partial | Embedding-space identity prevents mixed-vector retrieval. Add a resumable reindex/re-embedding path with extraction/chunking versions and explicit historical-reference handling. Validate or repair legacy source scope during migration; a manual source reset is not a complete migration workflow. | [Embedding identity](../app/llm/embedding_space.py), [retrieval](../app/rag/retrieval.py), [source reset](../app/services/ingestion.py), [retrieval tests](../tests/test_retrieval.py) |
| S76 | Retrieval correctness and corpus-scale behavior | Partial | Gate failure explained, reproduced and fixed (6c9d51d): every learner-scoped vector search ordered by the bare distance, so whenever table statistics favoured it the planner used the HNSW index, which returns the ~40 nearest vectors in the whole table *before* the learner filter; crowded by other learners' (or dead) rows it returned some or none of the learner's own. With the expected chunk missing, a distractor ties it at 1/61 in the fusion and wins on order — the gate's corpus then scores exactly the recorded 0.25. Chunk retrieval, memory retrieval and extraction's duplicate lookup now order by an exact expression the index cannot serve; each has a test that crowds the index and forces its plan (all three returned nothing before). Remaining: compare exact and index-based retrieval on representative permitted corpora before choosing a performance tradeoff (the historical synthetic measurements below). | [Retrieval gate](../tests/eval/test_eval.py), [exact distance](../app/llm/embedding_space.py), [regression tests](../tests/test_retrieval.py), [recall/plan experiment](../tests/eval/retrieval/recall.py) |
| S77 | Duplicate-source recovery and evaluation | Partial | Exact-byte/text deduplication, shared blobs, and suggestion-only near matching exist. Recover a text duplicate whose original disappears, and evaluate selected scanned/digital pairs. Keep near matches advisory; automatic near-duplicate deletion is not a missing feature. | [Duplicate suppression](../app/rag/pipeline.py), [source recovery](../app/services/ingestion.py), [text-dedup tests](../tests/test_text_dedup.py), [similarity tests](../tests/test_simhash.py) |

### 4. Durable learner work and controls

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S02 | Explicit learner preferences | Open | A per-plan guidance setting (guided/exploration) now exists under S11 and is the column a global default would seed. Add global defaults and subject overrides for guidance and presentation. Explicit settings must constrain adaptation; inferred preferences must remain inspectable and resettable. Shared tutor context and profile reset already exist. This owns the remaining preference work from S16/S44. | [Learner context](../app/services/learner_context.py), [profile API](../app/api/v1/profile.py), [accepted V09](V0_DECISIONS.md#accepted-product-decisions) |
| S42 | Durable memory correction and supersession | Partial | Correction, soft forgetting, and suppression exist. Replace similarity-only supersession with a justified distinction between contradiction and coexistence. Source/conversation deletion with optional forgetting and recomputation belongs to S61. | [Memory service](../app/services/memory.py), [memory lifecycle tests](../tests/test_memory_lifecycle.py), [memory UI](../frontend/src/pages/Memory.tsx) |
| S43 | Incremental and explicit refresh scheduling | Partial | Watermarks skip unchanged evidence, but profile recomputation still reads whole history and refresh/write-back is request-triggered. Add a deliberate trigger, catch up unattended backlogs, and make recomputation incremental while respecting explicit preferences and admin attribution. Measure growth under S62. | [Profile service](../app/services/profile.py), [memory service](../app/services/memory.py), [worker tasks](../app/workers/tasks.py), [cursor tests](../tests/test_refresh_cursors.py) |
| S61 | Archive, delete, forget, export, and retention | Partial | Export, immediate erasure, shared-blob reference protection, and some expiry loops exist. Implement V11/V12: distinct archive/delete/forget actions with provenance and recomputation; immediate access disable, seven-day recovery, and erase-now; selective 30-day diagnostic/backup retention; retryable orphan cleanup; usable export access to uploaded files. Preserve durable learning history, learner edits, and suppression records. | [Retention API](../app/api/v1/retention.py), [retention service](../app/services/retention.py), [retention tests](../tests/test_retention.py), [shared-blob tests](../tests/test_blob_sharing.py) |

### 5. Reliability and resource limits

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S17 | Durable practice lifecycle and restart verification | Partial | PostgreSQL checkpoints, advisory locks, durable onboarding ownership, and revalidation exist. Move checkpoint schema setup into a controlled migration lifecycle, verify actual process restart/resume races, and reconcile expiry with V12's durable-work policy. Define safe onboarding cleanup and graph-state compatibility. Durability alerts already exist. | [Checkpoint setup](../app/agent/checkpointing.py), [lifecycle service](../app/services/checkpoints.py), [durability tests](../tests/test_durable_state.py), [lifecycle tests](../tests/test_checkpoint_lifecycle.py) |
| S37 | Bounded, resumable ingestion | Partial | Visible jobs, leases, attempts, timeouts, and character/chunk caps exist. Make required concurrency ceilings strict, add per-job spend enforcement, and split long extraction/model work into short resumable stages. Share spend semantics with S47. | [Ingestion service](../app/services/ingestion.py), [pipeline](../app/rag/pipeline.py), [job tests](../tests/test_ingestion_jobs.py) |
| S47 | Whole-request and spend budgets | Partial | Input/history limits and rolling pre-turn budgets exist. Enforce caps across paid operations and concurrent requests, with whole-request deadlines and cancellation. The current chat preflight explicitly permits overshoot; resource proxies are not monetary ceilings. Actual alpha caps remain an operating decision. | [Budget service](../app/services/budget.py), [chat boundary](../app/api/v1/chat.py), [budget tests](../tests/test_chat_budget.py) |
| S48 | Complete call accounting and feature attribution | Partial | Independent logging, unknown-price handling, embedding accounting, completion latency, and streaming first-token timing exist. Record failures/partial calls and request/prompt/version provenance; report cost by feature. Reconcile against provider billing when operating. Interrupted-stream accounting from S51 belongs here. | [Call logging](../app/services/llm_log.py), [registry](../app/llm/registry.py), [logging tests](../tests/test_llm_log.py), [failure tests](../tests/test_fault_injection.py) |
| S49 | Provider failure experience | Partial | Capability checks, SDK timeouts/retries, malformed-tool handling, and stream closure exist. Give learners predictable rate-limit and provider-unavailable responses with safe retry behavior; do not reimplement the compatibility contract. | [Registry](../app/llm/registry.py), [tool-use tests](../tests/test_llm_tool_use.py), [turn lifecycle](../app/services/turn.py) |
| S53 | Technical rendering edge cases and accessibility | Partial | Math, tables, code blocks, citations, and responsive panels exist. Check indented code against LaTeX normalization and verify keyboard/screen-reader use and panel layout in the browser. Syntax coloring and Markdown transformation of the learner's own messages are not required fixes. | [Rich text](../frontend/src/components/content/RichText.tsx), [rendering tests](../frontend/src/components/content/RichText.test.tsx), [browser tests](../frontend/e2e/) |
| S62 | Measured long-history performance | Partial | Batched mastery/notes reads, query-count budgets, and transcript pagination exist. Measure representative long-history latency and message-page growth; make further aggregation changes only where measurements justify them. Incremental profile refresh is owned by S43. | [Query budgets](../tests/test_query_budgets.py), [transcript queries](../app/services/chat.py), [history tests](../tests/test_chat_budget.py) |

### 6. Evaluation and release verification

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S18 | Calibrate estimates and teaching heuristics | Partial | A constants inventory and synthetic difficulty/calibration instruments exist. Workstream 2 added more uncalibrated rules, all in the inventory: the mastery bar (ability − 2·uncertainty ≥ 0.5, k=2 per V02, about twelve correct medium answers from a cold start), retention's minimum spacing, goal-evidence freshness, three unassisted passes to disprove a detour, and a transferred head start's uncertainty floor (0.6) and confirming passes (2). With suitable data under S59, calibrate placement, mastery, assistance, detour, scaffolding, transfer, and goal-duration/freshness rules. Measure requested versus delivered generator difficulty; unrestricted subjects do not justify assuming a common repeated-item cohort. | [Constants inventory](../tests/eval/reliability/knobs.py), [difficulty report](../tests/eval/reliability/difficulty.py), [difficulty tests](../tests/eval/test_reliability_difficulty.py) |
| S20 | Synchronize remaining project documentation | Partial | This tracker is reconciled; other documents still contain stale implementation descriptions. Correct README's remaining private-ownership claim, CLAUDE's stub-auth/frontend descriptions, historical roadmap status, and OPERATIONS' polling/history gaps. Preserve historical milestones as historical. | [README](../README.md), [repository guidance](../CLAUDE.md), [roadmap](ROADMAP.md), [operations](OPERATIONS.md) |
| S58 | Current end-to-end and release gates | Partial | Backend/frontend, contract, real Redis, cross-connection, failure-injection, migration-data, and browser test infrastructure exists. S76's gate failure is fixed; add browser coverage for broad sudo, v0 web restriction, and durable note editing; verify newly changed boundaries together and required-check enforcement before release. Current branch protection was not inspected. Two order-dependent browser/suite failures are recorded and unfixed: the admin journey asserts "Not measured" for completion latency, which only holds while the shared e2e database has recorded no non-streamed call in 24 hours, and a mastery assertion is sensitive to clock skew. Sign-in and curriculum journeys now run against the hosted-identity path. Two suites that depended on the developer's machine rather than on the code are fixed, both found by the environment disagreeing with CI rather than by review: the backend suite reached a real object store on an upload path and passed only where `docker compose` was up, and the frontend suite read the Clerk key out of `.env.local`, so configuring the provider turned three passing tests red while CI stayed green. Both now default to the environment CI runs. Treat "green locally, red in CI" as the symptom to look for, not a flake. On 2026-09-26 the browser-journeys job broke on an upstream change: MinIO stopped publishing public images (`minio/minio` gone from Docker Hub, quay.io answering anonymous pulls with 401), so dev and CI now run RustFS 1.0.0 with the bucket created by the official aws-cli image, and `main` needs the same change. The same run found the journeys' stand-in model had no answer for S52's intent gate, so every typed answer paused practice instead of being graded; a stand-in shape and a prompt-coupling test now cover it. Any new LLM prompt a journey reaches needs a shape in `app/llm/providers/shaped.py`, or the journey fails in the browser rather than in a unit test. | [CI](../.github/workflows/ci.yml), [queue tests](../tests/test_queue_integration.py), [migration tests](../tests/test_migrations_with_data.py), [browser journeys](../frontend/e2e/) |
| S59 | Reliability, model quality, and learning evaluation | Partial | Report tools exist. Select traceable permitted fixtures/founder examples, add independent grading comparisons and founder labels, and measure reliability before setting thresholds. Include diagnosis, conversational intent, source support, injection, extraction, and difficulty cases. Live/paid runs wait for data and budget; later retention/transfer/outcome studies remain distinct from software gates. Owns S03/S67/S71/S72 evaluation overlap. | [Reliability report](../tests/eval/reliability/report.py), [agreement metrics](../tests/eval/reliability/agreement.py), [report tests](../tests/eval/test_reliability_report.py), [V14](V0_DECISIONS.md#accepted-product-decisions) |

### 7. Operated invited alpha

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S60 | Deployment, monitoring, backup, and recovery | Partial | Packaging, production guards, readiness, alert polling/history, admin dashboard, and local restore tools exist. Choose and configure hosting/TLS/secrets/mail/inference; deliver alerts; configure database and object backups with the accepted retention policy; demonstrate restore/rollback against the 24-hour loss and four-hour recovery targets. Then undertake founder use and observed invited sessions. Deployment and paid choices remain deferred until the product is ready. | [Release guard](../app/core/release.py), [alert history](../app/services/alert_history.py), [restore drill](../scripts/backup-drill.sh), [blob check](../app/workers/blob_check.py), [V16](V0_DECISIONS.md#accepted-product-decisions) |
| S64 | Stable founder-testing configuration | Proposed | During S60's founder stage, keep an initial model configuration stable and distinguish model, product/state, and serving failures. Configuration and monetary limits have not been selected. | [Accepted inference approach](V0_DECISIONS.md#accepted-product-decisions), S48, S59 |
| S65 | Observed first-use sessions before a longer pilot | Open | Accepted approach: sustained founder use, then observed sessions, then independent invited use when the exposed capabilities are ready. Record observed obstacles and repeat-use behavior. | [Release objective](V0_DECISIONS.md#release-objective), S58–S60 |
| S66 | Separate learner, reviewer, and sponsor feedback | Open | Accepted approach: keep evidence about usability, teaching correctness, learning, and commercial interest separate when gathering feedback from the early cohort. | S59, S65 |

## Jev integration

Source: the [implementation plan](jev-implementation-plan.md),
[architecture proposal](jev-architecture.md), and [capability snapshot](jev-capabilities.md),
recorded 2026-09-18. The `typesafe-sdk` dependency is declared in [pyproject.toml](../pyproject.toml)
and locked to 0.6.0 in [uv.lock](../uv.lock). No Jev adapter, `DecisionClient`, decision configuration,
or Jev evaluation suite exists in the current application. The existing
[FAST intent classifier](../app/learning/conversation_evidence.py) remains authoritative.

Track the plan in the order below. Synthetic contract work can proceed independently of unrelated
learner-evidence repairs. Learner-connected evaluation requires the relevant privacy and caller
checks; studies using ability or downstream learning outcomes also depend on S54/S56. S78, S81, S82
and S83 are implemented (2026-09-26) with every question off by default. No question has been
switched live, and no shadow results are recorded here yet.

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S78 | Jev decision contract and isolated adapter | Completed | `DecisionClient` + Guru-owned types, `TypeSafeDecisionClient` (sole SDK importer, no retries, SDK logger pinned at WARNING), `FakeDecisionClient`, settings off by default, startup refuses a mode that is on without a key. Opt-in smoke test `GURU_JEV_SMOKE=1`. | `app/llm/decisions.py`; `tests/test_decision_client.py`; 759b578, b8e47a2 |
| S79 | Offline answer-intent comparison | Superseded | Replaced by the shadow report on real founder traffic (`uv run poe decision-report`). A hand-labelled set remains a possible later check. | spec 2026-09-26-jev-turn-read §9 |
| S80 | Jev learner-data eligibility and provider controls | Proposed | Re-scoped: resolve the data-handling questions before any learner other than the founder is invited. Not a precondition for shadow traffic. See RUNBOOK §14. | spec §1, §4 |
| S81 | Bounded shadow intent execution | Completed | Broadened from intent-only to the turn read: one request per conversational check asks `intent` + `fully_correct`. The practice gate and direct submissions ask their own. Shadow never changes the outcome and writes in the background. | `app/learning/turn_read.py`, `app/services/decisions.py`; `tests/test_decisions.py`, `tests/test_decision_routes.py` |
| S82 | Jev accounting and diagnostic lifecycle | Completed | `decision_calls` (own transaction, `request_id` so a shared request is costed once, no learner text), Jev pricing, `uv run poe decision-report` (false passes, harmful intent directions, savings, latency, spend). | `app/models/decision.py`, `app/services/decision_report.py`; `tests/test_decision_log.py`, `tests/test_decision_report.py` |
| S83 | Gated live intent experiment and rollback | Completed | Redefined: a manual per-question `live` switch with threshold and deadline fallback, replacing the invited-cohort experiment. Jev never fails an answer. | `app/services/decisions.py`; live tests in `tests/test_decisions.py` |
| S84 | Optional Jev tutoring-move study | Proposed | Phase 6, only after useful intent results: let Guru construct the eligible action set from learner preferences, guidance, prerequisites, active attempts, and source scope. Zero/one eligible actions need no model call; revalidate a returned choice before applying it and fall back to current policy on failure. Compare incremental cost and pedagogical outcomes against the existing policy. Depends on relevant S02/S11/S24/S52/S56 work; does not alter goal achievement, mastery mathematics, or FSRS. | [Phase 6](jev-implementation-plan.md#phase-6--optional-bounded-tutoring-policy-study), [policy boundaries](jev-architecture.md#gates-boundaries-and-fallback), [session runner](../app/services/session_runner.py) |
| S85 | Optional additional Jev decision studies | Proposed | The grading second opinion moved into the turn read as `fully_correct`. The rest (grounding sufficiency, concept mapping, preference suggestions) remains later work. Per-turn learner signals are the next slice. Candidate from S28: `fully_sourced`, which would split the coverage label into full vs partial; shadow first. | spec §6 |

The proposal keeps prose generation, retrieval, DSPy, authorization, mastery estimation, and FSRS
in their existing components. Shadow comparisons establish decision-quality evidence, not durable
learning gains; any later causal learning claim requires its own outcome study under S59.

## Completed and consolidated work

These rows close the stated implementation or remove duplicate tracking. Referenced active items
retain the follow-up work; closure here is not a claim that the complete v0 product is ready.

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S01 | Goal-specific achievement and closure | Implemented | Mastery is judged on the conservative estimate ability − 2·uncertainty ≥ 0.5 (V02's k=2) and only on a component with measured evidence, so an unmeasured placement seed can never count. Achievement is recorded once per component and kept. Goal status reports four separate facts: the learner closed it, current evidence meets the target, achieved historically, and stale and due for a recheck. The learner can close a goal without any evidence being written. Staleness is reported, not acted on, by decision; re-surfacing stays FSRS's job. Thresholds are S18; delayed probes are S14. | [Goal-policy design](superpowers/specs/2026-09-22-goal-policy-design.md), [mastery rule](../app/services/lesson_plan.py), [achievement](../app/learning/mastery.py), [plan tests](../tests/test_lesson_plan.py), [status bar](../frontend/src/components/lessons/GoalStatusBar.tsx) |
| S03 | Independent checks alongside user feedback | Merged | Evaluation design and evidence are tracked once in S59. | [Evaluation decisions](V0_DECISIONS.md#accepted-product-decisions) |
| S08 | One trustworthy learning loop | Merged | Shared acceptance criterion for S01/S11/S14/S56/S58/S59: diagnose, teach, independently apply, and revisit. Existing diagnosis-to-feedback wiring need not be rebuilt. | [Feedback policy](../app/learning/feedback.py), [delivery sequence](V0_DECISIONS.md#delivery-sequence) |
| S09 | Structured per-component diagnosis | Implemented | Diagnoses, checked evidence, recurrent patterns, feedback moves, and failed-review escalation exist. Calibration and accuracy remain S18/S59. | [Diagnosis](../app/learning/diagnosis.py), [recurring-diagnosis tests](../tests/test_recurring_diagnoses.py) |
| S10 | Component evidence and generated rubrics | Implemented | Per-component grading, generated rubrics, scheduling, and displayed component scores exist. Immutable grading provenance and declared-check gaps are consolidated in S56. | [Rubric grading](../app/learning/rubric_grading.py), [item generation](../app/learning/item_generation.py), [component tests](../tests/test_component_evidence.py) |
| S11 | Learner-controlled prerequisite detours | Implemented | A per-plan guidance setting chooses the behaviour: exploration proposes a detour for the learner to take or skip; guided mode may take one automatically; either can be skipped. A detour ends when the prerequisite is mastered or when it is disproved, which takes a run of three unassisted, untaught passes on different items (a failure breaks the run). A disproved route may be offered again; a skipped one is remembered. Global preference defaults remain S02. | [Guidance design](superpowers/specs/2026-09-24-guidance-design.md), [plan service](../app/services/lesson_plan.py), [detour tests](../tests/test_prerequisite_detour.py), [step row](../frontend/src/components/lessons/LessonStepRow.tsx) |
| S12 | Difficulty targeting and instrumentation | Implemented | Selection/generation uses ability-conditioned targets; difficulty reports exist. Delivered difficulty still requires calibration under S18/S59. The two former S12 narratives are consolidated here. | [Difficulty policy](../app/learning/difficulty.py), [targeting tests](../tests/test_difficulty_targeting.py) |
| S13 | Discount assisted and repeated attempts | Implemented | Workflow assistance, repeat exposure, and reduced evidence credit are implemented. Server-established evidence kinds and self-rating separation remain S54/S56. | [Assistance policy](../app/learning/assistance.py), [assistance tests](../tests/test_assistance.py) |
| S15 | Deliberate conversational assessment | Implemented | Explicit checks, safe intent routing, help counts, and persisted results exist. Retry identity is S34, pause/resume is S52, grading provenance is S56, and classifier quality is S59. | [Conversation evidence](../app/learning/conversation_evidence.py), [evidence tests](../tests/test_conversation_evidence.py) |
| S16 | Shared learner context across modes | Implemented | Shared context gathering/composition and learner memory controls exist. Explicit preference precedence is tracked in S02. | [Context service](../app/services/learner_context.py), [context tests](../tests/test_learner_context.py) |
| S19 | Retain existing architectural foundations | Merged | Standing architectural principle, not a separate repair. Keep the modular application, provider-role seam, continuous estimator, and FSRS. | [Release objective](V0_DECISIONS.md#release-objective), [masterplan](MASTERPLAN.md) |
| S21 | Invited access and account recovery | Implemented | Identity is delegated to a hosted provider (Clerk): passwords, recovery, verification, and social sign-in are the provider's and Guru stores none. Guru keeps invitation-controlled enrollment, the admin tier, suspension and reinstatement, and audited visits; a proven identity is exchanged once for the session Guru already issued, so no downstream caller knows the provider exists. Guru's own password system was removed, with existing Argon2 digests handed to the provider so learners keep the password they had. A provider outage answers 503 and a rejected token 401, so an outage cannot present as "everyone signed out". A real instance is configured and verified from this checkout: the secret key is accepted, a malformed token is refused locally, and signature checking resolves the signing key offline. That first live run found what nineteen fake-backed tests had not — a token naming an unknown signing key was classified as a provider failure, so any unauthenticated caller could make sign-in report an outage; it is a bad token now, and the classifier's boundary is asserted in both directions. Sign-in and registration are pages rather than mounted panels, sharing one shell with the provider's own flows inside it. Production mail and account recovery remain unverified in a deployment (S60); the provider's application name and authorized parties are unset. | [Identity seam](../app/core/identity.py), [exchange API](../app/api/v1/auth.py), [error classification](../tests/test_identity_provider.py), [invitation tests](../tests/test_invitations.py), [exchange tests](../tests/test_identity_exchange.py), [auth pages](../frontend/src/pages/AuthPages.test.tsx), [RUNBOOK §11](RUNBOOK.md#11-identity-s21) |
| S22 | Persist generated prerequisites | Implemented | Stable proposal keys, validation, commit revalidation, and edge persistence exist. Concurrent graph integrity is S23; cross-subject transfer is S24; teaching quality is S59. | [Curriculum generation](../app/learning/curriculum.py), [prerequisite tests](../tests/test_prerequisites.py) |
| S23 | Graph integrity under concurrent edits | Implemented | The cycle check and the prerequisite insert run under one global advisory lock, so two concurrent edits cannot create a cycle between them; the edit that would close one is refused with a 409. A subject's owner sees its existing conflicts in a *Curriculum issues* panel and can remove the prerequisite the planner is ignoring. | [Integrity and transfer design §3](superpowers/specs/2026-09-25-integrity-transfer-design.md), [graph service](../app/services/knowledge.py), [concurrency tests](../tests/test_cross_connection.py), [issues panel](../frontend/src/components/lessons/CurriculumIssuesPanel.tsx) |
| S24 | Confirmed cross-subject concept transfer | Implemented | A shared concept name only makes two components a candidate. A link needs an endorsement (an administrator for curated pairs, a SMART-role judge for pairs touching a learner's private subjects) and then the learner's own acceptance; decline and revoke are always available. Accepting seeds a provisional head start from the source's estimate with uncertainty raised to at least 0.6, recorded as a `transfer_seed` event; it cannot count as mastered until two unassisted passes on different items confirm it, and the source's evidence is never touched. A provisional component is checked before it is explained in guided practice. An unmastered prerequisite in another subject becomes an external step in the plan. Named residuals: the judge runs only when a subject is committed and candidates are re-scanned on each Lessons load; an external step does not link to the other subject's plan; check-first does not apply in tutor chat; equivalents whose names differ are never found; a foreign prerequisite's own prerequisites are not traversed. | [Integrity and transfer design §4–6](superpowers/specs/2026-09-25-integrity-transfer-design.md), [link service](../app/services/concept_links.py), [judge](../app/learning/link_judge.py), [link tests](../tests/test_concept_links.py), [API tests](../tests/test_concept_links_api.py), [transfer tests](../tests/test_transfer.py), [RUNBOOK §13](RUNBOOK.md#13-concept-links-s24) |
| S25 | Private ownership and reviewed publication | Implemented | One gate (`is_visible_to`/`is_writable_by`) now covers every operation taking a graph id, including the lesson-plan and content-generation callers previously named here; a guard test fails when a new graph-id operation escapes the table, so the sweep cannot silently rot. Publishing takes a snapshot frozen at request time through administrator review into an immutable, anonymous copy: the original is never made public, withdrawal and superseding unlist without deleting, and upload-derived material is never publishable — that flag is a latch read from a server-side record rather than carried by the client. Subject slugs became per-owner, closing an existence oracle in the de-duplication suffix. Residual, not defended by code: an author who requests a clean proposal and then pastes source-derived material in as their own edits evades the flag, so administrator review of the full snapshot is the control that does not depend on the author's cooperation. Assessment publication (S33) is covered by the same path. | [Visibility gate](../app/services/knowledge.py), [publication service](../app/services/publication.py), [sweep guard](../tests/test_visibility_sweep.py), [author tests](../tests/test_publication_author.py), [review tests](../tests/test_publication_review.py), [slug scope](../tests/test_publication_slug_scope.py), [source-derived latch](../tests/test_source_derived_flag.py), [RUNBOOK §12](RUNBOOK.md#12-publication-review-s25b) |
| S30 | Disable unsafe URL intake for v0 | Implemented | URL import/retry and legacy queued URL jobs reject without fetching; tutor tools expose stored-material search only. External Markdown images are suppressed. New web intake is deferred beyond v0; browser verification remains S58. | [Web-policy tests](../tests/test_v0_web_policy.py), [agent tools](../app/agent/tools.py) |
| S32 | Bind onboarding state to the learner | Implemented | Onboarding ownership is stored durably and checked against authenticated identity. Lifecycle cleanup remains S17. | [Onboarding sessions](../app/services/onboarding_sessions.py), [onboarding API tests](../tests/test_onboarding_api.py) |
| S33 | Assessment authorship and private visibility | Implemented | Generated items/rubrics are private, with owner/visibility checks and legacy quarantine. Reviewed publication is S25; item/rubric versions are S56. Generated no longer means shared. | [Assessment models](../app/models/assessment.py), [ownership migration](../db/migrations/versions/0052_assessment_ownership.py), [privacy tests](../tests/test_assessment_privacy.py) |
| S34 | Concurrency-safe evidence and retries | Implemented | A component's state row is locked (`FOR UPDATE`, rows taken in sorted order) while an answer updates it, so two distinct attempts in flight at once cannot erase each other; a test shows the stale read the lock prevents. A retried chat or workflow turn derives the same attempt id from its client turn id, so the answer is recorded once. | [Integrity and transfer design §2](superpowers/specs/2026-09-25-integrity-transfer-design.md), [mastery updates](../app/learning/mastery.py), [cross-connection tests](../tests/test_cross_connection.py) |
| S35 | Repair plans after committed assessments | Implemented | Pending revision state permits repair without regrading committed evidence. | [Plan service](../app/services/lesson_plan.py), [plan tests](../tests/test_lesson_plan.py) |
| S36 | Recover stranded ingestion delivery | Implemented | Reconciliation, lease recovery, finite attempts, and retry paths exist; real broker coverage exists. Operator alert delivery belongs to S60; a separate dead-letter product is not required for this closure. | [Reconciler](../app/workers/reconcile.py), [recovery tests](../tests/test_ingestion_recovery.py), [queue tests](../tests/test_queue_integration.py) |
| S38 | Correct note catch-up cursors | Implemented | Separate bounded message/event cursors prevent skipped activity. | [Notes service](../app/services/notes.py), [notes tests](../tests/test_notes_service.py) |
| S39 | Topic and concept provenance in notes | Implemented | Distillation is topic-scoped and validates concept references. | [Note distillation](../app/learning/note_distill.py), [distillation tests](../tests/test_note_distill.py) |
| S40 | Preserve exact learner note edits | Implemented | Exact authored Markdown, editable surrounding additions, rewrite suggestions, revision history, and expected-revision restore are implemented. The old lossy-edit/unguarded-restore findings are obsolete. Browser verification remains S58. | [Notes service](../app/services/notes.py), [authorship migration tests](../tests/test_note_authorship_migration.py), [note UI tests](../frontend/src/pages/NoteView.test.tsx) |
| S41 | Bound note rewrites and tolerate render failure | Implemented | Bounded tail rewriting preserves older atoms; failed/truncated renders fall back to deterministic readable content. | [Note distillation](../app/learning/note_distill.py), [notes tests](../tests/test_notes_service.py) |
| S44 | Honest learner-profile proxies | Implemented | Proxy naming and narrower inference replace unsupported trait claims. Explicit settings are S02; refresh behavior is S43; empirical validity is S59. | [Profile estimators](../app/learning/profile_estimators.py), [proxy tests](../tests/test_profile_proxies.py) |
| S45 | Count attempts separately from component events | Implemented | Attempt identity supports correct aggregation rather than counting each component as a learner action. New evidence-kind distinctions remain S56. | [Analytics service](../app/services/analytics.py), [analytics tests](../tests/test_analytics.py) |
| S46 | Honest mastery displays | Implemented | Displays distinguish ability, uncertainty, and practice outcomes. V02's new achievement policy is S01. | [Mastery display tests](../tests/test_mastery.py), [dashboard](../frontend/src/pages/Dashboard.tsx) |
| S51 | Persist interrupted-turn lifecycle | Implemented | Pending/completed/failed/cancelled states, safe retries, and on-access stale-turn recovery exist. Partial-call billing belongs to S48 and process restart verification to S17. Token-by-token continuation is not an accepted requirement. | [Turn lifecycle](../app/services/turn.py), [lifecycle tests](../tests/test_turn_lifecycle.py) |
| S52 | Pause, resume, and skip practice explicitly | Implemented | Every reply to an open practice question passes the S15 intent gate: an attempt is graded, a side question pauses practice and is answered by the tutor ungraded, a withdrawal skips with no evidence. While paused, messages go to the tutor; only the explicit controls resume or skip. An agentic interjection neither resumes nor un-pauses. Help given during a pause counts as assistance on the eventual answer. | [Guidance design §4](superpowers/specs/2026-09-24-guidance-design.md), [practice service](../app/services/practice.py), [chat routing](../app/api/v1/chat.py), [pause tests](../tests/test_practice_pause.py) |
| S54 | Flashcard reveal and self-rating | Implemented | Think, reveal, then rate: the answer is revealed on request, the learner rates recall, and the copy says a rating schedules review rather than measuring ability (S56). Other item types still withhold their keys until answered. A failed reveal no longer strands the learner, and a re-asked flashcard adds nothing to the transcript. | [Evidence-kinds design §9](superpowers/specs/2026-09-21-s56-evidence-kinds-design.md), [flashcard panel](../frontend/src/components/lessons/FlashcardPanel.tsx), [privacy tests](../tests/test_assessment_privacy.py) |
| S55 | Validate source scope and refresh tags | Implemented | Source reassignment validates topic/subject relationships and refreshes derived tags. Consistent generation policy is S26; legacy scope repair and versioned reindexing are S50. | [Source service](../app/services/ingestion.py), [source API tests](../tests/test_sources_api.py) |
| S57 | Make supported sweep settings effective | Implemented | Supported knobs affect execution; unsupported settings fail; applied settings, roles, and dataset identity are recorded. Further experiment expansion belongs to Phase 9 and S59. | [Sweep settings](../tests/eval/sweep/settings.py), [sweep tests](../tests/eval/test_sweep_runner.py) |
| S63 | Show the complete goal beyond the planning window | Implemented | The Lessons page shows the whole goal (objectives in the window, deferred objectives, and goal status) so finishing the current window no longer looks like finishing the goal. | [Goal-policy design §9](superpowers/specs/2026-09-22-goal-policy-design.md), [status bar](../frontend/src/components/lessons/GoalStatusBar.tsx), [plan tests](../tests/test_lesson_plan.py) |
| S67 | Evaluate iterations through observed learning and return | Merged | Agreed evaluation direction, retained through S59/S65. | S59, S65 |
| P10 | Administrator portal and audited alpha sudo | Implemented | Portal, timing/spend summaries, reason-required short visits, durable action audit, live revocation, actor attribution, and learner-evidence exclusion exist. The switch defaults off. Broad V13 access supersedes read-only/learner-opt-in proposals; sudo browser coverage is S58. | [Impersonation service](../app/services/impersonation.py), [audit service](../app/services/admin_audit.py), [sudo tests](../tests/test_admin_sudo.py), [attribution tests](../tests/test_message_admin_attribution.py) |

## Deferred work and unaccepted strategy proposals

These are not additional v0 engineering gates. The original commercial and research suggestions
remain identifiable without duplicating development work or treating them as accepted strategy.

| ID | Item | Status | Next step or closure | Evidence |
| --- | --- | --- | --- | --- |
| S04 | Establish value, continued use, and delivery economics before expansion | Deferred | Revisit business strategy with usage evidence. Engineering cost accounting is S48; learning evaluation is S59. | [Release scope](V0_DECISIONS.md#release-objective) |
| S05 | Revalidate teaching for new populations and environments | Deferred | Revisit when expanding beyond invited adults. | [Deferred scope](V0_DECISIONS.md#already-deferred) |
| S06 | Institutional integration versus a full LMS | Deferred | Decide after institutional needs are known; O06 remains deferred. | [Deferred scope](V0_DECISIONS.md#already-deferred) |
| S07 | Evaluate the whole flipped-classroom arrangement | Deferred | Revisit with an institutional pilot, including teacher-led application and support. | [Masterplan](MASTERPLAN.md) |
| S68 | Position on observed comparative value | Proposed | Maintain accurate capability comparisons if positioning work resumes; no superiority claim is established. | S59, S69 |
| S69 | Compare suitable alternatives during founder use | Proposed | Record configuration, material, prior knowledge, and order effects. Do not treat a repeated topic as an uncontaminated learning comparison. | S64, S65 |
| S70 | Investigate actual learning habits and switching reasons | Proposed | Ask first users what they use, postpone, and choose after trying Guru. Seniority alone does not establish a market or willingness to pay. | S65, S66 |
| S71 | Build reusable reviewed learning cases | Proposed | If adopted, contribute permitted, traceable cases to S59 rather than a second evaluation pipeline. | S59 |
| S72 | Compare learning, effort, retention, and return separately | Proposed | If competitive testing is adopted, extend S59/S65 without confusing completion or enthusiasm with learning. | S59, S65 |
| S73 | Evaluate full value to a reachable audience | Proposed | Investigate experience, fit, support, convenience, and continued use without requiring category-wide superiority. | S68–S70 |
| S74 | Separate sustainability, expansion, and acquisition criteria | Proposed | Revisit within business strategy; these are different outcomes, not implementation milestones. | S04, S75 |
| S75 | Keep acquisition optional | Proposed | Do not make the operating plan depend on a named buyer. No buyer interest, offer, or acquisition probability has been established. | S74 |

URL ingestion/web discovery, DKT, billing, younger learner tiers, institutional/LMS work, native
clients, and offline operation remain outside v0. Historical competitor research, acquisition
precedents, and links to absent pilot/landscape documents have been removed from this tracker.

## Decisions and standing direction

The accepted v0 decisions are maintained once in [V0_DECISIONS.md](V0_DECISIONS.md), rather than
repeated as unresolved questions here.

| ID | Current disposition |
| --- | --- |
| O01 | Resolved: invited, experienced adults; subjects deliberately unrestricted. |
| O02 | Cohort payment/pricing arrangement remains open within later operating/business choices. |
| O03 | Direction resolved by V02. Goal-specific thresholds, evidence freshness, and duration still require S01/S18/S59. |
| O04 | Resolved approach: calibration and grading reliability first; later educational-effect studies remain separate. |
| O05 | Funding for broad free access remains open; no amount, source, or runway is committed. |
| O06 | Institutional integration versus LMS remains deferred under S06. |

Standing discussion IDs are retained compactly for continuity:

| ID | Direction or context |
| --- | --- |
| D01 | Durable, independently usable understanding; long-term access across subjects and populations. |
| D02 | Experienced, engaged adults form the initial learning and feedback cohort. |
| D03 | Model knowledge relative to domain, task, and goal; no global intelligence score. |
| D04 | Learner-led exploration with adjustable guidance, now specified by V07/V09. |
| D05 | Address forgotten prerequisites and never-established understanding while respecting existing capability. |
| D06 | Financial sustainability supports the access mission; margin maximization is not the mission. |
| D07 | Institutional delivery and flipped classrooms are possible later routes, not an immediate LMS commitment. |
| D08 | Large learning gains are a research ambition requiring Guru-specific evidence. |
| D09 | Let product direction evolve through use, feedback, and iteration. |
| D10 | Historical review/development coordination is complete as a process note; it creates no new backlog item. |
| D11 | Founder use precedes observed sessions and independent invited use. |
| D12 | Founder time is available; funding, runway, and commercial commitments remain unspecified. |

## Verification record and limitations

### Workstream 2 and hosted-identity pages — 2026-09-26

Run on the combined branch for PR #40 (workstream 2 merged into `fix/auth-ui`), locally and in CI:

- Backend: `poe check` **2112 passed, 6 skipped**; format check and API contract pass. One local
  `poe check` run failed and two reruns passed; the failure was not captured, so which test it
  was is unknown. CI's backend job passed.
- Frontend: **156 tests across 28 files**, production build, and lint pass.
- Browser journeys: **13 passed** locally against RustFS and in CI, after the fixes recorded
  under S58. Locally the admin journey passes only on a freshly created e2e database, which is
  the order dependence already recorded there.
- Educational effect of the new rules (the mastery bar, detour disproval, transferred head starts)
  is not established by any of this; see S18 and S59.

### Latest recorded integration run — 2026-09-19

Preserved from the prior tracker; **not rerun during this cleanup**:

- API contract regeneration/comparison, backend lint/format/types, migration to head, the
  `0053 → 0052 → 0053` round trip, and model/migration drift checks were recorded as passing.
- Item-exposure chronology: 16 passed. Frontend: 83 tests, lint, and production build passed.
- **Backend suite not green:** `tests/eval/test_eval.py::test_retrieval_eval_gate` reported
  pass rate **0.25**, requiring **1.0**, with three expected top-k recall misses. The remainder
  reported **1702 passed, 6 skipped**. Explained and fixed on 2026-09-26 under S76: the vector
  arm could use the HNSW index before the learner filter and return none of the learner's rows.
- That integration pass did not perform browser network capture, real Redis delivery, or live
  provider evaluation. Existing Redis/browser suites elsewhere in the repository do not change
  the scope of that recorded run.

This cleanup verifies tracker structure, stable IDs, local links, and the documentation diff.
Source-visible gaps are findings for follow-up, not claims of reproduced production incidents.
No educational outcomes, current hosted CI status, or deployment readiness are established here.

### S76 measurement context — recorded 2026-09-08

The earlier synthetic experiment found the scoped vector query using an exact join-based path,
not HNSW; it did not support the proposed filtered-ANN explanation for the intermittent failures.
Recorded exact-search cost was about 4 microseconds per owned chunk, reaching 185 ms at 45,000.
For a separate 40,050-chunk comparison requesting 50 results:

| Query shape | Recall against exact baseline | Recorded latency |
| --- | --- | --- |
| Scoped production query, exact path | 50/50 | 162 ms |
| Index-reaching alternative, `ef_search=40` | 44% | 1.4 ms |
| Index-reaching alternative, `ef_search=1000` | 86% | 15.0 ms |

These are historical measurements on hash-derived, near-uniform vectors, not current production
benchmarks or real-corpus relevance evidence. They justify measuring the tradeoff, not changing
retrieval or closing the failure. The repeatable experiment is documented in the
[runbook](RUNBOOK.md#65-retrieval-recall--plan-s76--needs-a-live-db-no-model).
