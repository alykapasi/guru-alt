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

Recorded 2026-10-09 with `uv run poe perf-report --reseed` (seed version 2), 30 timed runs per
path after 3 warm-ups, on an arm64 laptop with PostgreSQL 17.10 in Docker. `guru_perf` held the
power user and 20 ordinary learners: 300k messages, 225k learning events, and two items per
component. Seeding took about 17 minutes; later runs reuse the seed.

This replaces a first run (seed version 1). The final review found that run's `practice` row
measured an error exit: a transfer step had no item in a new setting. The seed now gives every
component an unanswered item in the next setting.

| path | p50 ms | p95 ms | max ms | statements | rows | budget |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| turn | 290.5 | 298.9 | 307.9 | 18 | 157 | over 250 |
| practice | 2187.5 | 2288.4 | 2305.0 | 62 | 32499 | over 250 |
| conversation_list | 2.7 | 2.8 | 3.0 | 2 | 51 | ok |
| transcript | 2.6 | 2.9 | 3.1 | 2 | 117 | ok |
| source_list | 2.3 | 3.1 | 3.4 | 1 | 101 | ok |
| reviews_due | 8452.8 | 9801.9 | 10108.2 | 4399 | 36157 | over 250 |
| activity | 47.8 | 136.4 | 142.0 | 1 | 18545 | ok |
| subject_mastery | 98.6 | 119.3 | 184.4 | 5 | 1533 | ok |
| memory_list | 13.4 | 14.4 | 14.7 | 1 | 50 | ok |
| lesson_plan | 1.5 | 1.7 | 1.8 | 1 | 1 | ok |
| plan_revision | 1854.9 | 1945.1 | 1958.0 | 17 | 32388 | over 250 |
| notes_index | 41.2 | 46.5 | 47.2 | 9 | 92 | ok |
| profile_refresh | 1935.4 | 2034.5 | 2055.2 | 37 | 36924 | over 250 |

Slowest statements:

- **reviews_due:** 4,399 statements, doing work for each of ~1,500 due components. The slowest
  two are the transfer-evidence query (`anon_1.kc_id, anon_1.setting, min(CASE …)`, ~500 ms
  each). Next is the per-component evidence count (`count(DISTINCT CASE …)`, ~210 ms).
- **practice:** answering grades the response and revises the plan. That runs the same
  transfer-evidence query twice (~500 ms each) and the evidence count (~200 ms).
- **plan_revision:** the same two queries (~550 ms each) and the evidence count (~220 ms).
- **profile_refresh:** the same queries (~510 ms each, ~200 ms). It also reads every learning
  event and message in its recency window.
- **turn:** the item lookup for the turn's practice check takes 148 ms. Memory retrieval takes
  79 ms.

Growth found by the budgets (`tests/test_history_budgets.py`, marked as expected failures):

- **activity:** reads one row per learning event in the streak/momentum window (18.5k rows
  here).
- **profile_refresh:** reads every learning event and message in its recency window.

**Selected for part B by the rule:**

- reviews_due (p95 9.8 s)
- practice (2.3 s)
- plan_revision (1.9 s)
- profile_refresh (2.0 s, and growth)
- turn (299 ms)
- activity (growth)

Four of the slow paths share one cause: the transfer-evidence query and the per-component
evidence count read every learning event for the learner's components. reviews_due also issues
statements per component. turn's cost is the item lookup.

### Part B rerun (2026-10-10)

Measured causes and fixes are in `docs/superpowers/plans/2026-10-10-long-history-performance-part-b.md`.
Same seed (version 2), 30 timed runs after 3 warm-ups, after migration 0077:

| path | p50 ms | p95 ms | max ms | statements | rows | budget |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| turn | 190.6 | 196.9 | 201.8 | 18 | 157 | ok |
| practice | 213.0 | 265.0 | 281.2 | 63 | 1639 | over 250 |
| conversation_list | 3.0 | 4.7 | 6.2 | 2 | 51 | ok |
| transcript | 3.4 | 3.9 | 4.4 | 2 | 202 | ok |
| source_list | 2.4 | 2.6 | 2.8 | 1 | 101 | ok |
| reviews_due | 130.1 | 207.5 | 214.2 | 45 | 5508 | ok |
| activity | 13.2 | 17.5 | 19.0 | 1 | 91 | ok |
| subject_mastery | 99.4 | 112.9 | 175.9 | 5 | 1603 | ok |
| memory_list | 11.7 | 12.2 | 12.3 | 1 | 50 | ok |
| lesson_plan | 2.1 | 2.2 | 2.2 | 1 | 1 | ok |
| plan_revision | 53.2 | 93.5 | 133.3 | 13 | 1522 | ok |
| notes_index | 37.0 | 40.3 | 42.1 | 9 | 92 | ok |
| profile_refresh | 161.6 | 240.2 | 242.9 | 34 | 6056 | ok |

- **reviews_due** 9.8 s → 208 ms: one access check for the queue (was 4,364); retention and
  transfer checks start from milestones on `learner_kc_state` and share one state read; a transfer
  check carries its setting, so resolving its item reads no evidence.
- **plan_revision** 1.9 s → 94 ms and **profile_refresh** 2.0 s → 240 ms: due checks only for the
  plan's subject.
- **turn** 299 ms → 197 ms: `ix_learning_events_learner_item` gained `created_at`, so the
  last-answer lookup is one index read (113 ms → under 1 ms).
- **activity**: counts attempts per day in SQL; 18.5k rows → 91.
- **practice** 2.3 s → 265 ms p95 (213 ms p50): at the line, its largest pieces memory retrieval
  (~55 ms) and the subject's capped transfer read (~50 ms). Recorded rather than chased further
  (decided in conversation 2026-10-10).

No path's statements or rows grow with history (`tests/test_history_budgets.py`, no expected
failures left). S62 is closed.
