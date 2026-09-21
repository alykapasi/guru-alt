# Evidence kinds: separating self-report from demonstration

**Date:** 2026-09-21
**Tracker items:** S56 (evidence kinds), S54 (flashcard reveal and self-rating)
**Workstream:** V0 workstream 2 — learning evidence, goals, and guidance (slice 1 of 4)

## The defect

A flashcard self-rating and an LLM-graded written answer travel the same path.

`app/services/assessment.py:383` builds one `Observation` from whatever `_grade` returned
and hands it to `mastery.DEFAULT_TRACER.update()`. Inside `record_observation`
(`app/learning/mastery.py:313-319`) a single `kc_score` drives both halves of the update:

```python
state.ability = post.ability
state.uncertainty = post.uncertainty
state.last_seen_at = now
state.fsrs_card, state.due_at = scheduler.review(state.fsrs_card, score=kc_score, now=now)
```

Nothing on that path branches on how the score was arrived at. `grade_flashcard`
(`app/learning/grading.py:66-84`) maps an FSRS rating through `_RATING_SCORE`
(`1→0.2 … 4→1.0`) and returns the same `GradeResult` shape as a rubric grade. A learner who
clicks *"I knew that"* moves their measured ability, shrinks its uncertainty, and refreshes
its decay clock — on their own say-so.

Everything downstream inherits the confusion. `kc_evidence` counts the rating as an attempt
backing the estimate. The profile estimators infer pace and optimal challenge from its
score and difficulty. The replay miner treats it as a step to reproduce.

The estimate is the product. An estimate a learner can move by asserting it is not a
measurement, and every consumer that presents it as one is repeating an unchecked claim.

## Principle

**A self-rating may ask for help. It may not make a claim.**

That single line decides every case below:

- It is real evidence about **retention** — whether the memory came back — so it advances
  FSRS scheduling, which is exactly the question FSRS asks.
- It is not evidence about **ability** — whether the learner can do the thing when it
  counts — so it moves neither `ability` nor `uncertainty`.
- It is a real **request**: repeated "I didn't know that" is a learner telling us they are
  stuck, and acting on that is help, not measurement. Struggle detection and prerequisite
  detours keep listening to it.
- It is real **effort**: the learner did the work, so streaks and momentum keep counting it.

## Scope

**In:** the evidence-kind split — deriving the kind server-side, branching the tracer
update, recording the kind in the event log, and correcting every consumer that currently
reads a self-rating as a demonstration. Plus S54's reveal-then-rate interaction and the
copy that tells the learner what a rating does.

**Out, deliberately:**

