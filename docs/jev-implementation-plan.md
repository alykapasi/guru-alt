# Jev integration implementation plan

> **Superseded, 2026-09-26.** Implemented instead as
> [the Jev turn read](superpowers/specs/2026-09-26-jev-turn-read-design.md), with plan
> `docs/superpowers/plans/2026-09-26-jev-turn-read.md`:
> - Phase 1 is done as specified.
> - Phase 2's offline labelled comparison is replaced by the shadow report on real founder
>   traffic.
> - Phase 3's privacy work is now a precondition for inviting other learners.
> - Phases 4–5 became the per-question shadow/live switch.
> - Phase 6 and the other studies remain future work.

Status: proposed on 2026-09-18. This document authorizes no implementation, dependency installation, paid evaluation, deployment, or learner-data export. Read [jev-architecture.md](jev-architecture.md) and the verified [capability snapshot](jev-capabilities.md) alongside this plan. Existing work in this checkout must remain intact.

**Goal:** evaluate whether a typed decision service improves Guru's conversational answer-intent gate at acceptable cost and latency, while preserving its conservative evidence semantics.

**Architecture:** retain Guru's generative LLM role registry, LangGraph, DSPy, continuous mastery estimator, and FSRS. Add a separate decision contract under `app/llm/`, then compare the existing FAST gate with Jev on the same labeled examples. Keep behavior unchanged until quality and privacy gates pass.

**Stack:** existing Python/FastAPI/Pydantic/async service architecture and deterministic test providers; the user-added installed `typesafe-sdk` 0.6.0 is the starting transport candidate. No reinstall or dependency change is part of this task.

## Choice of scope

| Approach | First seam | Benefit | Cost and risk |
| --- | --- | --- | --- |
| Answer-intent gate — recommended | `app/learning/conversation_evidence.py::classify_intent`, called by `app/services/chat.py::_resolve_check` | Existing finite attempt/deferral/withdrawal task and FAST baseline; errors and asymmetric consequences are easy to measure | A false attempt can cause grading and mastery updates, so live promotion needs strict evidence protections |
| Bounded tutoring move — later | `app/services/session_runner.py`, `app/learning/lesson_plan.py`, `app/services/lesson_plan.py` | Choose among pedagogical moves prepared by Guru | Adds inference to policy that is partly deterministic; candidate validity and learning benefit need separate evidence |
| Grading second opinion — optional research | `app/learning/rubric_grading.py`, `app/services/assessment.py` | Investigate rubric consistency on labeled responses | Scores and probabilities are not established mastery measurements; do not average them into grades or tracer inputs |

No approach replaces prose generation, retrieval, DSPy prompt compilation, Glicko estimation, or FSRS. The first scope tests a narrow decision task rather than the vendor's overall training claims.

## Shared contracts and constraints

The proposed `DecisionClient` is a separate contract, not an `LLMProvider` implementation. A request contains a task identifier, contract/prompt version, minimal task state, named typed questions, an explicit model identifier resolved from decision configuration, deadline, and request correlation key. Guru owns the allowed alternatives; private learner or source identifiers must not appear in synthetic smoke requests.

A normalized result contains the named typed answers, returned probabilities where supplied, actual model/transport metadata, timing, and a success/abstain/error status. It contains no tutor prose, database operations, tools, or instructions to update mastery. Validate the exact vendor schema at the transport boundary rather than inventing normalization rules for undocumented fields. A missing or malformed answer, unknown alternative, invalid probability, timeout, or provider error is not an attempt.

Probabilities describe model output, not empirically calibrated learner confidence. Any promotion threshold requires held-out labels, an explicitly recorded decision owner, and a versioned policy. No production cutoff is chosen in this document.

The intent pilot uses a single named Choice question with the three existing labels. Official Choice results provide selected choice, label probabilities and confidence; Noul has no separate confidence field, and Score's expected rubric level is not an ability estimate. Questions independently see the same state, so a request must not assume later answers observe earlier ones. Pin the evaluated model (`jev-1.13.0` in this snapshot), record the returned model, supply explicit question instructions, and require the expected answer name and type: the public SDK can silently skip unknown types. [API](https://docs.typesafe.ai/api), [Models](https://docs.typesafe.ai/models).

