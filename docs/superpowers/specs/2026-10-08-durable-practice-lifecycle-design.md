# Durable practice lifecycle and restart verification (S17, remainder)

**Status:** approved in conversation 2026-10-08; this document records it.
**Tracker:** S17. Workstream 5, piece 4 (after S47, S49, S37; before S62, S53).

## Problem

Paused practice questions and goal negotiations live in LangGraph's `AsyncPostgresSaver`
(`app/agent/checkpointing.py`). Four gaps remain:

1. **Schema setup at runtime.** Every process calls `saver.setup()` at start. That is DDL from
   the app's role, and two processes starting together race on `checkpoint_migrations`; the
   loser logs `checkpointer.durable_start_failed` and runs volatile.
2. **Checkpoints outlive their owners (V12).** `removal.delete_conversation` and
   `retention.delete_learner` leave the thread behind, and it holds learner text. The worker's
   prune (`checkpoints.prune`) only reaches threads whose conversation still exists, so orphans
   are never removed. Onboarding threads (`{learner_id}:{session_id}`) and `onboarding_sessions`
   rows are never expired. Retention has no entry for checkpoints.
3. **Restart is only simulated.** `tests/test_durable_state.py` rebuilds the saver inside one
   process. Nothing shows a separate process resuming, and there are no race tests (resume vs
   prune, two resumes, delete vs resume).
4. **No graph-shape compatibility.** A deploy that renames a node or state key leaves
   checkpoints that resume into the wrong node or raise mid-turn.

## Decisions (from the design conversation)

1. **The deploy step owns the checkpoint schema.** `poe db-upgrade` runs it; processes only
   check it.
2. **Version stamp, drop on mismatch.** Each graph has a hand-bumped version stamped on its
   checkpoints; a mismatched, unstamped or unreadable checkpoint is discarded and the learner
   falls back cleanly. A test fails when a graph's shape changes without a bump.

## Success criteria

- No checkpoint outlives its conversation, onboarding session or learner.
- A deploy never breaks a paused learner: their state resumes or is cleanly dropped.
- Schema setup cannot race, and no process runs DDL at start.

## Behaviour

### 1. Schema setup (deploy step)

- **`checkpointing.migrate(settings)`** opens one psycopg connection (autocommit), takes a
  transaction-independent advisory lock (`pg_advisory_lock`, a fixed key in a new namespace),
  runs `AsyncPostgresSaver(conn).setup()`, and unlocks. Idempotent.
- **`poe db-upgrade`** runs Alembic, then `python -m app.agent.checkpointing migrate`.
  `tests.testdb` does the same for the test database. CI's `alembic check` is unchanged: the
  four LangGraph tables stay excluded in `db/migrations/env.py` (its comment is updated).
- **`checkpointing.start(settings)`** no longer calls `setup()`. It opens the pool and reads
  `SELECT max(v) FROM checkpoint_migrations`:
  - equal to `len(AsyncPostgresSaver.MIGRATIONS) - 1` → durable;
  - table missing, empty, or behind → volatile fallback (as today when the database is
    unreachable), logged at error level as `checkpointer.schema_behind` with `expected` and
    `found` (`None` when missing). `/ready` reports not-durable as it already does.
  - ahead (a newer library wrote it) → durable, logged at warning as `checkpointer.schema_ahead`.
- RUNBOOK: "a LangGraph upgrade may need `poe db-upgrade` before deploy."

### 2. Erasure and expiry (V12)

- **`checkpoints.erase_threads(session, thread_ids, reason)`** deletes each thread through the
  saver (`adelete_thread`). A thread whose delete raises, or every thread when the saver is
  volatile, is queued as a `pending_erasures` row of a new **`ErasureKind.CHECKPOINT`**
  (target = thread id). It never raises to its caller.
  `checkpointing.delete_thread(thread_id)` is the raising primitive it uses; the best-effort
  `discard_thread` stays for the resume paths.
- **Conversation delete.** `removal.delete_conversation` erases its thread
  (`str(conversation_id)`) after the commit.
- **Account erase.** `retention.delete_learner` collects the learner's conversation ids and
  onboarding thread keys (`onboarding_sessions.thread_key`) before the rows cascade, and erases
  them in the same pass that deletes blobs.
- **Retry.** `retention._retry_one` handles `ErasureKind.CHECKPOINT` by calling
  `checkpointing.delete_thread`; a volatile saver is a refusal (raise), so it backs off like a
  refused blob.