- **Immutable item/rubric/prompt provenance** (S56's second half). A separate slice; it
  versions three artifacts and threads them through grading and replay, and bundling it
  with a live correctness bug delays the fix for a schema change.
- **Declared conversational checks needing explicit rubrics and difficulty targets**
  (S56's third part). Also its own slice.
- **Goal policy** (S01/S14/S63) and **guidance** (S11/S52) — workstream 2 slices 2 and 3.

## Design

### 1. The kind is derived from the grader, never supplied by the client

The thing that knows whether a judgement was made is the grader that made it. So
`GradeResult` carries the kind:

```python
class EvidenceKind(StrEnum):
    DEMONSTRATED = "demonstrated"
    SELF_REPORTED = "self_reported"
```

`grade_flashcard` returns `evidence_kind=EvidenceKind.SELF_REPORTED`; `auto_grade` and
`grade_open` take the `DEMONSTRATED` default. `Observation` gains the same field with the
same default, and `answer_item` — the single place in the codebase that constructs an
`Observation` — copies it across. `AnswerSubmit`, the client's request body, gains no such
field, so a browser cannot assert that its answer was demonstrated. This is the same
reasoning as the source-derived flag in S25b: a bit the client can set is a bit the client
can drop.

`detail={"rating": rating, "method": "self"}` already half-encodes this. That string is
freeform payload nothing queries; the new field replaces it as the load-bearing signal and
`method` stays only as human-readable detail.

### 2. Self-reported evidence is a distinct event type

`LearningEvent.event_type` is already how this log separates kinds of row:
`observation`, `admin_observation`, `placement_seed`, `detour`. Self-report becomes
`self_report`.

The alternative was a payload key, `payload["evidence_kind"]`. The event type wins on two
counts. It is an indexed column rather than a JSONB probe, so the hot per-item lookups stay
index scans. More importantly it makes **exclusion the default**: every existing consumer
filters `event_type == "observation"` explicitly, so a site nobody remembers to revisit
silently stops seeing self-reports rather than silently counting them as demonstrations.
Forgetting a site should under-count activity, never contaminate a measurement. The
`detour` event's own docstring already records this property of the log.

`EVENT_SCHEMA_VERSION` goes to 4, and its docstring — currently documenting only versions
1 and 2 while the constant reads 3 — is brought up to date in the same change.

### 3. The tracer branches in exactly one place

In `record_observation`, a self-reported observation:

- **advances FSRS** — `scheduler.review(state.fsrs_card, score=kc_score, now=now)` runs
  unchanged, because the self-rating is precisely the recall signal FSRS was designed for
- **leaves `ability` and `uncertainty` alone** — no `estimator.decay`, no
  `estimator.update`
- **leaves `last_seen_at` alone** — see §4
- **still writes one event per tagged KC**, typed `self_report`, carrying the rating, the
  score it mapped to, the item, the timing and the FSRS outcome

The last point matters: the interaction is recorded, not discarded. A self-rating that
vanished from the log would make the learner's history a lie of omission, and S54's own
requirement is that the rating be visible as what it is.

`LearnerKCState` rows are still created for a KC whose first contact is a flashcard —
otherwise there is nowhere to store the FSRS card — at the unknown prior (`ability=0.0`,
`uncertainty=1.0`, `last_seen_at=None`). A KC with FSRS state and no ability evidence is a
true description of that learner.

### 4. `last_seen_at` stays put, and no second clock is needed

`last_seen_at` is read in exactly three places — `mastery.py:190`, `mastery.py:220` and
`mastery.py:306` — and all three feed `estimator.decay(...)` through `_elapsed_days`.
(The fourth mention, `mastery.py:260`, is the admin branch constructing an unpersisted
default.) Nothing displays it. It does not mean "last active"; it means **"when we last
had ability evidence."**

So a self-rating must not touch it. If it did, self-report would suppress the uncertainty
growth that makes a stale estimate look stale — the same contamination as moving `ability`
directly, just slower and harder to see. Leaving it alone means uncertainty keeps growing
until a real demonstration arrives, which is the honest reading.

FSRS keeps its own clock inside `fsrs_card`, so retention scheduling is unaffected. The
two clocks the problem seemed to need already exist; they just have to stop being written
together.

### 5. Consumers

Every site that filters the event log, and what it asks:

| Site | Question it asks | Self-reports |
| --- | --- | --- |
| `mastery.record_observation` | update the model | **split** (§3) |
| `mastery.recent_attempts_at_item` | have they just seen this question | include |
| `mastery.recent_struggle` (S11) | is this learner stuck | include |
| `mastery.prior_failure_kinds` | why did they fail | naturally empty¹ |
| `mastery.kc_evidence` | how much evidence backs this estimate | **exclude**, counted separately |
| `analytics` streak / momentum | did they show up | include |
| `profile_estimators._attempts` | how does this learner learn | **exclude** |
| `assessment` `last_answered` | don't re-serve this item | include |
| `assessment._recorded_grade` | idempotent retry | include |
| `notes` activity probes | is there anything new to distill | include |
| `notes` outcome sample | what did they get right and wrong | **exclude** |
| `tests/eval/datasets/mine.py` | replay dataset | **exclude** |

¹ `grade_flashcard` returns no `diagnoses`, so this query finds nothing to count either
way. Listed so the audit is complete rather than silently short.

Three of these deserve their reasoning written down:

**`kc_evidence` excludes but does not hide.** This is the summary that says how well
supported an estimate is — attempts, distinct items, unassisted items, the span they cover
— and it gates goal claims in slice 2. A self-rating counted here would inflate the
apparent backing of an estimate it did not contribute to. It gains its own
`self_reported_attempts` field instead, so the interaction is visible beside the
demonstrated evidence rather than folded into it. `analytics._NO_EVIDENCE` — the zeroed
stand-in for a KC with no row — gains the field at zero along with it.

**The profile estimators exclude wholesale.** Every dimension they compute reads `score` or
`difficulty`: optimal challenge, pace, cognitive load, error types. Inferring "this learner
thrives at difficulty 0.7" from scores the learner assigned themselves is circular. The
cost is that flashcard latency stops feeding pace estimates — acceptable, because a
reveal-then-rate interaction's latency measures reading speed and honesty, not retrieval
effort.

**The notes outcome sample excludes.** A distilled note asserting "you struggled with
photosynthesis" sourced from a self-rating misrepresents the learner to themselves. The
activity probes that decide *whether* to distill keep counting self-reports, because
reviewing flashcards is activity.

### 6. `assessed` must stop meaning "has a state row"

`subject_mastery` decides whether a component has ever been assessed by the existence of a
`LearnerKCState` row (`app/services/analytics.py:66-75`). That was sound while a state row
could only be created by a graded answer. Under §3 it is not: a KC whose only contact is a
flashcard gets a state row so the FSRS card has somewhere to live, and it would then report
`assessed=true` with `ability=0.0` — which `KCMasteryRead` renders as **50% mastery derived
from no evidence**, the exact failure its own docstring says the flag exists to prevent.

So `assessed` is re-grounded on `last_seen_at IS NOT NULL`. Under §4 that field now means
precisely "we have had ability evidence here", which is the question `assessed` is asking.
No new column, and the flag becomes a direct reading of the thing it reports.

This changes an API response's values, not its shape. `KCMasteryRead` gains one field
(`self_reported_attempts`, §5) with a default, so the contract gate sees an additive change.

### 7. Index change

`ix_learning_events_learner_item` is partial on `WHERE event_type = 'observation'`, and two
of the sites that should include self-reports (`recent_attempts_at_item`, `last_answered`)
depend on it. Its predicate widens to `event_type IN ('observation', 'self_report')` in an
Alembic migration. No data migration accompanies it — see below.

### 8. No backfill

The `guru` database holds **1 observation row and 0 self-rated events**. There is no
contaminated history to recompute, so this is a fix going forward.

A backfill would have been the expensive part of this slice and it is worth being explicit
about why it is skipped: not because it is hard, but because the query says there is
nothing to fix. Should that change before this lands, the position stays the same —
existing rows keep the `observation` type and are read as demonstrated, which is what they
were recorded as. S56 already declares insufficiently-detailed legacy events unreplayable;
this is a narrower case of the same rule.

### 9. S54: reveal, then rate

The flashcard panel presents the prompt with a **Reveal** control. The answer and the
rating buttons (1–4, Again … Easy) appear only after reveal, in one step so a learner
cannot rate without having seen what they were rating against.

Beneath the rating buttons, in plain words: **a self-rating sets when this comes back, not
what Guru thinks you know.** The learner deserves to know which of the two things their
click does, and saying so is also the honest defence against the rating being treated as a
score.

Answer keys stay withheld before submission for every other item type — that is already
true (`app/learning/item_presentation.py`) and the reveal path must not open a hole in it.
The flashcard's answer is served on reveal, for flashcards only.

## Testing

The split is only real if it is pinned at both ends. Every test below must be shown to fail
against the current code before the fix.

**The core claim, twice:**

- A self-rated flashcard leaves `ability`, `uncertainty` and `last_seen_at` byte-identical
  while `due_at` and `fsrs_card` advance.
- A graded answer at the same KC still moves all five.

**The clock:** a learner who self-rates daily for a month, then answers a graded item, is
scored against an estimate that decayed across the whole month — not one refreshed by the
ratings. This is the test that catches `last_seen_at` regressing, and it is the one that
would otherwise be missed, because a single self-rating looks correct either way.

**A KC whose first contact is a flashcard** gets a state row with an FSRS card and the
unknown prior, and reports zero demonstrated evidence. On the mastery drill-down that same
KC comes back `assessed=false` — the §6 regression, and the one with a user-visible
consequence: without it the page shows a confident-looking 50% for a component nobody has
ever been measured on.

**Per consumer:** one test per row of the §5 table asserting inclusion or exclusion. The
excluding ones are the load-bearing half — `kc_evidence` reporting
`attempts=0, self_reported_attempts=3` after three ratings is the assertion that stops a
self-rating being presented as backing.

**Idempotency:** a retried self-rated attempt replays from the log exactly as a graded one
does, including through `_recorded_grade`'s event-type filter.

**Client cannot assert the kind:** a request body carrying an `evidence_kind` field is
ignored, and a flashcard submission is recorded as self-reported regardless of what was
sent.

**Frontend:** the rating buttons are absent before reveal and present after; the
explanatory line is on the panel.

Verification follows the established pattern: each new guard is regressed on purpose and
the named test must fail. `uv run poe check` and `npm run build` green per commit.

## What this does not fix

- **A learner can still self-rate honestly and be wrong.** Self-report is noisy evidence
  about retention too; this slice stops it being evidence about ability, which is the
  larger error.
- **Flashcard difficulty stays nominal.** `item.difficulty` is still recorded on the
  event, and still means nothing calibrated for a self-graded item. Difficulty calibration
  is not in this workstream.
- **The provenance gap remains open** (S56, deferred above): a grade still cannot be
  reproduced against the exact rubric and prompt version that produced it.