The installed SDK defaults (10-second HTTP timeout, two retries and a 30-second retry budget) are not an interactive latency target or guaranteed cancellation. Set Guru's explicit end-to-end deadline and conservative fallback; do not assume a remote job-cancellation API. Usage counts can be null, and SDK DEBUG wire logging can contain raw state: preserve unknown usage and suppress body logging. These are implementation checks, not measured latency results; see the capability snapshot for reproducibility pointers.

Keep operational states explicit: disabled uses today's FAST gate; offline and shadow compare without changing its outcome; enabled uses the approved Jev interpretation and resolves invalid, uncertain, or failed results to DEFERRAL. Rollback switches to disabled for subsequent requests. Do not silently retry through a second paid classifier in enabled mode: that changes failure semantics, latency, and accounting.

## Phase 1 — verify a stateless synthetic contract

**Proposed files:** create `app/llm/decision.py` for contract types and a deterministic decision fake; create `app/llm/providers/jev.py` for the isolated transport; extend `app/core/config.py` for disabled-by-default decision configuration; create `tests/test_decision.py` and `tests/test_jev_decision.py`. These are future file proposals, not files changed by this documentation task.

- [ ] Confirm the official API/SDK model identifier, authentication, typed question and response fields, supported asynchronous interface, error behavior, timeout/cancellation, and usage metadata against the sources linked in the architecture document. Do not infer streaming, embeddings, tools, prose, or user fine-tuning support.
- [ ] Write deterministic contract cases for a valid three-way choice, missing named answer, unknown choice, malformed probabilities, error, timeout, and cancellation. Confirm transport validation rejects invalid results without database access.
- [ ] Keep credentials in configuration and redact them from errors; keep prompt payloads out of ordinary logs. Verify disabled mode makes no Jev call.
- [ ] Prepare three invented, stateless examples: requesting a hint is deferral; an incorrect genuine answer is attempt; explicitly declining is withdrawal. These are smoke fixtures, not educational validation.

**Acceptance:** fake tests prove the contract and failure handling; no source, learner, grader, mastery, FSRS, or conversation write is reachable. A future live synthetic smoke requires explicit paid-run authorization and a spending cap, but does not require unrelated learner-evidence fixes because it uses invented state with no learner behavior.

## Phase 2 — compare answer intent offline

**Proposed files:** extend `tests/test_conversation_evidence.py`; create `tests/eval/cases/answer_intent.json` and `tests/eval/answer_intent_report.py`; preserve `tests/eval/prompts/` as the existing DSPy KC-tagging evaluation path.

**Interface:** both classifiers consume the same posed question and learner reply. The current baseline remains `classify_intent` with FAST and its existing conservative parse/error default. The candidate maps the validated finite decision into the same `TurnIntent` values; it does not grade correctness.

- [ ] Build labeled invented cases covering wrong attempts, partial attempts, uncertainty, hint requests, thinking aloud, unrelated replies, withdrawals, quoted instructions, empty text, and ambiguity. Have labels reviewed independently; keep train/development/held-out cases distinct.
- [ ] Run both classifiers against identical cases using deterministic providers first. Verify that a correct answer and a wrong genuine answer are both attempts, and hints are not scored as failures.
- [ ] Prepare a future budgeted paired report: confusion matrices; false-attempt rate on non-attempts; attempt recall; false-withdrawal rate; abstention/fallback rate; latency distribution; actual cost and unknown-cost coverage. Report errors rather than dropping failed cases.
- [ ] Record dataset, question schema, mapping policy, prompt, model, and transport versions. Keep thresholds provisional until an evaluation owner approves concrete tolerances before looking at the held-out result.

**Acceptance:** deterministic evaluation and fixtures work without network calls. A future paid comparison must meet prespecified quality, latency, and cost tolerances; synthetic success establishes neither real learner calibration nor learning benefit. An inconclusive or inferior candidate stays disabled.

## Phase 3 — prerequisite learner evidence and privacy fixes

These are existing Guru issues, not benefits attributed to Jev. Ownership/context isolation, vendor data handling, admin exclusions and evaluation-artifact retention are hard prerequisites for learner-connected export. Flashcard evidence separation and affected evidence replay are prerequisites for ability, policy or durable-outcome studies using those traces; they need not block an intent-only shadow that reads no mastery features and attributes no learning outcomes. Preserve existing data through reviewed migrations rather than resetting the database.