- **Orphan sweep.** The worker's purge loop also runs `checkpoints.prune_orphans(session)`:
  read `SELECT DISTINCT thread_id FROM checkpoints` with plain read-only SQL; keep a thread
  whose id is an existing conversation id or an existing onboarding session's thread key;
  delete every other one through the saver. Writes always go through the library.
- **Onboarding expiry.** `checkpoints.prune` also deletes onboarding sessions whose
  `updated_at` is older than `older_than` (the same window as conversations), erasing their
  threads. Nothing bumps `updated_at` today, so `onboarding_sessions.require` sets it to
  `now()` on each round; idleness is measured from the learner's last round.
- **Retention catalogue.** A `checkpoints` entry: "deleted with its conversation, onboarding
  session or account; idle threads pruned; orphans swept". The `onboarding_sessions` note is
  corrected to say the session and its checkpoint expire after the idle window.

### 3. Restart and race verification

- **Real second process.** A test pauses a practice question with committed rows (no outer
  test transaction; rows cleaned up explicitly), then runs
  `python -m tests.restart_probe <conversation_id>` as a subprocess against the test database.
  The probe starts the checkpointer fresh and prints JSON with the `paused_item_id` it sees and
  the outcome of resuming with an answer on `FakeProvider`. The test asserts the second process
  resumes exactly the paused item. It runs in `poe check` (no paid model).
- **Race tests** (real pool, committed rows):
  1. **Resume vs prune.** A turn holds `turn_lock` for a conversation that prune selects as
     stale; prune skips that thread. Prune takes the same lock with try-acquire and skips when
     it is busy.
  2. **Two resumes.** Two concurrent answers to one paused question: one grades, the other gets
     the existing turn-in-progress response, and one graded event is recorded.
  3. **Delete vs resume.** Deleting a conversation while a resume is in flight ends the resume
     cleanly (404 or a dropped turn, no crash) and leaves no thread behind.
- No paid calls; `GURU_JEV_SMOKE` is never set.

### 4. Graph-version compatibility

- **Version constants**, beside each compile: `WORKFLOW_GRAPH_VERSION` (`app/agent/workflow.py`)
  and `REFINEMENT_GRAPH_VERSION` (`app/agent/refinement.py`; onboarding uses this graph).
- **Stamp.** The run config carries `metadata={"graph_version": N}`; LangGraph copies run
  metadata onto each checkpoint and `aget_state` returns it as `snapshot.metadata`. The first
  implementation task verifies this against the installed LangGraph; if it does not hold, the
  stamp moves to a `graph_version` state key, recorded as a ruling.
- **Check.** `checkpoints.compatible(snapshot, version) -> bool`: false for a mismatched or
  missing stamp. Each resume path reads state through one helper that also treats an
  `aget_state` that raises (unreadable blob) as incompatible:
  - practice: `workflow.paused_item_id` (and through it `is_awaiting_reply`);
  - refinement: `app/services/refinement.py` resume;
  - onboarding: `app/services/onboarding.py` resume.
- **Fallback.** An incompatible checkpoint is discarded and logged as
  `checkpointer.incompatible_dropped` with `graph`, `expected`, `found` and `thread_id` — never
  learner text. Practice: the turn proceeds as ordinary chat, as when nothing is paused; the next
  practice request starts a fresh question. Refinement: as when no negotiation is open.
  Onboarding: the existing expired/start-again response, and the session row is deleted.
- **Existing checkpoints** have no stamp and are dropped once, on first resume. Intended.
- **Bump guard.** A test computes each compiled graph's fingerprint (sha256 of sorted node
  names, sorted edges and sorted state channel keys) and compares it with a pinned
  `(version, fingerprint)` pair; a mismatch fails with "bump `X_GRAPH_VERSION` and update the
  pin".

## Out of scope

- Migrating old checkpoints forward (decision 2 drops them).
- Changing the idle window or adding a setting for it.
- Moving LangGraph's tables under Alembic revisions.

## Testing summary

- Unit/integration: `migrate` idempotent under two concurrent calls; `start` volatile when the
  schema is missing or behind, durable when current; `erase_threads` deletes and queues on
  refusal; retry clears a `CHECKPOINT` row; conversation delete and account erase leave no
  thread; orphan sweep removes only orphans; onboarding expiry removes row and thread;
  compatibility drop for mismatched, unstamped and unreadable checkpoints on each resume path;
  fingerprint pin.
- Process/race: restart probe subprocess; resume vs prune; two resumes; delete vs resume.
