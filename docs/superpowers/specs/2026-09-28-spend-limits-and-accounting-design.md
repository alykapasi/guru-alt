# Spend limits and complete call accounting (S47, S48)

**Status:** approved in conversation 2026-09-28; this document records it.
**Tracker:** S47 (caps across every paid operation), S48 (complete accounting). Workstream 5,
slice A. Whole-request deadlines and cancellation (the rest of S47) are the next slice.

## Problem

- **Accounting is remembered at 29 call sites.** Each service calls `log_llm_call` by hand
  after a successful call. A row has learner, conversation, role, model, tokens, cost and
  timing, but not which feature or request paid for it.
- **Failed and interrupted calls leave no row.** A timeout or provider error after tokens were
  spent is invisible; a stream is recorded only from its final chunk, so a stream the learner
  closed early is unrecorded.
- **One budget check exists**, in `POST /chat` before a turn. Lesson generation, grading,
  placement, onboarding, notes, ingestion and the background write-back and profile refresh
  (S43) are unchecked. Concurrent requests all pass the check before any records its spend.
- **The deployment budget only alerts.** `spend_budget_usd` raises `spend_over_budget` and
  stops nothing.

## Decisions (from the design conversation)

1. **Scope (A):** this slice is complete accounting plus caps across every paid call.
   Deadlines and cancellation are the next slice.
2. **Deployment ceiling (A):** a hard ceiling. Background work pauses at 90% of it; every paid
   call is refused at 100%.
3. **Attribution (approach 1):** a `contextvars` attribution context read by `LLMClient`; the
   client records and checks every call itself.

## Design

### Attribution — `app/llm/attribution.py`

- `Attribution(learner_id, conversation_id, feature, request_id, background: bool)`, held in
  one `ContextVar`. `attributed(**fields)` is a context manager that overrides only the fields
  it names and restores the previous value on exit. `current() -> Attribution`.
- **Request and learner:** the request-id middleware sets `request_id`; `get_current_learner`
  sets `learner_id`. Worker tasks set `learner_id` (and `conversation_id` where they have one)
  at their start.
- **Feature:** set by the service that does the work, at its entry function, so the name says
  what was paid for: `chat_turn`, `practice_grading`, `lesson_generation`, `note_distillation`,
  `placement`, `onboarding`, `curriculum`, `ingestion`, `reindex`, `memory_write_back`,
  `profile_refresh`, `concept_links`. The plan enumerates every current caller and assigns one;
  a nested `attributed(feature=…)` wins (grading inside a chat turn is `practice_grading`).
- **Background:** `background=True` for the memory write-back, profile refresh, concept-link
  and reindex tasks. Ingestion stays foreground: the learner is waiting on their upload.

### Recording — inside `LLMClient`

`complete`, `stream` and `embed` go through one recorder; `log_llm_call` and its call sites
are deleted.

- **Before the call** (after the guard below): a `pending` row on the accounting session,
  committed, carrying the attribution, role, provider, model, `prompt_hash` and `app_version`,
  with a reserved estimate in `input_tokens`/`output_tokens`/`cost_usd` and `estimated=true`.
- **After:**
  - success → `ok`, real tokens, cost and timing, `estimated=false`;
  - an exception → `failed`, `error_kind` = the exception's class name (never its message, which
    may contain learner text), the input estimate kept (the provider may have charged) and
    output 0, `estimated=true`; the exception propagates unchanged;
  - a stream closed before its final chunk (consumer stopped, disconnect, cancellation) →
    `partial`, input estimate plus output tokens estimated from the text delivered,
    `estimated=true`.
- **Estimate:** input tokens ≈ characters of system prompt + messages ÷ 4; output = `max_tokens`
  (embeddings: input only). Cost via `price_usd`; an unpriced model reserves `NULL` cost but
  still reserves tokens.
- `prompt_hash`: first 16 hex characters of SHA-256 of the system prompt (NULL when none).
  `app_version`: new setting `app_version` (`GURU_APP_VERSION`, default `dev`).
- **Accounting never fails the work.** A failed write is logged `llm.call_not_recorded` and the
  call proceeds (before) or returns normally (after), as today. The structured `llm.call` log
  line is kept, now with feature and status.
- Jev's `decision_calls` stays its own ledger.

### Migration 0071

`llm_calls` gains `feature text NOT NULL DEFAULT 'legacy'`, `request_id text NULL`,
`status text NOT NULL DEFAULT 'ok'`, `error_kind text NULL`, `estimated boolean NOT NULL DEFAULT
false`, `prompt_hash text NULL`, `app_version text NULL`, and an index on
`(learner_id, created_at)` if one does not already exist. New rows set `feature` explicitly
(`unattributed` when no context gave one).

