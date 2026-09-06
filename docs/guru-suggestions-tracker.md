# Guru — Running Suggestions and Decisions

Last updated: 2026-09-06
Repository: https://github.com/alykapasi/guru-alt
Reviewed snapshot: `0d9b7f8abb1c623d0c46f3a53dda210a4790289f`

## Purpose and maintenance

This is the ongoing record of suggestions, agreed direction, open decisions, and findings from our discussion. Update this same file as the review progresses.

- Keep stable IDs so we can discuss individual suggestions.
- Record proposals separately from decisions. Agreement on the mission does not approve every implementation suggestion.
- Retain rejected and superseded entries with their reasons.
- Mark implementation complete only with evidence; record educational validation separately.
- Priorities below are provisional assistant recommendations, not an agreed implementation schedule.
- Accepted means the user agrees with the recommendation; detailed designs, sequencing, implementation, and validation remain outstanding.
- No repository changes have been made as part of this review.

Statuses: **Agreed direction**, **Accepted**, **Proposed**, **Open**, **Deferred**, **Implemented**, **Validated**, **Rejected**, **Superseded**.

## Agreed direction

| ID | Direction | Status |
| --- | --- | --- |
| D01 | Help people gain durable, independently usable understanding; ultimately support anyone learning anything. | Agreed direction |
| D02 | Start with experienced, engaged adults, including senior professionals and postdocs. They are initial beneficiaries and a proving ground that can help expose failures. | Agreed direction |
| D03 | Represent knowledge relative to a domain, task, and goal. Avoid assigning a global intelligence or sophistication score. Expertise, formal education, age, and access are distinct dimensions. | Agreed direction |
| D04 | Initially emphasize learner-led exploration with active support. Expand the ability to lead structured learning as audiences broaden. Guidance remains adjustable within every audience. | Agreed direction |
| D05 | Adapt both to forgotten prerequisites and to understanding that was never established; respect existing practical capability. | Agreed direction |
| D06 | Build a financially sustainable service. High-quality free access for underserved learners is a long-term objective; margin maximization is not the mission. | Agreed direction |
| D07 | Institutional delivery and a flipped-classroom model are potential later routes to access and revenue. A full LMS is a possibility, not a committed immediate build. | Agreed direction |
| D08 | Treat Bloom's two-sigma problem as a research ambition. Guru must establish its own educational effects rather than assume a two-standard-deviation improvement. | Agreed direction |

## Reference learning scenario

A learner studied CS and statistics, has applied ML professionally, and is comfortable coding models. Their linear algebra has faded and theoretical ML remains difficult. Guru should clarify their intended capability, investigate their current understanding, identify the prerequisite obstructing progress, teach it, reconnect it to ML, and check independent application and later retention.

ML is a concrete review scenario, not an agreed permanent subject boundary or launch market.

## Suggestions register

### Product, evidence, and business

| ID | Suggestion | Why / intended result | Priority | Status |
| --- | --- | --- | --- | --- |
| S01 | Define success for each learning goal in terms of what the learner can explain, derive, apply, critique, or produce independently. | Gives teaching and assessment a concrete destination beyond completing content. | Early | Accepted |
| S02 | Use audience and preferences to set initial guidance, then let learners change it and propose adjustments when difficulties emerge. | An experienced engineer may still want foundational mathematics taught step by step. | Early | Accepted |
| S03 | Combine feedback from capable early users with independent checks on explanations, grading, and learning outcomes. | Users may miss errors precisely in the material they are learning. | Early | Accepted |
| S04 | Establish learning value, continued use, and delivery economics before broad expansion. Track access and educational effectiveness separately. | Supports a sustainable business and prevents free availability from being mistaken for effective education. | Early | Accepted |
| S05 | Validate the teaching again when expanding to different prerequisites, literacy, languages, devices, and support environments. | Success with experienced adults is only part of the evidence needed for underserved populations. | Later | Accepted |
| S06 | Keep institutional integration versus a full LMS open until institutional needs are understood. | Establish the necessary class, curriculum, assignment, and teacher-oversight capabilities before committing to a large platform. | Later | Accepted |
| S07 | In future flipped-classroom pilots, evaluate the entire arrangement: individual preparation plus teacher-led discussion, application, collaboration, and targeted help. | Moving instruction outside class alone does not establish improved learning. | Later | Accepted |

### Adaptive learning architecture

| ID | Suggestion | Current evidence / gap | Desired result | Priority | Status |
| --- | --- | --- | --- | --- | --- |
| S08 | Build one trustworthy end-to-end learning sequence before adding more breadth. | The assessment and planning machinery exists, but diagnosis and teaching decisions are weakly connected. | Detect a specific gap → ask a discriminating question → teach → test a fresh unassisted application → revisit later. | First | Accepted |
| S09 | Add structured diagnosis of specific misconceptions and prerequisite gaps, with uncertainty and supporting evidence. | Placement infers rough levels; grading returns a single score and short rationale. These do not establish why an answer failed. [R1–R3] | Distinguish forgotten notation, a procedural error, and a conceptual misunderstanding before choosing help. | High | Accepted |
| S10 | Preserve component-specific assessment evidence and define explicit grading criteria for generated open questions. | The same aggregate score updates every tagged component with different weights; generated short questions have no explicit rubric. [R2–R4] | Avoid treating a failure in projections as equal evidence of failure in every skill involved in least squares. | High | Accepted |
| S11 | Make targeted prerequisite detours an explicit planning capability. | Routine revision changes status, review order, and scaffolding hints while preserving remaining new-topic order. [R5] | Investigate and address the prerequisite blocking the learner, then return to the original objective. | High | Accepted |
| S12 | Apply difficulty targeting to question selection/generation. | The session runner explicitly documents target difficulty as unapplied. [R6] | The learner's estimated capability affects the actual task they receive. | High | Accepted |
| S13 | Distinguish assisted retries from independent demonstrations in mastery evidence. | Guided practice hints and retries the same question; every attempt updates mastery. Hint context is omitted by that workflow and is not used by the estimator even when recorded elsewhere. [R7–R8] | Prevent assistance and repeated exposure from producing unjustified mastery confidence. | First | Accepted |
| S14 | Select fresh assessment items with awareness of prior exposure, and check delayed retention and transfer. | Bank selection returns the oldest matching item without considering the learner's exposure. [R2] | Establish that the learner can solve a different problem without help and retain that capability. | First | Accepted |
| S15 | Connect exploratory conversation to structured learning evidence through a deliberate assessment mechanism. | Plain chat can ask questions, but its conversational answers do not directly update mastery. [R9] | Make the initial learner-led experience contribute trustworthy evidence without treating all conversation as proof of mastery. | High | Accepted |
| S16 | Share appropriate learner context and learning-state access across chat, agentic, and guided modes. | Plain chat injects memory and plan hints; agentic service does not inject those same contexts. [R9–R10] | Switching modes retains relevant understanding of the learner and their goal. | High | Accepted |
| S17 | Persist resumable guided-practice state durably. | Workflow uses an in-memory checkpointer. [R7] | A restart does not lose the paused practice state needed to continue correctly. | Before reliable external use | Accepted |
| S18 | Calibrate mastery, placement, and scaffolding heuristics against real evidence. | Placement mappings, completion thresholds, and profile-to-scaffolding thresholds are explicitly described as arbitrary or uncalibrated. [R1, R5, R11] | Progress estimates and teaching choices correspond to demonstrated capability. | High; requires data | Accepted |
| S19 | Retain the useful existing foundations while improving the teaching loop. | Pure estimation logic, persistent per-component state, event logging, prerequisite planning, and provider abstraction already exist. | Improve the behavior incrementally using existing boundaries. | Ongoing | Accepted |
| S20 | Synchronize documentation with implementation and the clarified mission. | README describes the frontend as future work; roadmap labels the experiment suite not started despite tooling being present. Audience guidance also needs the nuance agreed here. | Future reviews and implementation plans start from an accurate description. | Supporting | Accepted |
| S21 | Replace the development identity stub before real multi-user access; review production readiness separately. | The inspected auth dependency resolves a single dev learner. [R12] | Real learner identity and tested authorization boundaries before independent user access. | Before external multi-user use | Accepted |

