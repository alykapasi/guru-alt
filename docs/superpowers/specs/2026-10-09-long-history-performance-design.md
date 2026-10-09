# Measured long-history performance (S62, remainder)

**Status:** approved in conversation 2026-10-09; this document records it.
**Tracker:** S62. Workstream 5, piece 5 (after S47, S49, S37, S17; before S53).

## Problem

S62's first part bounded the transcript a conversation read returns
(`chat.list_messages`, `chat_transcript_page_size` 100 / max 500) and added query-count budgets
for analytics, notes, rollups and profile refresh (`tests/test_query_budgets.py`). Two things
remain from the tracker row: *measure* long-history latency and message-page growth, and
aggregate further only where the measurements justify it.

Nothing today measures time, nothing counts rows, and several paths a learner hits on every
visit or turn have no budget at all. Two of them return every row the learner owns:
`GET /conversations` (`chat.list_conversations`) and `GET /sources`.

## Decisions (from the design conversation)

1. **Budgets plus a report.** Deterministic statement/row budgets at two history sizes gate in
   `poe check`; a non-gating `poe perf-report` seeds a long history and reports latency.
2. **The report's learner is a 5× power user:** five times a year of daily use.
3. **Fix rule:** a path is fixed when its statements or rows grow with history, or when its
   server-side p95 at the 5× history exceeds **250 ms** (model time excluded). Everything else
   is reported and left alone.

## Measured paths

Each has one entry point shared by its budget test and the report.

| Path | Entry point |
| --- | --- |
| Turn, everything but the model | `chat.run_tutor_turn` on `FakeProvider` (window, `learner_context.gather`, grounding retrieval, spend admission, persistence) |
| Practice turn, everything but the model | `workflow.run_workflow_turn` start and resume on `FakeProvider` |
| Conversation list | `GET /conversations` |
| Transcript, first page and one "load earlier" | `GET /conversations/{id}/messages` |
| Source list | `GET /sources` |
| Due reviews | `GET /reviews/due` |
| Activity | `GET /activity` |
| Subject mastery | `GET /subjects/{id}/mastery` |
| Memory list | `GET /memory` |
| Lesson plan read | `GET /subjects/{id}/lesson-plan` |
| Plan revision after a graded answer | the revision the answer path triggers (`lesson_plan` revise) |
| Notes index | `GET /subjects/{id}/notes` |
| Profile refresh | the S43 refresh entry point |

**Growth** means statements or rows fetched rise when history grows. Rows are counted per
statement (`cursor.rowcount` after execute).

## Behaviour

### 1. History builder (`tests/history.py`)

`async seed_history(session, learner_id, *, scale: float) -> SeededHistory` bulk-inserts a
proportional history with set-based SQL (`INSERT … SELECT generate_series`), not through the
services, so 100k rows take seconds. At `scale=1`: 5 conversations of 40 messages (with turns),
1 subject of 20 components with 150 graded learning events and their component states, 30
memories, 4 sources of 10 chunks, 3 topic notes with revisions, 60 `llm_calls`. Embeddings are
deterministic fake vectors (`tests.embedding`), so no model is called. `SeededHistory` returns
the ids a path needs (a conversation, a subject, a topic).

The report's power user is the one-year shape × 5: ≈2,000 conversations, 100k messages, 75k
graded events, 15k memories, 750 sources with chunks, 15 subjects × ≈300 components, notes,
turns and `llm_calls`. `seed_history` takes per-table counts so the report can state that
shape exactly; `scale` multiplies the small one.

### 2. Budgets in `poe check` (`tests/test_history_budgets.py`)

- `tests/querycount.py` gains rows: `QueryCount` records `(statement, rowcount)` and prints both.
- Per path: seed two learners, `scale=1` and `scale=4`, in the test transaction; run the path
  for each; assert statements(4) ≤ statements(1) + 2 and rows(4) ≤ rows(1) + 2 (the slack
  absorbs a conditional branch, not growth). A bounded page asserts equal rows.
- Tests for paths known to grow are written first and seen failing: the conversation list and
  the source list. Any further path the report selects gets its failing test in part B.
- Target: under 10 s added to `poe check`.

### 3. `poe perf-report` (`tests/perf/report.py`)

- Database `guru_perf`, created and migrated through `tests.testdb --suffix _perf`, never the
  dev or test database.
- Seeds the power user and 20 ordinary learners at a tenth of that size, so tables and indexes
  look like a shared database. A `perf_seed` marker (seed version) lets a second run reuse
  the seed; `--reseed` rebuilds it.
- Per path: 3 warm-up runs, then 30 timed, in-process (no network). Reports p50, p95, max,
  statements, rows, and the three slowest statements (timed per statement with
  `before/after_cursor_execute`). Turns use `FakeProvider`.
- Output: a markdown table on stdout with paths over 250 ms p95 marked; `--json PATH` writes
  the same data for comparison. Exit code 0 always (it is a report, not a gate).
- No paid calls; no secrets read.

### 4. Known fixes

- **Conversation list** pages: `GET /conversations?limit&before` returns
  `ConversationPage {conversations, has_more}`, newest first, keyed on `(created_at, id)`;
  `chat_conversation_page_size` 50, `chat_conversation_page_max` 200 (an oversized `limit` is
  clamped; a stale or foreign cursor yields the first page). Same pattern as `list_messages`.
- **Source list** pages the same way: `SourcePage {sources, has_more}`,
  `sources_page_size` 100, `sources_page_max` 500, with `subject_id` and `archived` filters
  kept.
- Frontend: the conversation sidebar and the source picker become infinite queries
  (`useInfiniteQuery`, "load more" on scroll / a button), like `useMessages`. The API contract
  snapshot is regenerated.

### 5. Sequencing

- **Part A** (the plan): history builder, row-counting counter, all budget tests, the two
  paging fixes, `poe perf-report`, then the first report run against `guru_perf`. Its table is
  added to this spec under "Results".
- **Part B** (a plan addendum, reviewed before it is built): for each further path the rule
  selects, its measured cause (missing index, per-item query, full scan, unbounded rows) and a
  test-first fix. If the rule selects nothing, part B records "none".
- S62 is done when a rerun of the report selects nothing.

## Out of scope

- Timed gates in CI (decision 1).
- Model latency, network and frontend render time.
- Paths not listed above (admin, publication review, ingestion jobs).

## Results

Filled in by part A's first report run.
