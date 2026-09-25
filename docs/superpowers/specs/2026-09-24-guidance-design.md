# Guidance: learner-controlled detours and explicit pause, resume, and skip

**Slice 3 of 4, V0 workstream 2.** Tracker items: **S11** (learner-controlled prerequisite
detours) and **S52** (pause, resume, and skip practice explicitly). Accepted decision: **V07 —
Guidance** in `docs/V0_DECISIONS.md`.

Builds on slices 1–2 on branch `feat/evidence-kinds` (unmerged). Slice 1's evidence kinds are a
hard dependency: "one demonstrated, unassisted pass" below is only expressible because a
self-rating is no longer an ability observation.

---

## 1. What this slice decides

V07: *Exploration proposes substantial prerequisite detours; guided mode may take them
automatically. Both allow skipping. Side discussions pause practice, with explicit resume/skip
behavior and preserved attempt state.*

Today:

- **Detours are fully automatic.** `revise_plan` inserts a detour step ahead of everything and it
  ends only when the prerequisite reaches the planner's mastery bar. Since slice 2 that bar is
  conservative (`ability − uncertainty ≥ 0.5` with measured evidence), so a detour that guessed
  wrong — the learner already knew the prerequisite — is the one that takes longest to leave.
  There is no skip and no learner choice, and nothing stores a guidance setting (S02 is Open).
- **Guided practice has no intent gate.** While the workflow graph waits at `await_response`,
  every learner message is graded against the item. "Wait, what does *derivative* mean?" becomes
  a failed attempt that reaches the tracer — the same false-evidence failure slice 1 fixed for
  self-ratings. The tutor-chat check already has a gate (S15: `classify_intent` → attempt,
  deferral, withdrawal); the workflow never got one.

This slice adds a per-plan guidance setting, learner decisions on detours, an evidence-based
early exit from a wrong detour, and a paused practice state with explicit controls.

**Out of scope:** S02's global defaults and presentation preferences (a later global default only
has to seed the per-plan column); a size-based definition of "substantial"; pause/resume for the
refinement gate; delayed independent probes (slice 2b).

## 2. Guidance setting

`LessonPlan.guidance: str`, one of `"guided"` / `"exploration"`, server default `'guided'`, not
null, no backfill. **Guided reproduces today's behaviour exactly**; every existing detour test is
a guided-mode test and must pass unchanged.

`PATCH /subjects/{subject_id}/lesson-plan/guidance` with `LessonPlanGuidanceSubmit(guidance:
Literal["guided", "exploration"])`, returning `svc.plan_read(...)` like the closure endpoint.
`require_visible_subject` first; 404 when no plan exists. Changing the setting does not rewrite
existing steps: an already-active detour stays active, an existing proposal stays a proposal.

`LessonPlanRead.guidance` is exposed.

## 3. Detour steps: proposal, decision, outcome

### 3.1 Step statuses and keys

`StepStatus` gains `"proposed"` and `"skipped"`:

| Status | Meaning | Counts as open | Can become active |
| --- | --- | --- | --- |
| `pending` / `active` | as today | yes | yes |
| `proposed` | a detour offered in exploration, awaiting the learner | yes, for "already open" | no |
| `done` | finished | no | no |
| `skipped` | the learner declined or abandoned the detour | no | no |

`proposed` and `skipped` apply to `step_type == "detour"` only.

New `NotRequired` keys on a detour step:

- `opened_at: str` — ISO-8601 UTC, set when the step becomes active (on insertion in guided; on
  acceptance in exploration). Absent on detours written before this slice.
- `detour_outcome: Literal["mastered", "disproved", "skipped"]` — set when the step closes.

### 3.2 Insertion by mode

The trigger (`_prerequisite_detour`) is unchanged. `engine.revise_steps` takes a new
`guidance` parameter:

- **guided:** insert `status="pending"` with `opened_at=now`; the existing ordering makes it
  active. (Today's behaviour plus the new key.)
- **exploration:** insert `status="proposed"`, no `opened_at`. A proposed step is never made
  active by the status recomputation; the blocked step stays active. It is ordered directly
  before the step it was proposed for.

Every detour in exploration is proposed: any detour leaves the objective, and the tracker's
"substantial" has no measurable definition in the current graph. Recorded as a decision, not a
gap.

"Already open" (the check that stops a second step for the same prerequisite, and
`_open_detour_keys`) counts `pending`, `active`, and `proposed`; not `done` or `skipped`.
`horizon_extension`'s open-step count excludes `proposed` and `skipped`.

`record_detour` is written on insertion as today, for both a proposal and an automatic detour —
the event's `payload` gains `"proposed": bool`.

### 3.3 Decision endpoint

`POST /subjects/{subject_id}/lesson-plan/detours/{prereq_kc_id}` with
`DetourDecisionSubmit(decision: Literal["accept", "skip"])`, returning `plan_read`.

- `require_visible_subject` first.
- The target is the open (`proposed`/`pending`/`active`) detour step whose `kc_id` is
  `prereq_kc_id`. None → **409**.
- **accept:** only valid on `proposed` (else 409). Sets `pending`, `opened_at=now`, then runs the
  normal revision so it becomes active.
- **skip:** valid on `proposed`, `pending`, or `active`, in either mode. Sets `skipped`,
  `detour_outcome="skipped"`, then revises (the blocked step becomes active again).

Both write in one transaction with the outcome event below.

### 3.4 Closing a detour, and the return

In `revise_steps`, an open, accepted detour step (`pending`/`active`) closes as:

1. **`mastered`** — its KC is in `mastered_kc_ids` (today's rule).
2. **`disproved`** — its KC is in a new `disproved_kc_ids` input: the service found a **run**
   of `detour_disprove_passes` (default 3) **demonstrated, unassisted, untaught** passes
   (`score >= detour_failure_threshold`) on different questions with `observed_at >= opened_at`,
   counted back from the latest attempt. A failure ends the run; a helped or taught pass neither
   counts nor breaks it. One pass is too small a sample to call the learner solid — a guess or
   an easy item gets there — so it only feeds mastery, as every answer does. A step without
   `opened_at` (pre-slice) can never be disproved — there is no trustworthy start time, and an
   old pass must not close a new detour.

Both set `status="done"` and the outcome. Mastered wins when both hold. The return needs no new
mechanism: with the detour closed, the ordering makes the blocked step active again.

`disproved_kc_ids` comes from a new `mastery.passed_since(session, learner_id, {kc_id:
opened_at}, *, threshold, passes) -> set[uuid.UUID]`. It uses the same "demonstrated" and "unassisted"
definitions as `kc_evidence` (slice 2), not a second copy of them. A self-rating
(`self_report` event) can never disprove.

A proposal is never closed by evidence: the learner has not decided yet, and if the prerequisite
becomes mastered the proposal is dropped (status `done`, outcome `mastered`) since there is
nothing left to offer.

### 3.5 Remembering outcomes

A closed detour writes a `LearningEvent` with `event_type = DETOUR_OUTCOME_EVENT =
"detour_outcome"` (admin variant `"admin_detour_outcome"`, following `record_detour`), `kc_id =`
the blocked component, payload `{prereq_kc_id, outcome}`. It is written by the service in the
same transaction as the revision or decision that closed the step, detected by diffing step
outcomes before and after (the same pattern `_open_detour_keys` already uses for insertion).

`_prerequisite_detour` drops a candidate prerequisite for a blocked component when an outcome
event says `skipped` for that pair (new `mastery.closed_detour_routes(session,
learner_id, blocked_kc_id) -> set[uuid.UUID]`). `mastered` does not exhaust a route: the detour
worked. Nor does `disproved`: it is an inference from a handful of answers and can be wrong, and
a permanent bar would make a wrong one unrecoverable. `detour_max_repeats` stays as the backstop. Nothing clears these except a new plan
generation for a different goal (which already starts from fresh steps; the events are kept).

`ATTEMPT_EVENTS`, retention counters, analytics, and replay must ignore `detour_outcome` the
same way they ignore `detour` today — it is a decision record, not evidence.

## 4. Paused practice

### 4.1 State

`ConversationPhase.PRACTICE_PAUSED = "practice_paused"`. While paused, the workflow checkpoint
is untouched: same item, `rounds`, and everything else in `WorkflowState`. Pausing only changes
where the next message goes.

`Conversation.practice_scaffolds: int` (not null, server default 0) counts tutor replies given
while practice was paused. It resets to 0 whenever a new practice item is presented and on skip.

### 4.2 Routing in `_choose_flow`

When `workflow_awaiting` is true and `data.mode != "agentic"`:

1. **Phase is `PRACTICE_PAUSED`:** route to `TUTOR`. No gate, no grading.
2. **A rating is attached** (`data.rating is not None`): resume the workflow, no gate — a rating
   is an explicit attempt.
3. **Otherwise run the intent gate** (`conversation_evidence.classify_intent` with the item's
   stem; one FAST call, logged through `log_llm_call` like the chat check):
   - `ATTEMPT` → resume the workflow and grade, as today.
   - `DEFERRAL` → set `PRACTICE_PAUSED`; route this turn to `TUTOR`.
   - `WITHDRAWAL` → skip (§4.3) then route to `TUTOR`.
   - Every gate failure is `DEFERRAL` (inherited from `parse_intent`). A wrong pause costs the
     learner one click; a wrong grade writes false evidence.

Resuming is only ever the explicit control (§4.3): a message sent with `mode="workflow"` while
paused is still routed to `TUTOR`. If the phase is `PRACTICE_PAUSED` but `is_awaiting_reply`
reports no live, current checkpoint (it went stale or was lost), the phase is reset to
`CHATTING` and routing proceeds as if nothing were paused.

`_phase_after` must return `PRACTICE_PAUSED` for a tutor turn taken while paused (today's
`workflow_paused and flow is not WORKFLOW → AWAITING_ANSWER` rule would undo the pause), and
the agentic interjection rule keeps its current meaning when not paused.

A tutor turn taken while paused:

- increments `practice_scaffolds` by one;
- **does not open its own check** (a second question would compete for `active_item_id`);
  the tutor service gets an explicit flag for this rather than inferring it.

### 4.3 Explicit controls

`POST /conversations/{conversation_id}/practice` with `PracticeActionSubmit(action:
Literal["pause", "resume", "skip"])`, returning `PracticeStateRead(phase, item: ItemRead | None,
prompt: str | None, ended: bool)`. Non-streaming. Scoped to the owning learner (404 otherwise, as
the other conversation routes). Takes the conversation's turn lock (`app/services/turn_lock.py`)
so it cannot interleave with a streaming turn; a held lock → 409.

- **pause** — valid only while the workflow is awaiting an answer (`AWAITING_ANSWER` with a live
  workflow checkpoint). Sets `PRACTICE_PAUSED`. Else 409.
- **resume** — valid only in `PRACTICE_PAUSED`. Runs `checkpoints.paused_practice_is_current`:
  - stale → discard the thread, phase `CHATTING`, return `ended=true` (200; the learner did
    nothing wrong);
  - current → phase `AWAITING_ANSWER`, return the stored `last_message` as `prompt` and the item.
    No LLM call, no new message row.
- **skip** — valid in `AWAITING_ANSWER` or `PRACTICE_PAUSED`.
  - Workflow practice: `checkpointing.discard_thread`, `practice_scaffolds = 0`.
  - Tutor-chat check (no workflow checkpoint): clear `active_item_id` and
    `active_item_scaffolds`, exactly the existing withdrawal path.
  - Phase `CHATTING`. **No `LearningEvent`, no FSRS change.**

### 4.4 Assistance after a pause

When the workflow grades an attempt, `hints_used = rounds + practice_scaffolds`. Help received
during a side discussion is help, and is discounted through `assistance.evidence_credit` like
any hint. `practice_scaffolds` is read from the conversation at resume time and passed into
the `Command(resume=...)` payload, so the graph stays free of DB reads outside `grade`.

## 5. Frontend

- **Plan panel (`LessonPlanPanel`, `LessonStepRow`):** a Guided / Exploration toggle. A proposed
  detour row shows **Take detour** / **Skip**; an active detour row shows **Skip**. A closed
  detour reads by outcome — `disproved`: "Turned out not to be the gap — back to {blocked}";
  `skipped`: "Skipped"; `mastered`: as today.
- **Session / chat composer:** while an answer is expected, **Pause** and **Skip** controls. In
  `PRACTICE_PAUSED` a strip: "Practice paused — [Back to the question] [Skip it]". Resume shows
  the returned prompt; `ended` shows "That question no longer fits your plan, so practice ended."
- `schema.d.ts` regenerated; type gate is `npm run build`.

## 6. Data

Migration `0059_guidance` (down_revision `0058_goal_policy`):

- `lesson_plans.guidance` text not null server default `'guided'`;
- `conversations.practice_scaffolds` integer not null server default `0`.

Phase and step statuses are strings in existing columns / JSON; no DDL. Existing plans are guided;
existing open detours have no `opened_at` and can close only as `mastered` or `skipped`.

## 7. Errors

| Case | Response |
| --- | --- |
| Invisible subject / foreign conversation | 404 (existing helpers) |
| No plan for the subject | 404 |
| Detour decision on a missing or closed detour, or accept on a non-proposed one | 409 |
| Invalid guidance / decision / action value | 422 |
| Practice action in the wrong phase | 409 |
| Turn lock held | 409 |
| Resume of stale practice | 200, `ended: true` |

## 8. Tests

Behaviour through the service and API call paths (tracker verification rule: a helper alone does
not establish its callers).

**S11**

1. Guided inserts an active detour — existing detour tests pass unchanged.
2. Exploration inserts `proposed`; repeated revisions never make it active; the blocked step
   stays active.
3. Accept makes it active with `opened_at`; skip on proposed and on active returns the blocked
   step to active, in both modes.
4. A skipped route is not proposed again for that component; disproved and mastered ones are
   not excluded.
5. A run of `detour_disprove_passes` demonstrated, unassisted, untaught passes on different
   questions after `opened_at` closes the detour as `disproved`; fewer does not, a failure
   restarts the count, and the same question answered again counts once.
6. **Guards:** a pass before `opened_at` does not disprove; an assisted pass does not; a
   self-rating does not; a detour without `opened_at` does not.
7. Outcome events are written once per closure and ignored by attempts, retention, analytics.
8. Guidance PATCH and detour POST over HTTP, including 409s; visibility sweep cases for both.

**S52**

9. A deferral while the workflow awaits pauses and writes no `LearningEvent`.
10. A message while paused is routed to the tutor and never graded; the tutor opens no check.
11. Resume then attempt grades with `hints_used == rounds + practice_scaffolds`.
12. Skip (workflow and tutor-chat check) writes no event, discards the checkpoint, sets
    `CHATTING`.
13. Resume after the plan moved on returns `ended: true`.
14. **Guards:** a gate failure pauses rather than grades; a rating bypasses the gate;
    `_phase_after` keeps `PRACTICE_PAUSED` across a paused tutor turn.
15. Practice endpoint over HTTP including wrong-phase 409s and the turn-lock 409; visibility
    sweep case.

**Frontend:** detour row states and decisions, guidance toggle, pause strip and resume/ended
copy.

Each named guard (6, 14) gets a mutation proof: remove the guard, see a test fail with a
summary line, restore.