### Knowledge graph and content pipeline — second review

These are new proposals from the second review; the user's acceptance of prior suggestions applies to S01–S21 only.

| ID | Suggestion | Evidence / consequence | Priority | Status |
| --- | --- | --- | --- | --- | --- |
| S22 | Generate, validate, and persist prerequisite relationships during curriculum creation. | CurriculumProposal and create_subject_with_graph contain topics and KCs but no prerequisite edges. Newly generated curricula therefore lack the dependencies the planner needs. [R13–R14] | First | Proposed |
| S23 | Validate the prerequisite graph, including multi-node cycles and references, before relying on its order. | Edge creation checks self-loops and existence but not longer cycles; topo_sort appends unresolved nodes when a cycle occurs. [R5, R15] | High | Proposed |
| S24 | Define concept identity and cross-subject prerequisite handling explicitly. | Each KC belongs to one topic. Duplicate concepts get separate IDs and mastery states; the planner's candidate pool and edge loading do not establish a complete cross-subject traversal. Cross-subject edges are possible in the schema, so this is an incomplete policy rather than a database prohibition. [R14, R16, R11] | High | Proposed |
| S25 | Separate shared, curated knowledge from learner-specific generated curricula, with ownership and publishing rules. | Subjects are global, listing is unscoped, commit rejects a duplicate subject name globally, and learner-authenticated routes can add global topics/KCs/edges. Personal goal-derived structure has no explicit private draft boundary. [R14–R16] | Before multi-user release | Proposed |
| S26 | Apply explicit, consistent source scope to every generation path; make intentional cross-subject expansion a separate decision. | Chat uses subject/source filters; generate_block retrieves across the learner's sources without subject/topic filters. This remains learner-scoped and is not evidence of cross-user retrieval leakage. [R9, R17–R18] | High | Proposed |
| S27 | Preserve technical document structure and evaluate extraction on equations, tables, code, and derivations; represent unknown extraction quality honestly. | PDF extraction falls back to OCR based on text length; chunking collapses whitespace and uses 1,000-character windows; pipeline assigns confidence 1.0 to every chunk. This establishes risk, not measured corruption rates. [R19–R21] | High for advanced technical learning | Proposed |
| S28 | Distinguish valid citation pointers from claim support, and establish behavior when sources are insufficient or contradictory. | Citation resolution validates indices, not whether passages support claims. Content generation's source-only system instruction conflicts with its general-knowledge fallback for empty retrieval. [R17, R22] | High | Proposed |
| S29 | Define content cache versions and invalidation for changes in objectives, prompts, models, and source revisions; separately decide what may be shared. | The current key includes learner, KC IDs, block type, and grounding IDs, but omits prompt/model versions and KC description changes. Current cache is learner-specific despite the long-term reuse ambition. Reingestion deletes/recreates chunks, warranting explicit handling for historical citation references. [R17, R21] | Supporting; before broad reuse | Proposed |

## Open decisions

| ID | Question | Current position |
| --- | --- | --- |
| O01 | Who exactly are the first users, and in which subject or task? | Audience characteristics agreed; recruitment cohort and first domain remain open. |
| O02 | Are initial users primarily a closely involved test cohort, paying customers, or both? | Sustainable revenue matters; cohort arrangement and pricing remain open. |
| O03 | Which independent capabilities define the first successful experience? | ML theory and forgotten linear algebra provide a reference case; acceptance criteria not yet chosen. |
| O04 | How will learning gains, retention, transfer, grading reliability, and cost be measured? | Need identified; study design, baselines, and thresholds remain open. |
| O05 | What eventually funds free access? | Individual payments, institutions, sponsorship, or combinations remain possibilities, not commitments. |
| O06 | Build an LMS or integrate with existing institutional systems? | Deferred until institutional requirements are understood. |

## Evidence and review limits

The architecture findings are based on source inspection at the snapshot above, including relevant test source. The app and tests were not executed during this review. No learner outcomes or production behavior have been measured. A failure path visible in code is distinct from a demonstrated real-world incident.

Existing tests and evaluation tooling are useful foundations; their presence does not establish educational efficacy.

## Sources

All repository links below are pinned to the reviewed commit.

- **R1:** [app/services/placement.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/placement.py)
- **R2:** [app/services/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/assessment.py)
- **R3:** [app/learning/rubric_grading.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/rubric_grading.py)
- **R4:** [app/learning/item_generation.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/item_generation.py)
- **R5:** [app/learning/lesson_plan.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/lesson_plan.py)
- **R6:** [app/services/session_runner.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/session_runner.py)
- **R7:** [app/agent/workflow.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/agent/workflow.py)
- **R8:** [app/learning/mastery.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/mastery.py)
- **R9:** [app/services/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/chat.py)
- **R10:** [app/services/agentic.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/agentic.py)
- **R11:** [app/services/lesson_plan.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/lesson_plan.py)
- **R12:** [app/api/deps.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/deps.py)

