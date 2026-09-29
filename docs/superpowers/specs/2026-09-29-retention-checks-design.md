# Delayed independent retention checks (S14, "slice 2b")

**Status:** approved in conversation 2026-09-29; this document records it.
**Tracker:** S14 (its first remaining half). Workstream 2, piece 3 of 4 (after grading provenance
and declared-check criteria; before transfer). Deferred from the
[goal-policy design](2026-09-22-goal-policy-design.md) §1 and §11.

## Problem

- Retention means **two unaided demonstrations at least `retention_min_days` apart**
  (`KCEvidence.retention_shown`), and a component is achieved only with it. An unaided
  demonstration is a *judged* attempt (event type `observation`, right or wrong) with no hints
  and not a re-look at the same question.
- Nothing arranges for the second one. Reviews come from FSRS and are served as flashcards —
  self-ratings, which are never demonstrations — unless the component keeps failing. So the
  evidence retention needs arrives only if something happens to re-test the component, unaided,
  later.
- The unaided rule (`_unassisted_clause`) reads `hints_used` and `prior_attempts` but not
  `taught_first`, so a guided-practice answer given straight after a worked example counts as
  unaided. The goal-policy design did not intend that, and it makes deliberate checks nearly
  redundant.

## Decisions (from the design conversation)

1. **Where (A):** in the review queue. A due retention check *is* a review step — the moment the
   product already has for "come back to this later".
2. **When (hybrid):** a check is due at FSRS's review if that falls at least `retention_min_days`
   after the component's latest unaided demonstration, and on its own at `retention_probe_days`
   (default 7) if FSRS has not surfaced it by then. It stops once retention is shown; keeping
   evidence fresh afterwards is freshness work, reported only by earlier decision.
3. **Taught-first is not unaided:** fixed as part of this slice.

## Design

### When a check is due — `mastery.due_retention_checks`

`due_retention_checks(session, learner_id, *, now=None) -> list[RetentionCheck]` returns the
components for which, at `now`:

1. `KCEvidence.unassisted_attempts >= 1`;
2. `retention_shown(min_days=retention_min_days)` is false;
3. `days since last unassisted demonstration >= retention_min_days`; and
4. either the component's FSRS `due_at <= now`, or `days since last unassisted demonstration
   >= probe_days`,

where `probe_days = max(retention_probe_days, retention_min_days)` — a check sooner than
`retention_min_days` could not count. `RetentionCheck` carries `kc_id`, `due_at` (the earlier of
the FSRS due date and `last_unassisted + probe_days`), `ability` and `uncertainty`, and is
ordered soonest-due first. Derived entirely from `learning_events` and `learner_kc_states`:
nothing is stored, so a missed check stays due and a backlog catches itself up.

Setting: `retention_probe_days: float = 7.0` in `app/core/config.py`, beside
`retention_min_days`, uncalibrated and saying so, like its neighbours.

### The unaided rule

`_unassisted_clause` also requires `coalesce(payload->>'taught_first', 'false') != 'true'`. A
first-round guided-practice answer after a worked example no longer counts toward retention.
Achievements already recorded stand (achievement is recorded and kept, by design); only later
evaluations read the stricter rule. `unassisted_items` / `unassisted_attempts` shown on the
dashboard move accordingly.

### The plan

- `lesson_plan._due_review_kc_ids` (services) returns this subject's due reviews and due
  retention checks together, soonest first, plus the set of components whose entry is a check.
- `revise_steps` gives a due-check component a `review` step carrying
  `retention_check: True` (a new `NotRequired[bool]` on the step), or sets the flag on the
  component's open review step. A component due both ways gets one step, flagged. Ordering and
  closure are the review step's: it closes when the component is no longer due (retention shown,
  or answered unaided so condition 3 fails again).
- `PlanGroundingContext` gains `retention_check: bool`, read like `check_first`.

### Guided practice on a retention check

- The step is run as a check-first step: `CHECK_FIRST_SYSTEM_PROMPT`, no worked example,
  `taught_first=False`.
- Its item is `item_for_kc(..., preferred_type=ItemType.SHORT)` at the practice target — an
  unseen item where the learner has one, generated otherwise; never a flashcard, which cannot
  count.
- Help is not blocked. Extra rounds and a paused side discussion are counted as hints as today,
  so an answer given after help is recorded as assisted and does not count.
- Graded through `answer_item` like any answer: provenance, FSRS, tracer, plan revision.

### API and UI

- `ReviewItemRead` gains `kind: Literal["review", "retention_check"]`. `due_review_items` merges
  `due_retention_checks` into the FSRS list (one entry per component, a check wins); a check's
  resolved item is the SHORT one above.
- `ReviewsDueCard` labels check rows "Retention check". API types regenerated
  (`uv run poe api-types`), contract checked (`uv run poe api-contract`).

## Errors

- A component the learner can no longer see is skipped, as reviews are (`_kcs_authorized`).
- Item generation failing leaves the step without an item, the existing review path; it is still
  due at the next revision.
- A wrong answer to a check still counts as an unaided demonstration: the tracer lowers the
  estimate, and achievement's ability bar holds it back until the learner succeeds.

## Testing

- `due_retention_checks`: not due before `retention_min_days`; due when FSRS surfaces it after
  that; due at `retention_probe_days` without FSRS; not due with no unaided answer; not due once
  retention is shown; `retention_probe_days < retention_min_days` is clamped up.
- The unaided rule: a taught-first answer does not count; a check-first answer does. Existing
  retention/evidence tests updated where they relied on taught-first answers.
- The plan: a due check becomes a flagged review step; due both ways is one step; the step closes
  once retention is shown.
- Guided practice on a flagged step: the check-first prompt, `taught_first=False`, a SHORT unseen
  item even where a flashcard would be chosen, and a first-round answer that counts as unaided;
  an answer after a hint round does not.
- `/reviews/due` returns `kind`; the dashboard card shows "Retention check" (vitest).

## Docs

Tracker S14: delayed independent checks done; transfer remains (piece 4). Goal-policy design §11:
a pointer to this document. CLAUDE.md: the achievement bullet notes that an answer after a worked
example is not unaided. No RUNBOOK change.

## Out of scope

Transfer (piece 4); checks after retention is shown (freshness); calibrating
`retention_probe_days`; blocking help during a check; chat-posed retention checks.
