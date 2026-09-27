# Refresh scheduling and incremental profile (S43)

**Status:** approved in conversation 2026-09-27; this document records it.
**Tracker:** S43. Workstream 4, slice E.

## Problem

- Memory write-back runs only when something calls
  `POST /conversations/{id}/memory/write-back`, and nothing in the frontend does. A learner
  who only chats never has memories written.
- The profile refreshes only when the learner presses Refresh on the Dashboard. Lesson-plan
  hints and pacing are revised only after that.
- A refresh re-reads the learner's whole history every time. The three model-backed estimators
  already sample recent input, so their cost is bounded, but they run again whenever *any*
  evidence moved, even when their own sample did not.
- `latest_evidence_at` counts admin-visit chat messages. An admin visit makes the profile look
  stale and triggers a paid recompute that cannot change anything (the estimators already
  exclude those messages).
- Nothing catches up a backlog: evidence that arrives while nobody asks is never processed.

## Decisions (from the design conversation)

1. **Trigger (A):** work runs when a conversation or learner has gone quiet, found by a
   state-driven sweep; the manual paths stay.
2. **Incremental (A):** refresh reads a recency window, and a model-backed estimator runs only
   when the sample it would send changed.
3. **Memory consent (A):** a global "Remember things from my conversations" setting, on by
   default.

## Design

### Scheduling — one worker loop

`refresh_due` runs every `refresh_poll_interval_seconds` (default 300; `0` disables it, like
the other loops in `app/workers/tasks.py`). Each pass:

1. **Conversations due for write-back.** A conversation is due when:
   - it has a message with `role = 'user'`, no `admin_actor_id` and no `admin_action_id`,
     newer than `memory_watermark` (or any such message when the watermark is NULL);
   - its newest message of any kind is at least `memory_quiet_minutes` (default 20) old;
   - it is not archived (`archived_at IS NULL`);
   - its learner has `remember_conversations = true`, no `deletion_requested_at` and no
     `suspended_at`;
   - `memory_attempted_at` is NULL or older than `refresh_retry_minutes` (default 60).
2. **Learners due for a profile refresh.** A learner is due when:
   - their newest learner evidence — a graded `observation` event or a message they wrote
     (admin-visit messages excluded) — is newer than `learner_profiles.evidence_watermark`
     (or there is no profile/watermark yet and there is evidence);
   - that newest evidence is at least `memory_quiet_minutes` old;
   - no `deletion_requested_at`, no `suspended_at`;
   - `refresh_attempted_at` is NULL or older than `refresh_retry_minutes`.

Each pass claims at most `refresh_batch_size` (default 50) of each, oldest due first. A claim
is a conditional `UPDATE … SET memory_attempted_at = now() WHERE id = … AND (memory_attempted_at
IS NULL OR memory_attempted_at < cutoff) RETURNING id` (and the same for the profile's
`refresh_attempted_at`, creating the `learner_profiles` row if missing), so two workers never
claim the same item. Each claimed item is queued: conversations through the existing
`memory_write_back_task`, learners through a new `profile_refresh_task`. A learner whose
conversation and profile are both due in one pass gets both; order between them is not
guaranteed and does not matter (write-back does not produce profile evidence).

"Due" is derived from stored data on every pass, so a worker restart or a learner returning
after weeks is caught up by the next passes; there is no separate catch-up job.

The manual paths stay: `POST /profile/refresh` (with `force`) and the write-back endpoint.
Nothing runs inside a turn.

### Admin attribution

- `latest_evidence_at` excludes messages with `admin_actor_id` or `admin_action_id`, matching
  what the estimators read. Graded answers during a visit are already `admin_observation`
  events and are already excluded.
- Scheduled jobs log their model calls against the learner with no admin actor, as extraction
  already does.

### Incremental profile

- **Window.** `refresh_profile` loads the learner's most recent `profile_event_window` events
  (default 2000) and most recent `profile_message_window` own messages (default 500), newest
  first in the query, handed to estimators oldest first as today.