- [README](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/README.md) and [roadmap](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/docs/ROADMAP.md).
- [Evaluation harness](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/harness.py).
- [Bloom (1984), The 2 Sigma Problem](https://web.mit.edu/5.95/readings/bloom-two-sigma.pdf).

- **R13:** [app/learning/curriculum.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/curriculum.py)
- **R14:** [app/services/knowledge.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/knowledge.py)
- **R15:** [app/api/v1/knowledge.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/knowledge.py)
- **R16:** [app/models/knowledge.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/knowledge.py)
- **R17:** [app/services/content.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/content.py)
- **R18:** [app/rag/retrieval.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/retrieval.py)
- **R19:** [app/rag/adapters/pdf.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/adapters/pdf.py)
- **R20:** [app/rag/chunking.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/chunking.py)
- **R21:** [app/rag/pipeline.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/pipeline.py)
- **R22:** [app/services/turn_common.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/turn_common.py)

## Update history

| Date | Change |
| --- | --- |
| 2026-09-06 | Created tracker from the mission discussion and first adaptive-loop source review. Recorded 8 agreed directions, 21 proposals, and 6 open decisions. No implementation proposals marked approved, implemented, or validated. |

| 2026-09-06 | User accepted prior recommendations S01–S21. Added new proposals S22–S29 from the knowledge graph and content pipeline review; these remain proposed. No code modified or tests executed. |

| 2026-09-06 | First implementation pass. S54 (`929c390`) and S38 (`889df5f`) implemented on branch `fix/tracker-s54-s38`, originally off `0d9b7f8` (the same snapshot this review inspected) and later rebased onto merged `main`; the SHAs here are the post-rebase ones. Both claims were re-verified against current source before any change. `uv run poe check` green (604 passed, 3 skipped). Implemented means software correctness with tests, not educational validation. All other suggestions unchanged. |

| 2026-09-06 | S45 implemented (`d84f69c`) on the same branch, after PR #15 (Phase 9c) merged and the branch was rebased onto it. `uv run poe check` green (626 passed, 1 skipped). Note: an unrelated leftover `dev` learner row from earlier live testing caused 8 spurious failures until removed — the suite itself leaves none behind. |

## Remaining architecture autopsy — source pass

Review date: 2026-09-06. Same pinned repository snapshot as above. These findings extend S01–S29. User requested a comprehensive autopsy for later implementation; no application code was changed. S30–S63 are new proposals. Each entry includes a concrete second-pass check. Priorities describe urgency, not permission to implement.

Coverage: API identity/scope, assessments, knowledge model, ingestion/workers, notes/memory/profile, provider routing/costs, streaming/frontend, analytics, evaluation, CI and deployment artifacts. This is a broad source-based review, not exhaustive line-by-line certification, a live penetration test, a load test, or evidence that the app currently passes its tests. Third-party SDK/broker defaults and deployed infrastructure behavior require runtime verification. Findings describe visible code behavior or explicitly labeled failure scenarios.

### Important qualifications to earlier findings

- S09: an error-type classifier does exist in profile_estimators. It aggregates conceptual/procedural/careless classifications across past errors into a learner profile; it does not provide a validated per-attempt misconception diagnosis that selects the next discriminating question.
- S17: durable conversation messages exist; the missing durability concerns resumable graph/practice state and frontend reconstruction, not all history.
- Notes API GET routes are pure reads. The frontend triggers an explicit refresh POST after loading a stale note; older roadmap language is less precise.
- Existing chat/source/chunk/notes routes perform several real ownership checks. The multi-user findings target specific gaps and global authoring policy, not a claim that all data is unscoped.
- Redis broker delivery guarantees, SDK retries, and cloud perimeter controls were not executed or verified. Recommendations address missing application-level guarantees.

### S30 — Block unsafe outbound requests on every URL intake path

**Status:** Proposed · **Priority:** Before external access

**Evidence:** Learner-supplied URLs use default_fetch, which follows redirects and performs no public-address check. safe_fetch only protects the model-controlled tool and documents a DNS check/connect gap. Both retrieve whole responses before testing MAX_BYTES; robots responses have no size bound.

**Suggested change:** Use one outbound-fetch policy across user/model URLs, enforce destination safety at connection and redirect time, and bound streamed bytes including robots responses. Add egress limits appropriate to deployment.

**Second-pass check:** Internal/loopback/link-local targets and redirects are refused; oversized responses stop during transfer. Test through the deployed network boundary without probing real internal services.

**Code:** [app/rag/fetch.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/fetch.py), [app/services/ingestion.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/ingestion.py).

### S31 — Protect learner data from indirect prompt injection and unintended outbound disclosure

**Status:** Proposed · **Priority:** Before external access

**Evidence:** Retrieved passages are inserted into prompt context; the agent can both retrieve private learner materials and fetch arbitrary public URLs. The code documents that public-target exfiltration remains unmitigated. This is an exposed capability combination, not a demonstrated exploit.

**Suggested change:** Treat retrieved text as untrusted data, constrain tool egress and what data may enter URLs, validate tool arguments, and adversarially evaluate grading/memory extraction as well as chat. Prompt wording alone is insufficient.

**Second-pass check:** Hostile source text cannot cause private source content or learner history to be sent to an unauthorized destination or silently dictate grades.

**Code:** [app/services/turn_common.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/turn_common.py), [app/agent/tools.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/agent/tools.py), [app/services/agentic.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/agentic.py).

### S32 — Bind onboarding checkpoint identity to the authenticated learner

**Status:** Proposed · **Priority:** Before multi-user access

**Evidence:** Goal refinement accepts a caller-provided session_id and uses it as the checkpoint key without incorporating learner identity or checking ownership. Ordinary conversations do perform ownership checks.

**Suggested change:** Issue server-owned session identities, associate them with learner and purpose, and enforce ownership on resume. Extend the durable-state work in S17 to onboarding.

**Second-pass check:** Two learners cannot resume or overwrite each other's onboarding state, including when an identifier is known.

**Code:** [app/api/v1/onboarding.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/onboarding.py), [app/services/onboarding.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/onboarding.py).

### S33 — Define authority over shared assessment items

**Status:** Proposed · **Priority:** Before multi-user access

**Evidence:** Any current learner can create globally stored items and answer keys. Bank reuse can select those items for other learners. Authenticated identity alone does not establish trust to author shared assessment content.

**Suggested change:** Separate private draft items from approved shared items; restrict publication and version grading criteria. Coordinate with shared-graph ownership in S25.

**Second-pass check:** A learner-created question/key cannot silently become another learner's trusted assessment.

**Code:** [app/api/v1/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/assessment.py), [app/models/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/assessment.py), [app/services/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/assessment.py).

### S34 — Make attempts and turns idempotent and concurrency-safe

**Status:** Proposed · **Priority:** First

**Evidence:** Answer submissions have no attempt/idempotency key. Mastery updates read then write without a lock/version check. The route has no server-side per-conversation turn serialization. Retries can duplicate evidence; concurrent updates can lose changes or race checkpoint resumes.

**Suggested change:** Persist attempt/turn identity, deduplicate retried requests, serialize or version conflicting state changes, and use conflict-safe creation of learner state.

**Second-pass check:** A retried attempt updates mastery once; simultaneous distinct attempts are both retained and applied in an explicit order; overlapping turns have defined behavior.

**Code:** [app/schemas/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/schemas/assessment.py), [app/services/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/assessment.py), [app/learning/mastery.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/mastery.py), [app/api/v1/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/chat.py).

### S35 — Repair derived plans after committed assessments without regrading

**Status:** Proposed · **Priority:** High

**Evidence:** Mastery and event records commit before plan revision. If revision fails, the client can receive a failure despite a committed answer. Retrying the answer currently generates another observation.

**Suggested change:** Keep the deliberate authoritative-event/derived-plan distinction, but add durable pending revision work or reconciliation and return the committed attempt's status on retry.

**Second-pass check:** Inject a plan-revision failure after answer commit: the grade is visible, applied once, and the plan catches up without another assessment.

**Code:** [app/services/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/assessment.py), [app/services/lesson_plan.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/lesson_plan.py).

### S36 — Make database-to-queue delivery recoverable

**Status:** Proposed · **Priority:** Before reliable external use

**Evidence:** Source creation commits before enqueue. A queue failure can leave a pending source without a job. Ingestion catches errors, marks FAILED, and returns normally; the task wrapper does not turn that state into a retry decision.

**Suggested change:** Use durable dispatch intent, reconciliation for stranded sources, explicit retry/backoff classification, and an operator/learner retry surface. Clean up blobs orphaned by failed database commits.

**Second-pass check:** Simulate Redis failure immediately after upload commit; the same source is eventually processed without duplicate uploads. Transient failures retry and terminal failures remain diagnosable.

**Code:** [app/api/v1/sources.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/sources.py), [app/services/ingestion.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/ingestion.py), [app/workers/tasks.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/workers/tasks.py).

### S37 — Give long ingestion jobs visible state, ownership, and resource budgets

**Status:** Proposed · **Priority:** Before reliable external use

**Evidence:** PROCESSING is flushed but not committed until completion, so other sessions cannot reliably observe it. Long extraction/model work runs inside the transaction. There is no explicit job claim/lease preventing duplicate processing. A 1 GiB byte cap does not bound pages, decoded media, chunks, model calls, or total spend.

**Suggested change:** Use short transactions, committed job status, claim/lease and recovery, stage checkpoints where useful, and per-job decoded-content/time/cost limits. Enforce global concurrency as well as per-job limits.

**Second-pass check:** Polling sees progress; killed jobs are recovered; duplicate deliveries do not run the same work concurrently; adversarially large documents hit explicit resource limits.

**Code:** [app/services/ingestion.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/ingestion.py), [app/rag/pipeline.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/pipeline.py), [app/workers/broker.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/workers/broker.py), [app/core/config.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/core/config.py).

### S38 — Fix note catch-up cursors so activity cannot be skipped

**Status:** Implemented (`889df5f`, branch `fix/tracker-s54-s38`) · **Priority:** First

**Implemented:** The shared watermark is split into per-stream cursors (`Note.messages_watermark`,
`Note.events_watermark`; migration `0018`, both seeded from the value they replace so the boundary
neither re-reads nor skips; downgrade takes `LEAST()` because re-reading is recoverable and skipping
is not). Each page is also tie-safe: `created_at` is `server_default=func.now()`, i.e.
transaction-start time, so one answer tagged to several KCs writes several events at the identical
instant — a truncated page now drops its trailing partial group and stops just below it, leaving the
group whole for the next pass. One instant holding more rows than an entire page logs a warning
rather than truncating silently. Second-pass check covered by four tests, including an end-to-end
one asserting the note stays *stale* while a truncated backlog remains (previously it reported done).
Software correctness only; no educational validation claimed.

**Evidence:** _gather independently limits messages and outcomes, then advances one watermark to the maximum timestamp across both. If one stream is truncated before that maximum, unread rows in that stream are skipped on the next refresh. Strict timestamp comparison can also skip ties at a batch boundary.

**Suggested change:** Use independent stable cursors per stream, such as timestamp plus ID, or a single ordered durable activity sequence with a safe common frontier.

**Second-pass check:** Backlogs exceeding both limits, unequal stream rates, and tied timestamps are processed exactly once without losing activity.

**Code:** [app/services/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/notes.py).

### S39 — Give note distillation explicit topic and validated concept provenance

**Status:** Proposed · **Priority:** First

**Evidence:** Transcript gathering includes all subject conversations. distill receives atoms/transcript/outcomes/reading level, but no topic identity, description, or allowed KC catalog. The prompt says 'this topic' without defining it. Atom KC IDs and provenance are only loosely shape-checked.

**Suggested change:** Pass the topic and candidate KCs, scope or label evidence, and validate returned references against actual input records. Preserve message/attempt/source lineage.

**Second-pass check:** Conversation about one topic does not populate every sibling note, particularly when notes start empty; all stored references resolve to evidence actually supplied.

**Code:** [app/services/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/notes.py), [app/learning/note_distill.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/note_distill.py).

### S40 — Preserve learner-authored note content and edits faithfully

**Status:** Proposed · **Priority:** High

**Evidence:** _learner_atoms_preserved only checks IDs; a retained ID with changed text/kind passes. Direct learner edits are interpreted by an LLM and rerendered, so exact edits are not stored as the authoritative document. Mutation requests have no expected revision.

**Suggested change:** Preserve immutable learner-authored text or explicit edit patches, use optimistic revision checks, and make AI rewrites distinguishable from user edits. Strengthen invariants beyond ID presence.

**Second-pass check:** A model cannot replace a learner atom's meaning while keeping its ID; concurrent edits produce an explicit conflict; the original submitted edit is recoverable.

**Code:** [app/learning/note_distill.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/note_distill.py), [app/services/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/notes.py), [app/api/v1/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/notes.py).

### S41 — Bound cumulative note growth and tolerate rendering failures

**Status:** Proposed · **Priority:** High

**Evidence:** Every distillation resends the entire substrate and requests a replacement with a fixed 4096-token output cap. render accepts any stripped string, including empty/truncated output, as cacheable content. Revision/render work is coupled in a transaction.

**Suggested change:** Use incremental or section-level updates with bounded context, validate render completion, and provide deterministic fallback rendering of the retained substrate.

**Second-pass check:** Large notes retain existing content; empty/truncated model output does not become a successful final render; a provider failure leaves a readable recoverable note.

**Code:** [app/learning/note_distill.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/note_distill.py), [app/services/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/notes.py).

### S42 — Make memory correction and forgetting durable

**Status:** Proposed · **Priority:** Before trusted longitudinal use

**Evidence:** Memory dedup skips semantically similar entries rather than reconciling corrections. Retrieval always returns nearest entries without a relevance floor. Deletions have no tombstone and can be re-extracted from the same history; conversation deletion deliberately leaves memories behind.

**Suggested change:** Track evidence, current/superseded status, user corrections, and deletion suppression. Filter retrieval by relevance and policy. Define explicit conversation-memory-note deletion semantics.

**Second-pass check:** A corrected preference replaces outdated guidance; irrelevant memories are omitted; deleting a memory prevents its recreation from the same evidence.

**Code:** [app/services/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/memory.py), [app/models/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/memory.py), [app/memory/retrieval.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/memory/retrieval.py).

### S43 — Make memory/profile refresh scheduling explicit and incremental

**Status:** Proposed · **Priority:** High

**Evidence:** Memory write-back and profile refresh are on-demand service operations. Memory extraction revisits only a recent-message window; profile refresh loads all learner events and user messages. Neither service establishes a durable incremental processing cursor.

**Suggested change:** Choose an explicit session/turn/job trigger and bounded incremental work. Track completed input ranges and expose refresh failures rather than relying on incidental UI visits.

**Second-pass check:** A completed session produces intended updates without visiting a special screen; repeated refreshes do not repeatedly pay for unchanged evidence; old unprocessed material is not silently dropped.

**Code:** [app/services/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/memory.py), [app/services/profile.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/profile.py).

### S44 — Treat learner profile measures as provisional proxies, not measured traits

**Status:** Proposed · **Priority:** High

**Evidence:** Reading level is calculated from learner message text with a readability formula. Cognitive-load tolerance is a within-session score difference. Format effectiveness uses average scores by item type, and the planner picks the highest mean. Difficulty, subject, assistance, and exposure can confound these measures. This is a code-level interpretation concern, not a literature validation.

**Suggested change:** Rename proxies honestly, condition on task/context where possible, use explicit preferences for presentation, and test whether interventions improve independent outcomes. Avoid routing learners toward easier formats solely because scores are higher.

**Second-pass check:** The dashboard describes what was observed; casual short writing does not assert low reading ability; format selection is evaluated on later transfer/retention rather than immediate score alone.

**Code:** [app/learning/profile_estimators.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/profile_estimators.py), [app/learning/lesson_plan.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/lesson_plan.py).

### S45 — Separate attempts from per-component events in analytics and profiling

**Status:** Implemented (`d84f69c`, branch `fix/tracker-s54-s38`) · **Priority:** High

**Implemented:** `learning_events.attempt_id` (migration `0019`) carries one id across a single
graded answer's whole per-KC fan-out. Each KC keeps its own evidence row and its own state update —
only the *counting* changes. Historical rows are backfilled by grouping an observation's
(learner, item, instant), which is exactly how the fan-out was written; a row with no `attempt_id`
is treated as its own attempt, so pre-migration rows and non-attempt events such as
`placement_seed` are never collapsed together.

Confirmed while implementing that every payload field except `weight` — score, difficulty,
`latency_ms`, `hints_used`, `item_id`, `response` — is item-level and identical across the fan-out,
and that **no estimator reads `kc_id` or `weight`**. Deduplicating at the shared `_observations()`
entry point is therefore correct for all nine of its call sites: pace, optimal challenge, cognitive
load, error type, help-seeking, persistence, engagement, session logistics and format effectiveness.
Format effectiveness was the most consequential — a multi-KC MCQ counted triple toward "this format
works for this learner", which the planner then acts on. `get_activity` now counts distinct
attempts, so momentum reflects learner effort rather than KC-tagging breadth.
`_estimate_persistence`'s manual `(item_id, created_at)` workaround is now redundant for the
fan-out and retains only its real job, grouping retries of the same item.

Software correctness only; the calibration question of whether these dimensions *should* drive
teaching decisions is S18/S44 and remains open.

**Evidence:** One answer creates one event per tagged KC. Activity counts each observation row, and profile estimators treat those rows as samples. A three-KC answer can therefore count three times for activity and some statistical summaries.

**Suggested change:** Introduce a shared attempt identifier and aggregate at the appropriate level; retain component-level evidence without treating it as independent learner actions.

**Second-pass check:** An answer tagged to three components counts as one attempt in activity and latency metrics while still updating the intended three component states.

**Code:** [app/learning/mastery.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/mastery.py), [app/learning/profile_estimators.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/profile_estimators.py), [app/services/analytics.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/analytics.py).

### S46 — Make mastery displays match the estimator's meaning

**Status:** Proposed · **Priority:** High

**Evidence:** masteryPercent applies sigmoid to ability. An unseen prior of zero displays as 50%; under this estimator sigmoid(theta) is expected performance against difficulty zero, not percentage of a subject understood. Uncertainty labels do not resolve this mismatch.

**Suggested change:** Show unassessed coverage explicitly, distinguish estimated task performance from demonstrated mastery, and define dashboard denominators and calibration.

**Second-pass check:** Unseen components are labeled unassessed; displayed percentages have a documented interpretation supported by validation.

**Code:** [frontend/src/lib/mastery.ts](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/lib/mastery.ts), [app/learning/tracer.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/tracer.py), [app/services/analytics.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/analytics.py).

### S47 — Enforce input, context, and total-request budgets

**Status:** Proposed · **Priority:** Before external access

**Evidence:** Chat content has a minimum length but no maximum. Plain/agentic chat load and forward complete conversation history. Output caps and tool iteration limits exist, but do not bound cumulative input context, learner spend, or concurrent requests.

**Suggested change:** Use bounded context assembly, retained summaries plus relevant evidence, per-request/learner budgets, explicit deadlines and cancellation, and production rate/concurrency controls.

**Second-pass check:** Long-running conversations stay within supported context and cost limits; oversized input is rejected before paid calls; repeated parallel requests respect a learner budget.

**Code:** [app/schemas/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/schemas/chat.py), [app/services/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/chat.py), [app/services/profile.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/profile.py), [app/llm/registry.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/registry.py), [app/core/config.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/core/config.py).

### S48 — Make model-call accounting complete and independent of business transactions

**Status:** Proposed · **Priority:** Before cost decisions

**Evidence:** Unknown models are priced as zero. Embeddings return only vectors and lose usage. Curriculum generation discards usage; onboarding refinement does not persist it. Many logs occur after successful parsing/stream completion or inside transactions that can roll back after paid work.

**Suggested change:** Instrument the provider boundary with request identity, model/provider/prompt version, timing, usage, failure/partial status, and known/unknown price. Reconcile estimates with actual billing.

**Second-pass check:** Unknown price is not free; failed parses and rolled-back operations retain usage records; embeddings and onboarding costs appear in per-learner/session totals.

**Code:** [app/llm/pricing.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/pricing.py), [app/llm/registry.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/registry.py), [app/llm/providers/openai_compat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/providers/openai_compat.py), [app/learning/curriculum.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/curriculum.py), [app/services/onboarding.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/onboarding.py), [app/rag/pipeline.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/pipeline.py).

### S49 — Make provider compatibility an explicit contract

**Status:** Proposed · **Priority:** High

**Evidence:** The OpenAI-compatible streaming adapter emits assembled tool calls only when a chunk includes usage. A compatible endpoint that finishes tool calls without a usage chunk can lose them. Registry configuration does not validate role capabilities or provider names up front.

**Suggested change:** Finalize tool calls from stream completion independently of usage; validate capabilities/config at startup; define truncation, missing usage, retries, timeouts, and resource cleanup behavior.

**Second-pass check:** Provider contract tests cover tool-call completion without usage, malformed arguments, early disconnect, rate limit, and role/model capability mismatch.

**Code:** [app/llm/providers/openai_compat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/providers/openai_compat.py), [app/llm/registry.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/registry.py).

### S50 — Version embedding spaces and plan migration explicitly

**Status:** Proposed · **Priority:** Before embedding changes

**Evidence:** Chunks and memories store vectors with a configured dimension but no embedding model/version identity. Changing to a same-dimension model via configuration can silently query incompatible stored vectors; a different dimension additionally requires schema changes.

**Suggested change:** Record embedding-space identity and extraction/chunking versions; re-embed or dual-index during migration and reject incompatible queries.

**Second-pass check:** A model change never silently mixes vector spaces, including two models with identical dimensions.

**Code:** [app/models/source.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/source.py), [app/models/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/memory.py), [app/llm/registry.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/llm/registry.py), [app/core/config.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/core/config.py).

### S51 — Persist turn lifecycle and handle interrupted streams explicitly

**Status:** Proposed · **Priority:** Before reliable external use

**Evidence:** The user message commits before generation, while the assistant response commits only after streaming completes. Client stream EOF without a terminal event is treated as normal completion. The hook has no wired abort/cleanup and retries have no durable turn identity.

**Suggested change:** Store pending/completed/failed/cancelled turns, define partial-output policy, detect missing terminal events, and support retry/resume without duplicating the user turn.

**Second-pass check:** Disconnect before and after commit produces consistent history, a visible recoverable status, and no duplicate assessment or message.

**Code:** [app/services/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/chat.py), [app/services/agentic.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/agentic.py), [frontend/src/api/sse.ts](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/api/sse.ts), [frontend/src/hooks/useChatConversation.ts](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/hooks/useChatConversation.ts).

### S52 — Persist conversation phase instead of inferring it from missing goals

**Status:** Proposed · **Priority:** High

**Evidence:** awaitingGoalAccept treats any last assistant message with no committed goal as a proposal, including an agentic response. Backend modes can bypass refinement. Practice state and outcome remain local to live SSE in the frontend.

**Suggested change:** Expose authoritative conversation/turn phase and active practice state; restore it on reload. Define mode switching while a workflow is paused.

**Second-pass check:** An agentic answer is not displayed as a goal proposal; refresh restores active item and completed status; mode switches do not resume the wrong state.

**Code:** [frontend/src/hooks/useChatConversation.ts](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/hooks/useChatConversation.ts), [app/api/v1/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/chat.py), [frontend/src/pages/Session.tsx](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/pages/Session.tsx).

### S53 — Support technical content rendering and working citations in practice

**Status:** Proposed · **Priority:** High for initial audience

**Evidence:** Chat renders plain text with citation buttons, not Markdown/math/code. Notes use basic ReactMarkdown without math plugins. Session passes a no-op citation click handler, so its citation controls cannot open evidence.

**Suggested change:** Use a safe shared renderer for Markdown, equations, code, and citations; wire the citation pane into guided practice and verify keyboard/readability behavior.

**Second-pass check:** A linear-algebra derivation and code sample render correctly; every displayed actionable citation works in both chat and practice.

**Code:** [frontend/src/components/chat/MessageBlock.tsx](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/components/chat/MessageBlock.tsx), [frontend/src/pages/NoteView.tsx](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/pages/NoteView.tsx), [frontend/src/pages/Session.tsx](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/src/pages/Session.tsx), [frontend/package.json](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/package.json).

### S54 — Separate public item presentation from secret answer keys

**Status:** Implemented (`929c390`, branch `fix/tracker-s54-s38`) · **Priority:** High

**Implemented:** `app/learning/item_presentation.py` decides per item type which part of an
`answer_key` is public — a **whitelist**, so a new answer-key field cannot leak by omission. MCQ
`choices` are exposed via a new `ItemRead.presentation`; the `correct` index is not. Cloze and
fill-blank keys *are* the answers and stay withheld. Fixes every read path at once, since all five
(`GET /items`, due reviews, placement, chat practice, workflow) go through `item_to_read`. Response
validation added: a missing, non-integer, or out-of-range MCQ `choice`, and a missing `blanks` list,
now raise `InvalidResponse` → 422 instead of grading 0.0 and tracing an observation that the learner
does not know the material; a short-but-valid `blanks` list still grades partially.

**Behavior change:** an empty response previously scored 0.0. The unit test and the `mcq-no-answer`
golden eval case both encoded that; the test now asserts rejection and the golden case was removed,
since the harness compares scores and has no notion of a rejected response.

**Deliberately deferred:** the flashcard reveal operation. `answer_key["back"]` stays withheld and no
reveal endpoint was added — that is a new capability needing a UX decision about when reveal is
allowed relative to self-rating, not part of this defect. The whitelist makes it a small follow-up.

**Evidence:** MCQ choices are stored inside answer_key, while ItemRead withholds that entire object. Generated stems are separate from choices. The learner-facing response therefore omits the choices needed to answer those generated MCQs. Flashcard reveal content is similarly housed in answer_key.

**Suggested change:** Define type-specific presentation payloads, response validation, and deliberate reveal operations while keeping correctness keys private.

**Second-pass check:** Generated MCQs display all options without the correct index; flashcards can reveal their back; invalid response shapes are rejected rather than treated as knowledge failure.

**Code:** [app/models/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/assessment.py), [app/schemas/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/schemas/assessment.py), [app/services/assessment.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/assessment.py), [app/learning/item_generation.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/item_generation.py).

### S55 — Validate source scope changes and refresh derived tags

**Status:** Proposed · **Priority:** High

**Evidence:** Source subject/topic IDs are not checked for consistent parentage. Onboarding reassigns subject but does not clear a conflicting topic or retag existing chunks. Unscoped ingestion has no candidate KCs; retrieval currently does not use ChunkKC joins despite paying for tags where present.

**Suggested change:** Validate source scope, retag/reconcile after curriculum assignment, and either connect KC tags to retrieval/coverage or defer their cost until evaluated.

**Second-pass check:** A source cannot belong to a topic in a different subject; reassignment leaves consistent tags; tag-based retrieval shows measured value over its baseline.

**Code:** [app/api/v1/sources.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/api/v1/sources.py), [app/services/knowledge.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/knowledge.py), [app/learning/kc_tagging.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/kc_tagging.py), [app/rag/retrieval.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/rag/retrieval.py).

### S56 — Make event replay reproduce production learner state

**Status:** Proposed · **Priority:** High

**Evidence:** Mining keeps only score/difficulty per step. Replay starts from the default prior and omits production time decay, placement seeds, and multi-KC weights. The scoring code therefore does not replay the full production update path.

**Suggested change:** Version events and estimator configuration; retain timestamps, weights, attempts, initial seeds, item/rubric versions, and prediction-before-observation. Build exact replay before model comparison.

**Second-pass check:** Replaying an unchanged production history reproduces stored ability/uncertainty within tolerance, including placement, gaps, and multi-component questions.

**Code:** [tests/eval/datasets/mine.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/datasets/mine.py), [tests/eval/datasets/calibration.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/datasets/calibration.py), [app/learning/mastery.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/mastery.py).

### S57 — Ensure sweep settings actually change execution

**Status:** Proposed · **Priority:** High

**Evidence:** gen_config is logged but run_cell does not apply it. Only strict_kc_tagging is implemented as a toggle in the runner. Logging unsupported settings can make an experiment appear to compare configurations it never used.

**Suggested change:** Apply supported generation/prompt/toggle settings through the actual call path and reject unsupported ones. Record complete resolved configuration, dataset and prompt revisions, and model identities.

**Second-pass check:** A changed supported parameter is observable at provider invocation; an unsupported parameter fails validation instead of producing a misleading comparison.

**Code:** [tests/eval/sweep/runner.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/sweep/runner.py), [tests/eval/sweep/orchestrate.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/sweep/orchestrate.py).

### S58 — Expand CI to cover the delivered product and actual failure boundaries

**Status:** Proposed · **Priority:** Before release

**Evidence:** CI builds/tests the backend and applies migrations, but has no frontend install/build/lint or browser workflow. Backend fixtures use one transactional session with savepoints, and many workflow tests use canned model responses; these do not establish separate-worker or real-commit behavior.

**Suggested change:** Add frontend and API-contract gates, focused end-to-end journeys, separate-session concurrency tests, queue integration, and fault injection. Add migration tests against representative existing data, not only a fresh database.

**Second-pass check:** Upload→curriculum→chat→practice→notes works in a real browser; retry/restart/ownership failures are covered; incompatible frontend/backend changes block release.

**Code:** [.github/workflows/ci.yml](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/.github/workflows/ci.yml), [frontend/package.json](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/frontend/package.json), [tests/conftest.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/conftest.py), [tests/test_workflow.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/test_workflow.py).

### S59 — Separate software correctness, model quality, and educational effectiveness gates

**Status:** Proposed · **Priority:** High

**Evidence:** Existing tests and suites cover useful mechanics and model task scores, but the reviewed release workflow does not establish durable independent learner gains. Model-scored event labels can reproduce the model's errors.

**Suggested change:** Maintain distinct deterministic, expert-labeled model-quality, and learner-outcome evaluations; use held-out learners/items, delayed unassisted tasks, baselines, and uncertainty. Do not use only the production grader as the judge of its own teaching.

**Second-pass check:** Report which gate passed; quantify grading agreement and independently assessed learning outcomes without treating test pass rate as learning efficacy.

**Code:** [tests/eval/harness.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/harness.py), [tests/eval/datasets/calibration.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/tests/eval/datasets/calibration.py), [.github/workflows/ci.yml](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/.github/workflows/ci.yml).

### S60 — Provide an operational release and recovery path

**Status:** Proposed · **Priority:** Before production

**Evidence:** Compose explicitly provides local dependencies, not a deployable API/frontend/worker release. /health is liveness only. The reviewed repo has no demonstrated backup/restore, deployment rollback, readiness, worker-lag alerts, or production configuration validation.

**Suggested change:** Create a minimal deployment/runbook with explicit secrets/config, readiness checks, worker/error/cost monitoring, backups and restore drills, and compatible migration rollout. Keep the current modular backend unless measured needs justify service extraction.

**Second-pass check:** Rebuild a clean environment, restore a backup, roll out and roll back a compatible release, and detect a failed queue/worker before users report stalled learning.

**Code:** [docker-compose.yml](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/docker-compose.yml), [app/main.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/main.py), [app/core/config.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/core/config.py), [.github/workflows/ci.yml](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/.github/workflows/ci.yml).

### S61 — Define retention, export, deletion, and diagnostics policy across all stores

**Status:** Proposed · **Priority:** Before external learner data

**Evidence:** Deleting a conversation leaves memories by design. Raw blobs, extracted text, notes, events and experiment exports form separate stores; a whole-learner deletion/export workflow is not demonstrated. Some error paths log raw model replies or exception text.

**Suggested change:** Define intentional retention/deletion relationships, implement learner export/deletion with background reconciliation, and redact diagnostics. Test provenance-dependent artifacts and pending jobs as part of deletion.

**Second-pass check:** An export is usable; deletion removes the intended data and prevents pending jobs from recreating it; diagnostics do not unnecessarily retain sensitive content.

**Code:** [app/services/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/memory.py), [app/services/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/chat.py), [app/models/memory.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/models/memory.py), [app/services/ingestion.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/ingestion.py), [app/learning/curriculum.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/curriculum.py).

### S62 — Reduce repeated database work as learner history grows

**Status:** Proposed · **Priority:** Supporting

**Evidence:** Analytics loads per-KC estimates and then rollups reload them; notes index performs multiple queries per topic. Profile refresh and message listing load full histories. These costs grow with exactly the long-term use Guru seeks.

**Suggested change:** Batch state reads, reuse computed estimates, paginate user-visible history, and incrementally aggregate activity/profile data. Set query-count and latency budgets before considering more services.

**Second-pass check:** Representative long-lived accounts meet defined latency/query budgets with realistic graph sizes; optimizations preserve estimates and ordering.

**Code:** [app/services/analytics.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/analytics.py), [app/learning/mastery.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/learning/mastery.py), [app/services/notes.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/notes.py), [app/services/profile.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/profile.py), [app/services/chat.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/chat.py).

### S63 — Keep the full learning goal visible when planning is capped

**Status:** Proposed · **Priority:** High

**Evidence:** Plan generation slices prerequisite-ordered KCs to lesson_plan_max_steps (20). There is no explicit remaining-goal continuation in that generation path. A prerequisite-heavy goal may have its target omitted from the current plan.

**Suggested change:** Represent the complete objective separately from the active horizon, expose deferred steps and completion criteria, and extend the horizon as work completes.

**Second-pass check:** A goal requiring over 20 components continues through the actual target; completing the first window is not presented as completing the entire goal.

**Code:** [app/services/lesson_plan.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/services/lesson_plan.py), [app/core/config.py](https://github.com/alykapasi/guru-alt/blob/0d9b7f8abb1c623d0c46f3a53dda210a4790289f/app/core/config.py).

## Implementation order for consideration

1. Prevent misleading evidence/data loss: S13–S14, S34–S35, S38–S40, S45–S46, S54, S56.
2. Close external-access boundaries before inviting independent users: S21, S25, S30–S33, S36–S37, S47–S48, S61.
3. Prove one complete learning journey: S08–S12, S15–S18, S22–S28, S39, S44, S53, S55, S63.
4. Make continued usage dependable: S17, S29, S41–S43, S49–S52, S58, S60, S62.
5. Validate educational and financial results before expanding cohorts: S03–S05, S48, S56–S59.

These groups overlap intentionally: an issue may affect both trust and teaching. No microservices rewrite, LMS build, or DKT upgrade is required simply to address these findings.

## What to bring to the second pass

- The updated repository commit and a mapping from suggestion IDs to implemented changes; mark deviations and deliberate deferrals.
- Setup instructions and a seeded, non-sensitive reference learning journey.
- Test/CI results and evaluation dataset versions, with failure-injection and replay cases for the repaired guarantees.
- Known remaining limitations and evidence for any claims about learning gains or cost.

The second pass should inspect the implementation, run feasible checks, and distinguish implemented behavior from validated educational effectiveness. Until then, the review records proposals and source findings, not verified repairs.

Update: 2026-09-06 — Added S30–S63 (34 new proposals), second-pass checks, review qualifications, and provisional implementation order. S01–S21 remain accepted; S22–S63 remain proposed.


## Subsequent discussion — founder testing and operating context

Recorded 2026-09-06. These updates extend the context without implying implementation of prior suggestions.

| ID | Direction / context | Status |
| --- | --- | --- |
| D09 | Let the product and business direction emerge through repeated use, feedback, and iteration; preserve the mission without prescribing a final product shape. | Agreed direction |
| D10 | Continue strategy discussion while development happens separately, then review the updated repository in a second pass. | Agreed direction |
| D11 | Complete essential repairs, undertake sustained founder use with a capable and responsive configuration, then begin observed external sessions before a longer pilot. | Agreed direction |
| D12 | Founder can devote substantial time; available funding depends on execution. No funding amount, source, commitment, or runway has been specified. | User-provided context |

| ID | Suggestion | Why / intended result | Status |
| --- | --- | --- | --- |
| S64 | During sustained founder testing, distinguish model, product/state, and serving failures; keep the initial model configuration stable enough to compare iterations. | Avoid attributing weak inference or latency entirely to product design, or attributing missing state and evidence entirely to the model. | Proposed |
| S65 | Use observed first-use sessions before the longer pilot, with readiness proportional to the exposed capabilities. | A supervised narrow trial need not wait for every architecture item, while independent accounts require adequate isolation and recovery. | Accepted |
| S66 | Separate learner, expert-reviewer, and buyer/sponsor feedback within the accessible senior-professional cohort. | Product enthusiasm, teaching accuracy, learning outcomes, and commercial demand are different evidence. | Accepted |
| S67 | Evaluate each iteration through observed learning, recurring obstacles, voluntary return, and the next question to investigate. | Maintain rigor while allowing product direction to evolve. | Agreed direction |

The detailed alpha and pilot plan is maintained in `guru-alpha-readiness-and-pilot.md`. It now includes Stage 0 (sustained founder use). Prior suggestion statuses remain unchanged; no new implementation has been verified.

## Competitive landscape — first research pass

Recorded 2026-09-06. Detailed sources, scope qualifications, and a proposed comparison plan are maintained in `guru-competitive-landscape.md`.

The current official product descriptions show substantial overlap with Guru's intended learning loop. This is evidence about documented alternatives, not an independent test of their quality or of Guru's relative performance. Neither a unique feature position nor a superior learning outcome has been established. The new recommendations below preserve the agreed approach of letting product direction emerge through use and iteration.

| ID | Suggestion | Why / intended result | Status |
| --- | --- | --- | --- |
| S68 | Base positioning on observed comparative value for a learning task; maintain an accurate view of documented competitor capabilities. | Avoid assuming that adaptive explanations, memory, skill tracking, source-based study tools, or reassessment are unique. | Proposed |
| S69 | Add a small comparison against suitable current alternatives during founder testing. Record product/configuration, input material, starting knowledge, and order effects. | Establish a useful reference for Guru's behavior and experience. Scripted diagnostic cases evaluate behavior; learning a topic in one product contaminates a later same-topic learning comparison. | Proposed |
| S70 | Ask first users what they actually use to learn, what they postpone, and what they choose after trying Guru. | Find unmet learning jobs and switching reasons within the accessible network. Professional seniority alone does not identify a learner segment, buyer, or willingness to pay. | Proposed |
| S71 | Build reusable, reviewed cases linking learning obstacles, diagnostic evidence, interventions, and subsequent independent work; respect permissions for reuse. | Improve teaching and evaluation through evidence. Accumulated data or a heavier estimator becomes an advantage only if it improves relevant decisions or outcomes. | Proposed |
| S72 | Compare diagnosis quality, new independent application, delayed retention, learner effort, and voluntary return as separate signals. | Extend S03, S14, S59, and S67 into competitive testing without mistaking enthusiasm, completion, or model-generated grades for the entire learning result. | Proposed |

These entries are proposed, not accepted, implemented, or validated. Research authorization does not approve a fixed positioning, permanent first subject, new development schedule, or product endpoint. The earlier alpha document and suggestion statuses are unchanged.

Next strategy topic: delivery economics and plausible payers, followed by evidence from actual adoption and distribution. Funding commitments, pricing, and runway remain unspecified.

## Market coexistence and acquisition possibilities

Recorded 2026-09-06. The user challenged whether incumbent competition rules out a viable segment and raised acquisition by a larger technology provider as a possible outcome. This is exploration, not a decision to sell or an acquisition-led business plan. The discussion is assumed to concern Guru; the user's message used the name Saga.

Clarification to the competitive assessment: commercial viability does not require unprecedented features or broad technical superiority. A particular audience might prefer the overall experience, fit, support, convenience, or delivery arrangement. That preference must be sufficient to support adoption, continued use, and sustainable provision. Competitors' existence does not establish failure; market size or incomplete incumbent coverage does not guarantee demand for Guru either. Better learning remains a mission objective, while the commercial reason for choosing Guru may include other benefits.

| ID | Suggestion | Why / intended result | Status |
| --- | --- | --- | --- |
| S73 | Evaluate Guru's full value to a reachable audience, including experience and fit, without requiring an unprecedented feature or superiority across the entire category. | A sustainable service can coexist with larger providers. Evidence should establish why people choose and continue using it. | Proposed |
| S74 | Distinguish the requirements for a sustainable business, substantial expansion, and a strategic acquisition when evaluating progress. | These are different possible outcomes. The access mission does not require adopting maximum market dominance or an acquisition as an immediate success criterion. | Proposed |
| S75 | Keep acquisition as an optional outcome. Build useful assets and investigate strategic fit when there is evidence of interest; do not make the operating plan depend on a named buyer. | A buyer might value technology, a team, customers, distribution, or accumulated instructional knowledge. No interest from OpenAI, Meta, Apple, or another buyer has been established. Any actual offer would also need evaluation against the access mission. | Proposed |

Relevant precedents: Google stated in its [August 2019 Socratic announcement](https://blog.google/products-and-platforms/products/education/socratic-by-google/) that it had acquired the learning app the prior year. Workday [completed its acquisition of Sana in November 2025](https://newsroom.workday.com/2025-11-04-Workday-Completes-Acquisition-of-Sana), describing a combination of enterprise knowledge, agents, and learning capabilities. These examples establish that such transactions occur; they do not estimate Guru's acquisition probability or identify an interested buyer.

S73–S75 are new proposals. No market segment, buyer, commercial demand, acquisition probability, or implementation result has been validated in this discussion.