| Required slice | Exact current seams | Acceptance cases |
| --- | --- | --- |
| Item ownership and provenance | `app/models/assessment.py`; `app/services/assessment.py::_assessable_by`, `get_item_for`, `find_item_for_kc`; `app/learning/item_generation.py`; `app/services/chat.py::_resolve_check` | A source-derived generated item belonging to learner A cannot be selected, loaded, posed, or answered by B. Cover `_resolve_check`'s current unscoped `get_item` call and legacy generated items; unknown ownership must not become global publication. Extend `tests/test_assessment.py`, `tests/test_source_scope.py`, `tests/test_session_runner.py` |
| Flashcard evidence separation | `app/learning/grading.py::grade_flashcard`; `app/services/assessment.py::answer_item`; `app/learning/mastery.py::Observation`, `record_observation`; `app/learning/scheduler.py` | A reveal/self-rating advances appropriate retention evidence without improving ability or shrinking uncertainty. A real independently graded answer still updates ability. Server-recorded evidence kind supports replay; test `tests/test_assessment.py`, `tests/test_mastery.py`, `tests/test_scheduler.py` |
| Admin/sudo exclusions | `app/api/deps.py`; `app/services/turn_common.py::add_message`; `app/learning/mastery.py::record_observation`; `app/services/profile.py`; `app/services/impersonation.py` | Borrowed-account actions remain audit tagged and cannot become learner intent labels, mastery, FSRS, profile, or experiment outcomes. Preserve separate `admin_observation`; extend `tests/test_impersonation.py` and `tests/test_conversation_evidence.py` |
| Source and provider context isolation | `app/rag/retrieval.py::retrieve`; `app/agent/tools.py::build_tools`; `app/services/learner_context.py`; `app/agent/untrusted.py`; `app/agent/egress.py` | Tenant and conversation source scope survive actual callers; only minimal posed question/reply reaches intent inference. A source-derived stem is still private material and subject to provider-export controls. No bulk retrieval, unrelated transcripts, memories, URLs, or tools accompany that request. Retrieved text remains data; URL text matching is not a general defense against summaries leaking |
| Versioning and deletion | `app/models/learning.py`; `app/models/chat.py`; `app/services/retention.py`; `app/services/llm_log.py` | Evidence is distinguishable by item/rubric/prompt/model/version and retains replay semantics; candidate evaluation artifacts participate in export, erasure, retention and access rules. Extend `tests/test_retention.py` and `tests/test_llm_log.py` |

**Gate:** relevant scoped protections must be verified through service/API callers, including retries, cross-learner access, and legacy data. Do not require completion of every unrelated roadmap workstream for the synthetic smoke or intent-only shadow.

## Phase 4 — read-only shadow intent comparison

**Proposed files:** adapt `app/services/chat.py::_resolve_check` to invoke a shadow decision path; extend `app/learning/conversation_evidence.py` with the normalized candidate mapping; extend accounting near `app/services/llm_log.py` and `app/models/chat.py`; include `app/services/budget.py`, `app/services/spend.py`, and `app/services/retention.py` in the boundary review; add `tests/test_decision_shadow.py` and extend `tests/test_chat_budget.py`.

