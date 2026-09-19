# Guru v0 — accepted decisions and remaining delivery work

Recorded: 2026-09-16. Source: the maintainer's numbered responses in the **Review Guru product
progress** task. This record supersedes conflicting earlier proposals in the masterplan, roadmap,
and suggestions tracker. It records agreed behavior, not a claim that the behavior has shipped.

## Release objective

Complete the remaining product and engineering work toward independent use by invited adults,
following sustained founder testing. Subjects remain unrestricted. Keep the current modular
application, provider-role abstraction, continuous estimator, and separate FSRS retention model.
Evaluation starts with estimator calibration and grading agreement. Business strategy is outside
this development effort.

## Accepted product decisions

| Decision | Accepted behavior | Related work |
| --- | --- | --- |
| V01 — Release | Independent invited-adult use, with founder testing first. No public-registration launch is implied. | S08, S21, S58–S60 |
| V02 — Goal status | Achievement is a threshold-based assessment with time-sensitive confidence. Sustained evidence substantially above the threshold can establish stronger understanding. Learners may explicitly mark a goal done. Use `ability - 2 * current_uncertainty`; calibrate the sustained-evidence duration from data. | O03, S01, S12, S14, S18, S46, S59 |
| V03 — Ownership | Learner-generated curricula, graphs, questions, and teaching material are private by default. Sharing requires an explicit reviewed publication path; private-source-derived material stays private. | S25, S29, S33 |
| V04 — Transfer | Link genuinely equivalent concepts across subjects, preserve the context of the original evidence, and use a short confirmation when the new application demands it. Names alone do not establish equivalence or merge estimates. | S24, S22 |
| V05 — Sources | General knowledge may supplement learner-selected sources with clear attribution. Offer a sources-only mode. Unassigned material is not silently added to an unrelated conversation. | S26–S28, S55 |
| V06 — Web scope | All URL ingestion and external web access are disabled for v0, including learner-supplied links. Crawling/discovery may return in v1/v2. File uploads and existing stored material remain available. | S30–S31, Phase 6 |
| V07 — Guidance | Exploration proposes substantial prerequisite detours; guided mode may take them automatically. Both allow skipping. Side discussions pause practice, with explicit resume/skip behavior and preserved attempt state. | S09, S11, S15, S16, S52 |
| V08 — Flashcards | Think, reveal, then self-rate. Self-rated flashcards update retention scheduling; independently graded answers supply ability evidence. Preserve the distinction in events, analytics, and replay. | S10, S13, S14, S54, S56 |
| V09 — Preferences | Explicit learner settings win. Provide global defaults and subject overrides. Adapt within the selected guidance level; inferred preferences remain inspectable and resettable. | S16, S43–S44 |
| V10 — Notes | Preserve the exact wording of learner edits. Automatic additions belong in editable surrounding sections; conflicting rewrites are suggestions. Retain revision history. | S38–S41 |
| V11 — Removal | Distinguish archiving from deletion. Offer an explicit action to also forget derived learning from a source or conversation, backed by provenance and evidence recomputation. Ordinary deletion explains separately retained artifacts. | S42, S61 |
| V12 — Retention | Learning artifacts remain until explicitly deleted. Initial configurable diagnostic/backup retention is 30 days. Account deletion disables access immediately, with a seven-day recovery window and an explicit erase-now path. Apply expiry selectively: preserve durable learning history, learner edits, and required deletion-suppression records. | S17, S29, S61 |
| V13 — Alpha admin | Give authenticated administrators broad sudo privileges for alpha: inspect and act across learner accounts. Do not require the learner-enabled support access proposed earlier. Keep the actual actor, effective learner, actions, and reason visible in an audit trail. Fine-grained roles can follow alpha. | S21, Phase 10 |
| V14 — Evaluation | Start with synthetic/public fixtures and explicitly selected examples from founder use. Keep private evaluation copies traceable. Founder review is the initial human-review route; particular private examples have not yet been selected. | S18, S27, S59, S76–S77 |
| V15 — Inference | Choose a stable initial provider/model configuration, enforce configurable spending caps, and report costs by feature. Provider/model names and actual monetary limits are not selected by accepting this approach. | S37, S47–S50, Phase 9 |
| V16 — Operations | Prefer a single-region deployment, same-site app/API, and managed data services where practical. Initial recovery objectives: at most 24 hours of data loss and four hours to restore, subject to a demonstrated drill. These are targets, not achieved guarantees. | S21, S60 |

An alpha admin is a role of an authenticated account, not a bypass of authentication. Sudo must
preserve who actually performed a mutation; it must not make an administrator's activity look like
an independent learner attempt. Broad product access does not require exposing passwords, session
tokens, or provider secrets.

## Follow-up decisions and deferred operating choices

- **Goal confidence:** use `ability - 2 * uncertainty_at(now) >= goal_threshold`. This is a
  conservative bound on the individual estimate, not a population percentile or a calibrated
  confidence percentage. Independent evidence must span time; calibrate that duration from data.
  Exact thresholds and freshness requirements remain measurement-dependent. Explicit learner
  closure never changes measured ability.
