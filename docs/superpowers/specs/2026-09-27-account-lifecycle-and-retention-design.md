# Account lifecycle and retention (S61; V12)

**Status:** approved in conversation 2026-09-27; this document records it.
**Tracker:** S61 (the V12 half). Workstream 4, slice B. Builds on slice A
(`docs/superpowers/specs/2026-09-26-archive-delete-forget-design.md`).

## Problem

- `DELETE /me` erases everything at once. V12 asks for immediate loss of access, a seven-day
  recovery window, and an explicit erase-now.
- Object-store deletions that fail are reported and then lost: the rows naming the keys are gone,
  so nothing can find them again. The same will be true of the identity provider's copy of the
  learner (Clerk keeps the email address), which nothing deletes today.
- Diagnostic data has no expiry. V12: "Initial configurable diagnostic/backup retention is 30
  days. Apply expiry selectively: preserve durable learning history, learner edits, and required
  deletion-suppression records."
- Export lists uploads as metadata only; a learner cannot take their files with them.
- Slice A leftover: `DELETE …?forget=true` reports forgotten items under `kept`.

## Decisions (from the design conversation)

1. **Pending deletion with self-service recovery** (choice A): deleting signs every session out
   and hides everything; signing in again within seven days shows a recovery screen with Restore,
   Download your data and Erase now; after seven days a worker erases.
2. **Diagnostics: anonymise accounting, delete the rest** (choice A): after 30 days, model-call
   and decision-call rows lose their learner and conversation ids; finished turns and old alert
   transitions are deleted.
3. **A state on the learner row, enforced by the authenticated-learner dependency** (approach 1),
   the same pattern as suspension.

## Design

### Data — migration 0067

- `learners.deletion_requested_at timestamptz NULL`, `learners.deletion_due_at timestamptz NULL`.
- `pending_erasures`: `id uuid pk`, `kind text` (`'blob' | 'identity'`), `target text` (a blob
  key or provider subject), `attempts int default 0`, `last_error text NULL`,
  `next_attempt_at timestamptz`, `created_at timestamptz default now()`; unique `(kind, target)`.
  It names no learner, so it survives the erase it belongs to; a row is deleted once its erasure
  succeeds.
- Settings: `account_recovery_days = 7`, `diagnostic_retention_days = 30`.

### Lifecycle

States: **active** (both columns NULL) → **pending** (`deletion_due_at` set) → **erased** (row
gone). Restore returns pending → active.

- `DELETE /me` → **request deletion**: sets `deletion_requested_at = now`,
  `deletion_due_at = now + account_recovery_days`, revokes every session of the learner, returns
  202 `{"due_at": …}`. Idempotent: repeating it on a pending account returns the same due date.
- **Gate:** `get_current_learner` refuses a pending account with 403
  `{"code": "deletion_pending", "due_at": …}` everywhere except the allow-list:
  `GET /me/deletion`, `POST /me/deletion/restore`, `POST /me/deletion/erase`, `GET /me/export`,
  `GET /me/export/sources/{id}/file`. These use a separate dependency that admits a pending
  account. An administrator's visit to a pending account is gated the same way; admin routes are
  unaffected.
- **Sign-in:** `identity.enroll` does not refuse a pending account; its new session is simply
  gated. A suspended account is still refused at sign-in.
- `GET /me/deletion` → `{"pending": bool, "requested_at", "due_at"}`.
- `POST /me/deletion/restore` → clears both columns; 409 `not_pending` if not pending.
- `POST /me/deletion/erase` → erases now and returns the deletion report; 409 `not_pending` if
  not pending (erasing an active account always goes through the request first; the settings
  dialog does both in sequence for "Erase now instead").
- **Erase** (`retention.erase_learner`): the existing `delete_learner` walk, then
  `IdentityProvider.delete_user(auth_subject)` when the learner had a provider subject. A refused
  provider delete or object-store delete is written to `pending_erasures`; the report still says
  what happened. Never blocks the database erase.
- **Worker:** `_erase_due_once` erases every learner whose `deletion_due_at <= now`, one
  transaction each; a learner already gone is skipped.