- [ ] Keep FAST authoritative. Jev disagreement cannot open/close the check, change scaffolds, trigger grading, update an event, or change tutor output. Record comparison metadata separately from learner observations.
- [ ] Schedule the shadow as a concurrent best-effort request with an explicit local deadline and cancellation/skip policy; do not await it in the authoritative reply path beyond the existing baseline. Reserve shadow budget first, cap outstanding tasks, and shut down/cancel tracked tasks rather than create unbounded detached work. Deduplicate application scheduling by the request correlation key; do not assume this guarantees remote deduplication or prevents duplicate billing after retries.
- [ ] Copy only immutable minimal request data into a bounded process-lifetime task supervisor; never retain the request's `AsyncSession` or ORM objects after its response closes. Use independent sessions for authorization rechecks and diagnostic/accounting writes. Immediately before dispatch, recheck consent, ownership, actor and deletion/suppression state; skip revoked, expired, late or ineligible tasks. On disconnect/shutdown cancel local pending work, discard late diagnostic payloads, and record any potentially incurred provider usage as known or unknown independently; local cancellation cannot promise remote non-processing or a billing refund.
- [ ] Define an explicit authorized sample and provider-data policy before sending real replies; exclude admin/sudo traffic and suppressed/deleted data. Resolve the applicable agreement, retention/backup purge, deletion mechanism and timeline, subprocessor locations and ZDR entitlement/configuration; advertised enterprise ZDR is not the default. Use a bounded sample and deadline, with skip behavior when the shadow budget is unavailable.
- [ ] Account for actual candidate spend independently of business rollback, as existing LLM accounting does. Do not invent token prices or mark unavailable cost as zero; decide whether extending the existing call schema or a separate decision-call record preserves truthful accounting. Enforce conservative request-count and input-size caps alongside a spending reservation when token usage is missing, and report unknown-cost coverage separately.
- [ ] Exercise timeout, malformed answer, rollback, disconnect, duplicate request, budget denial, logging failure, export, erasure, and disabled-mode cases. The shadow path must not alter the authoritative intent result or duplicate learner evidence.

**Acceptance:** actual chat behavior and learner state are identical with shadow on/off. Obtain reviewed labels for sampled disagreements; require the prespecified false-attempt, latency, cost and deletion gates before considering enabled behavior. No automatic promotion.

## Phase 5 — bounded enabled intent experiment

**Proposed files:** `app/services/chat.py::_resolve_check`, `app/learning/conversation_evidence.py`, `app/core/config.py`; extend `tests/test_conversation_evidence.py`, `tests/test_assessment.py`, `tests/test_chat_budget.py`, and `tests/test_impersonation.py`.

- [ ] Authorize a small invited cohort with an explicit versioned policy, exposure cap, budget, stop conditions and rollback owner. No cohort or numerical tolerance is silently chosen by this plan.
- [ ] Enable only the intent decision. Existing rubric grading, assistance discounting, atomic/idempotent observations, LangGraph generation, and lesson revision stay authoritative after a validated attempt.
- [ ] Verify errors and uncertainty retain the posed check as DEFERRAL; withdrawal records no failed observation; repeated hints remain scaffolds; duplicate attempts cannot produce duplicate mastery evidence.
- [ ] Stop and disable on false-attempt/privacy failures or breached operational tolerances. Review label accuracy and learner experience separately from any longer-term educational outcome.

**Acceptance:** evidence/privacy invariants and operational gates pass on actual paths; an evaluation owner explicitly decides retain, revise, or remove. Passing software tests is not proof of durable learning.

## Phase 6 — optional bounded tutoring policy study

Proceed only if the intent study supports continued investment. Proposed seams are `app/services/session_runner.py`, `app/learning/lesson_plan.py` and `app/services/lesson_plan.py`, with tests in `tests/test_session_runner.py`, `tests/test_lesson_plan_engine.py` and `tests/test_lesson_plan.py`.

Guru prepares a small set of allowed next moves using current learner scope, prerequisites, due reviews, active check, assistance, source availability and learner choice. A decision client may choose only within that set; rejected, failed or ambiguous choices use today's deterministic policy. The generative SMART tutor writes any explanation afterward. Compare against the current policy, which may cost no decision call; measure incremental cost and pedagogical outcomes before live promotion.

Do not change goal achievement as part of this study. Current `mastered_kc_ids` uses ability >= 1.0 and uncertainty <= 0.5; the accepted future direction in `V0_DECISIONS.md` is `ability - 2 * current_uncertainty`, with independent evidence over time and durations calibrated from data. Vendor probabilities do not replace either policy, and merely waiting does not establish sustained evidence.

## Review and rollout checklist

- [ ] Review each slice against the architecture and current source before future implementation; preserve existing dirty work and current learner data.
- [ ] Run relevant deterministic tests for the changed boundaries; run repository lint/type/test checks and migration/API contract checks when implementation changes those boundaries.
- [ ] Record verification as software correctness, model decision quality, or educational effectiveness, with their separate limitations.
- [ ] Keep all paid runs, learner exports, dependencies and deployments deferred until specifically authorized.
- [ ] Keep decision integration disabled unless its current phase has explicit promotion evidence; rollback must preserve genuine already-recorded observations rather than rewrite history.
