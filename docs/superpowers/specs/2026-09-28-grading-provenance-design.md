# Grading provenance and re-grading (S56, versioning part)

**Status:** approved in conversation 2026-09-28; this document records it.
**Tracker:** S56 (its second half: reproducible grading history). Workstream 2, piece 1 of 4
(then declared-check rubrics, delayed probes, transfer).

## Problem

- A grade records the score, the response and the item id, but not what it was measured
  against. Items and rubrics are never edited today, so the id pins the content only by
  convention, and deletion (a KC cascade, forgetting derived material, account erasure) takes
  the content away while the event stays.
- The grading prompt lives in code (`app/learning/rubric_grading.py`) and changes with a deploy.
  S48 records a `prompt_hash`, model and app version on the grading call's `llm_calls` row, but
  the learning event does not point at it, and a hash cannot give the prompt back.
- "Replay" today means replaying the mastery tracer from the event log (exact since event schema
  v2). Nothing can put a past answer through the grader again, so a change to the grading prompt
  or model cannot be measured against real answers (S59).

## Decisions (from the design conversation)

1. **Purpose (A):** audit a past grade, and re-grade past answers under a new prompt or model to
   compare. Exact reproduction of an LLM's output is not a goal.
2. **Approach (1):** content-addressed, per-learner snapshots, referenced from the event.

## Design

### Snapshots — `grading_snapshots`

Migration 0072 creates:

| column | type | notes |
| --- | --- | --- |
| `learner_id` | uuid, FK `learners.id` ON DELETE CASCADE | whose evidence this explains |
| `sha256` | text | hex SHA-256 of the canonical JSON of `content` |
| `kind` | text | `item` \| `rubric` \| `prompt` |
| `content` | JSONB | the frozen artefact |
| `created_at` | timestamp | naive UTC, server default |

Primary key `(learner_id, sha256)`. Rows are inserted with `ON CONFLICT DO NOTHING` and never
updated or deleted except by the learner cascade, so an unchanged item costs one row however
often it is graded. There is no foreign key to `items` or `rubrics`: deleting either leaves the
history intact. Canonical JSON is `json.dumps(content, sort_keys=True, separators=(",", ":"),
default=str)`.

Contents:

- **item:** `item_type`, `stem`, `answer_key`, `difficulty`, and `components` — the KC ids and
  names the item was graded on, in the order handed to the grader.
- **rubric:** the criteria and the component each applied to, exactly as handed to the grader
  (`_components_of` in `app/services/assessment.py` attaches a rubric only to its own KC).
- **prompt:** `system` (the grading system prompt text actually used — the single-component or
  per-component one) and `template_version`, a constant in `app/learning/rubric_grading.py`
  bumped whenever the code that builds the user message from item, rubric and response changes.

### The event

`EVENT_SCHEMA_VERSION` goes to 5. Every `observation`, `self_report` and `admin_observation`
event written from `answer_item` gains:

```json
"grading": {
  "grader": "auto" | "self" | "rubric" | "jev",
  "item": "<sha256>",
  "rubric": "<sha256>" | null,
  "prompt": "<sha256>" | null,
  "model": "<provider>:<model>" | null,
  "app_version": "<settings.app_version>"
}
```

- `grader`: `auto` for deterministic grading, `self` for a flashcard self-rating, `rubric` when
  the rubric model graded, `jev` when a live Jev pass decided without the model call.
- `rubric`, `prompt`, `model` are null where they do not apply (no rubric on the item; an auto,
  self or Jev grade has no grading prompt or model).
- `GradeResult` (`app/learning/grading.py`) gains a `provenance` field that each grading path
  fills: `auto_grade` and `grade_flashcard` set the grader; `grade_open` sets `rubric`, the
  system prompt used, the template version and the model spec; `decide_grade` sets `jev` when
  its live answer stands. `answer_item` writes the snapshots and copies the hashes into the
  observation, and `record_observation` puts them on every per-KC event of the attempt.
- Events written before v5 have no `grading` block. Nothing is backfilled: a reconstruction of
  what was graded is a guess, not provenance.

### Re-grading — `uv run poe regrade`

An operator command, dry run by default like `poe reindex`; paid calls only with `--run`.

- **Selection:** graded events (`observation`, `admin_observation`) with a `grading` block and a
  stored `response`, filtered by `--since` / `--until` (default: the last 7 days), optionally
  `--learner` and `--subject`, one per attempt, capped by `--limit` (default 200). `self_report`
  events are skipped: there is nothing to re-judge.
- **The grader compared against:** `--prompt recorded|current` (default `current`) chooses the
  snapshot's system prompt or today's; `--model provider:model` (default: the current
  `GRADING_ROLE` model). Auto-graded events are re-graded with `auto_grade` against the
  snapshot's answer key — free, and it catches answer-key or grader changes. Jev-graded events
  are re-graded by the rubric model.
- **Dry run:** counts of qualifying events by grader, of events not re-gradable (no `grading`
  block, or a snapshot missing), and the estimated cost from the price table.
- **Run:** each response goes through the chosen grader, attributed to feature `regrade` with no
  learner (so it counts against the deployment ceiling, never a learner's cap). It writes a JSON
  report and prints: agreement on `correct`, mean absolute score difference, per-component
  agreement where both sides scored components, and the largest disagreements by event id and
  score. No learner text is written to the report.
- **Never corrective:** a re-grade changes no score, event or mastery state.

## Errors

- A failed snapshot write fails the grade's transaction: an answer recorded without its
  provenance is exactly the gap this closes. The inserts use the grading session, not the
  accounting one.
- A snapshot missing at re-grade time (erased) → the event is counted not re-gradable and
  skipped.
- A re-grade call that fails, or is refused by the spend guard, is counted `failed` in the
  report; the run continues.

## Privacy

Snapshots of private items and the stored response are the learner's data: they are included in
the account export and removed by account erasure (the `learner_id` cascade). The re-grade report
carries ids and scores only.

## Testing

All with the fake LLM.

- Each grader writes its `grading` block and snapshots: auto, self, rubric, and a live Jev pass.
- The same item graded twice stores one item snapshot.
- Deleting the item leaves the snapshot and the event intact.
- Account erasure removes the snapshots; the export includes them.
- A pre-v5 event and an event whose snapshot is missing are reported not re-gradable.
- The dry run makes no calls.
- A run against a fake model with a different verdict reports the disagreement, and the learner's
  scores and mastery are unchanged afterwards.
- `--prompt recorded` sends the snapshot's system prompt text.
- Migration 0072, and the tracer replay tests still pass at schema v5.

## Docs

RUNBOOK section "Grading provenance and re-grading": reading a grade's provenance, running
`poe regrade`, what "not re-gradable" means. Tracker: S56 notes the versioning part done;
declared-check rubrics stay open for the next piece. CLAUDE.md: one bullet.

## Out of scope

Making items and rubrics immutable in the database (nothing edits them); re-grading that corrects
a score; explicit rubrics for declared conversational checks (next piece); an admin UI for
provenance.