- **Web intake:** disable every URL-import and external web-access path in v0, including explicit
  learner links, retries, queued jobs, and the tutor's web tool. Preserve existing stored material.
- **Operating configuration:** propose paid-evaluation budgets, alpha caps, hosting/domain/mail,
  and provider choices later, once the product is built. Continue local implementation and offline
  testing now. Paid runs and deployment remain deferred. Private evaluation examples must be
  explicitly identified before use.

## Goal-model interpretation to resolve in the focused design

The current implementation checks ability >= 1.0 and uncertainty <= 0.5
(`app/services/lesson_plan.py::mastered_kc_ids`). These are documented provisional constants.
`GlickoEstimator.decay` increases uncertainty, capped at its configured maximum, while leaving
ability unchanged. FSRS independently schedules retention reviews.

Consequently, increasing uncertainty alone cannot guarantee that an arbitrarily high past ability
estimate will eventually fall below a threshold. The new design must consider evidence freshness
and retention as well as the conservative estimate. It must distinguish:

- the learner choosing to finish or archive a goal;
- current evidence meeting its target;
- a historical achievement established across independent checks over time;
- current evidence becoming stale and requiring reassessment.

Closing a goal does not fabricate assessment evidence. A historical achievement does not promise
permanent knowledge. Merely waiting above a threshold, without new independent demonstrations,
does not earn the sustained-evidence state. The final API names and transition rules belong in the
focused goal-policy implementation; the conservative estimate is now accepted.

## Delivery sequence

Each row is a workstream requiring focused, independently verifiable slices. No row is implemented
merely because the decision is accepted. Preserve existing learner work through migrations; do not
reset the current database to simplify ownership or evidence changes.

| Order | Workstream | Concrete completion evidence |
| --- | --- | --- |
| 1 | Identity, private ownership, and alpha administration | Invite-controlled access; authenticated admin role and audited sudo; private curriculum/item/content boundaries; reviewed publication; existing-data ownership migration; real mail seam and account recovery; cross-learner tests through the actual API. |
| 2 | Learning evidence and goal policy | Flashcard reveal/rating separated from ability; server-recorded evidence kind; reproducible event replay; rubric/item/prompt versions; goal status with temporal confidence and independent checks; graph integrity under concurrency; confirmed cross-subject links; adjustable detours and pause/resume. |
| 3 | Source and teaching quality | Explicit source scope and sources-only behavior across all generation paths; honest insufficient/conflicting-source responses; structure-preserving extraction; usable citations and historical references; safe retained URL intake if selected; versioned reindexing; duplicate-source recovery. |
| 4 | Durable learner work and controls | Exact note-edit preservation; preferences and inferred-profile controls; correctable memories and provenance; incremental refresh; archive/delete/forget/export flows; account-deletion recovery and erase-now; background cleanup that respects live references and suppression records. |
| 5 | Reliability and resource limits | Restart-safe checkpoints and migration lifecycle; real Redis delivery and worker-race tests; short resumable ingestion stages; strict resource/spend limits; cancellation and partial-call accounting; provider-error UX; prompt/model/version attribution; measured history/query budgets. |
| 6 | Evaluation and release gates | End-to-end browser journey; failure injection; migrations against existing data; deterministic CI and contract gates; expanded datasets, DSPy modules, and ablations; live calibration and human/model grading comparisons once the data and budget exist. Preserve the distinction between software correctness, model quality, and educational effectiveness. |
| 7 | Operated invited alpha | Configured production inference and email; deployment and secret delivery; alert polling/delivery/history; database and object-store backups; measured restore/rollback drills; sustained founder testing followed by observed and independent invited use. |

Full publication and concept transfer depend on the private-ownership boundary. Erasure and replay
depend on provenance and event semantics. Goal-policy changes depend on separating self-reports
from demonstrated ability. Live evaluation depends on suitable data and approved budgets, not just
the existence of report commands. Deployment depends on the actual operating configuration.

Some existing tracker sections contain an original gap followed by a later repair. Inspect the
current call path and the latest evidence before scheduling a repair a second time. Keep the
original numbered findings as historical evidence and append fresh implementation results.

## Verification and completion rules

- Use deterministic providers for ordinary tests. Paid inference is an explicit evaluation run.
- Exercise changed service/API call paths, including ownership, retries, concurrency, and existing
  data; testing a helper alone does not establish its callers use it.
- Run the relevant backend/frontend checks and API contract/migration gates when their boundaries
  change. Add real-browser and queue/process tests for behavior those layers alone can establish.
- Record what was tested, what the result proves, and what remains unmeasured. Do not promote
  “implemented” to “educationally validated” on the basis of passing software tests.
- Synthetic calibration cannot supply real learner thresholds or relevance judgments. Keep those
  evidence-dependent tasks open until suitable observations exist.

## Already deferred

All URL ingestion and external web access, DKT, institutional/LMS work, billing, younger learner tiers,
native mobile/desktop clients, and offline operation remain outside this v0 effort. Uploaded-file
adapters and current desktop-web features remain in scope.