- **Fingerprints.** `DimensionSpec` gains an optional
  `fingerprint: Callable[[EstimatorContext], str | None] | None`. The three model-backed
  estimators implement it as a SHA-256 of exactly what they would send:
  - `error_type`: the ids of the sampled incorrect events;
  - `goal_orientation`: the last five goals' text;
  - `interests`: the ids of the sampled messages.
  `profile_dimensions` gains `input_fingerprint text NULL`. When a spec has a fingerprint, the
  dimension's row exists, and the stored fingerprint equals the new one, the estimator is not
  called and the stored value stands. A computed result stores its fingerprint. `force=true`
  ignores fingerprints; a dimension reset deletes the row, so it recomputes.
- Free (statistical) estimators always recompute from the window.
- The Dashboard's profile section gets one caption: "Read from your most recent answers and
  messages." No per-dimension label claims all-time history, so none changes.
- An automatic refresh revises plans exactly as a manual one does (`_revise_lesson_plans`).
  Pinned settings (S02) still override the inferred values where instructions are assembled;
  nothing new is needed for that.

### The memory setting

- `learners.remember_conversations boolean NOT NULL DEFAULT true` (migration 0070, with the
  two claim columns and `input_fingerprint`). Exported with the account.
- `GET /me/memory-setting` → `{remember: bool}`; `PUT /me/memory-setting {remember: bool}` →
  the same. `CurrentLearner` routes, so a pending-deletion account is refused. During an admin
  visit a change is audited like any sudo write.
- Off means: the scheduler skips the learner; `POST /conversations/{id}/memory/write-back`
  answers 409 `memory_paused`; `write_back` itself returns `[]` without a model call if the
  learner is paused by the time the job runs. Memories already stored stay, are still
  retrieved by the tutor, and can be corrected or forgotten — pausing stops learning, it does
  not hide what is known. Forget everything clears them.
- Frontend: a switch in the Account page's Preferences section; the Memory page shows "Memory
  is paused — Guru isn't learning anything new from your conversations" with a link to it.

## Errors

- A failed job leaves its watermark unmoved; the claim stamp keeps the next attempt at least
  `refresh_retry_minutes` away. Profile failures keep recording `last_error`; a failed
  write-back logs `memory.write_back_failed` with the conversation id and no content.
- The loop catches and logs its own errors per pass; one bad item never stops a pass.
- Jobs re-check at run time: a learner now pending deletion or suspended, or paused (write-back
  only), gets no work and no model call.
- **Alert** `refresh_stuck` (warning): any conversation or learner that has been due — quiet,
  with unprocessed evidence — for longer than `refresh_stuck_hours` (default 6). Action: check
  the worker is running and the loop enabled, then read `learner_profiles.last_error` and the
  `memory.write_back_failed` logs.

## Docs

OPERATIONS.md: the new loop in the worker sweeps, the new alert, and the admin-visit table's
stale "Read-only" row corrected to audited sudo. RUNBOOK: a short section on how refresh is
scheduled, how to force one, and how to pause it (`refresh_poll_interval_seconds=0`).

## Testing

All with the fake LLM.

- Due-ness: quiet conversation due, one with a message 5 minutes ago not; admin-visit messages
  make nothing due; paused, pending deletion, suspended and archived skipped; a recent attempt
  blocks a re-claim until the retry window passes; the batch cap, oldest first; a second claim
  of the same item in the same instant claims nothing.
- A pass end to end: a quiet conversation gets its memories and the learner's profile
  refreshes; a second pass does nothing.
- Admin attribution: an admin-visit message does not move `latest_evidence_at`.
- Incremental: only the newest N events/messages are read; a second refresh after only a new
  correct answer makes no model call; a changed sample calls again; reset and `force`
  recompute; a query-count budget on refresh.
- Memory setting: default on; paused blocks the scheduler, the endpoint (409) and `write_back`;
  existing memories still retrieved while paused; exported; a change during an admin visit is
  audited.
- Migration 0070: the columns and defaults on existing rows.
- Alert: fires past the threshold, silent below it.
- Frontend (vitest): the Account switch; the Memory page's paused notice.

## Out of scope

Calibrating the quiet period, windows and batch size (testing phase); true running aggregates;
a monetary spend ceiling (S47); growth measurement (S62).

## Tracker updates on completion

- S43 → **Implemented**: write-back and profile refresh run when a conversation or learner goes
  quiet, from a state-driven sweep that also catches up backlogs; the profile reads a recency
  window and model-backed estimators run only when their sample changed; admin-visit messages
  no longer count as evidence; learners can pause memory.