### The guard — `app/services/spend_guard.py`

Called by the recorder immediately before the `pending` row is written; raises
`BudgetExceeded(scope: "learner" | "deployment", message)` and no provider call is made.
`app/services/budget.py` folds into it.

- **Learner (strict).** In one accounting transaction holding
  `pg_advisory_xact_lock` on the learner: sum the trailing 24 hours of the learner's rows
  (settled at real cost/tokens, `pending` at their estimate — including a pending row a crashed
  process left, which keeps counting until it leaves the window), compare with
  `learner_daily_cost_usd_limit` and `learner_daily_token_limit`, and insert the `pending` row
  in the same transaction. Concurrent calls for one learner therefore see each other's
  reservations. A `background` call is refused at 90% of either limit.
- **Deployment.** When `spend_budget_usd` is set: the trailing `spend_window_hours` total, read
  at most every `spend_guard_cache_seconds` (default 30) per process. A `background` call is
  refused at 90%, every call at 100%. The cache can lag by up to its age — a documented soft
  edge on a hard ceiling; the learner caps stay exact.
- **No learner in context** (system work): only the deployment check applies.
- **Admin visits:** calls during a visit are the learner's and count against their cap.

### Surfaces

- **API:** one exception handler maps `BudgetExceeded` to 429
  `{"code": "budget_exceeded", "scope": …, "message": …}`. Learner scope: "You've reached
  today's usage limit. It resets over the next 24 hours." Deployment scope: "Guru has reached
  its usage limit for now — try again later."
- **Streaming turns:** a refusal mid-turn ends the turn with the existing error event carrying
  that message; the turn lifecycle marks it failed, so it can be retried. The chat preflight
  stays as an early refusal and also checks the deployment ceiling.
- **Workers:** a background task refused logs `budget.deferred` at info and returns; its claim
  stays, so it is retried later. The refresh sweep claims nothing while the deployment is past
  90%. Ingestion refused marks the source failed with a budget reason the learner can see and
  retry.
- **Frontend:** the composer and the generic API error display show the message; no new screen.

## Errors

- Accounting unreadable or unwritable before a call: the call proceeds and
  `llm.call_not_recorded` is logged (bookkeeping must not become an outage). The learner cap
  then undercounts by what went unrecorded.
- Deployment total unreadable: use the last cached value; with none, allow and log a warning.

## Observability

- `/ops/spend` and the admin dashboard: cost by feature (with the unpriced-floor rule per
  feature), failed and partial counts, and the share of estimated rows.
- Alerts: `spend_near_budget` (warning) at 90% — background work paused; `spend_over_budget`
  at 100% with its action updated to say every paid call is refused; `calls_pending_stale`
  (warning) when a `pending` row is older than 15 minutes — a process died mid-call.

## Docs

RUNBOOK §18 "Spend limits": the caps, the ceiling and the 90% rule, reading cost by feature,
stale pending rows. OPERATIONS: settings and alerts. CLAUDE.md: a bullet. Tracker: S48 →
Implemented; S47 keeps deadlines and cancellation as its remainder.

## Testing

All with the fake LLM.

- Recording: completion, stream and embedding each write one `ok` row with feature, request id,
  prompt hash and app version; a provider exception writes `failed` with the class name and no
  message; a stream closed after two chunks writes `partial` with estimated output; an
  accounting failure does not fail the call; nested `attributed` overrides only what it names.
- Guard: over either learner cap → refused before any provider call; two concurrent calls near
  the cap → the second is refused; background refused at 90%, live not; deployment ceiling
  refuses background at 90% and all at 100% (including through the cache); a pending row counts
  at its estimate.
- Surfaces: 429 `budget_exceeded` with scope; a streaming turn refused mid-way ends with the
  error event and a failed turn; a background task deferred with its claim kept; ingestion marks
  the source failed with the budget reason.
- Attribution guard: API-client and worker tests fail on a paid call recorded as
  `unattributed`.
- Reporting: cost by feature with the unpriced floor; the two new alerts.
- Migration 0071: defaults on existing rows.

## Out of scope

Deadlines and cancellation (next slice); the Jev ledger; budget amounts (an operating choice);
per-feature caps.

## Tracker updates on completion

- S48 → **Implemented**: every call is recorded by the client, including failed and partial
  ones, with feature, request, prompt hash and version; cost is reported by feature.
- S47 → still **Partial**: caps are enforced on every paid call (learner exact under
  concurrency, deployment ceiling with background paused at 90%); remaining: whole-request
  deadlines and cancellation.