`IdentityProvider` gains `async def delete_user(self, subject: str) -> None` (Clerk: delete the
user; "not found" is success). The fake provider in tests records calls and can be told to fail.

### Diagnostic expiry

`_expire_diagnostics_once`, hourly, for rows older than `diagnostic_retention_days`:

- `llm_calls`, `decision_calls`: `learner_id` and `conversation_id` set to NULL (totals and Jev's
  evidence survive, tied to nobody).
- `turns` with a finished status (completed, failed, cancelled): deleted. Pending turns are never
  touched.
- `alert_transitions`: deleted, except the newest row per alert name (the current state).

Learning history, notes, memories, sources, content and audit records (admin actions, account
actions, impersonations, publications) are never expired. `RETENTION` entries for the expiring
stores gain a stated window, so `GET /me/retention` shows the 30 days. RUNBOOK documents the
setting and that it is the target window for backups once workstream 7 creates them.

### Retryable erasure

`_retry_erasures_once` takes rows with `next_attempt_at <= now`:

- `blob`: delete only if no source references the key again (a re-upload may have claimed it);
  then delete the row either way (a re-referenced key is no longer this erasure's business).
- `identity`: `delete_user`; not-found counts as success.
- Failure: `attempts += 1`, `last_error` set, `next_attempt_at = now + min(2^attempts minutes,
  1 day)`. Rows keep retrying at the cap. An alert condition fires while any row has
  `attempts >= 10`, so nothing is abandoned silently.

Writers: `delete_learner` (keys the store refused), `removal.delete_source` (a failed file
delete), and `erase_learner` (a refused provider delete).

### Export of uploads

- `GET /me/export/sources/{source_id}/file` streams the stored bytes with the source's content
  type and `Content-Disposition: attachment; filename="<origin>"`. 404 for another learner's
  source, a source without a file, or bytes missing from the store. Archived sources included.
- `GET /me/export`'s source entries gain `file_path` (the route above) when the source has a
  file.

### Slice A leftover

`removal.delete_source` / `delete_conversation` with `forget=True` report the forgotten keys as 0
under `kept`.

### Frontend

- Any 403 `deletion_pending` routes the app to a **Recovery** page: "Your account is scheduled
  for deletion on *date*", Restore, Download your data (JSON and per-file links), Erase now
  (confirmed).
- The settings page's delete action explains the seven-day window and offers "Erase now
  instead".

## Errors

Repeat request → same due date. Restore / erase-now on a non-pending account → 409
`not_pending`. Suspended + pending → sign-in refused; the worker still erases at the due time.
Provider or store refusal → pending erasure, never a failed erase. File export → 404 as above.

## Testing

- Migration: existing learners arrive active; `pending_erasures` exists.
- Lifecycle: request revokes every session and sets `due = now + 7d`; ordinary routes → 403
  `deletion_pending`, allow-listed ones work; re-sign-in through the fake provider is gated;
  restore reopens everything; erase-now removes the learner and calls `delete_user`; the worker
  erases only past-due accounts; a refused provider delete becomes a pending erasure; repeat
  request keeps the due date; restore/erase on an active account → 409.
- Expiry: old accounting rows anonymised, newer ones intact; old finished turns deleted, pending
  kept; newest alert row per alert kept; learning history, notes and audit rows untouched.
- Retry: a refused blob is retried and deleted; a re-referenced key is left and the row removed;
  backoff grows; success removes the row; the alert condition fires at the threshold.
- Export: own file downloads with content type and filename; another learner's → 404.
- Slice A: `forget=True` reports zero under `kept`.
- Frontend (vitest): Recovery page states, restore, erase confirmation; "Erase now instead".

## Out of scope

Creating backups (workstream 7); expiring learner-owned data (V12 keeps it until deleted); an
admin UI for pending erasures (alert and logs cover it); re-authentication before restore
(choice C, declined).

## Tracker updates on completion

- S61 → **Done** for software (V11 and V12 both delivered); remaining operating items (backup
  creation and its 30-day window) move to workstream 7.
