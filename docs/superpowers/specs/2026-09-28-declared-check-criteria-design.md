# Criteria and difficulty for conversational checks (S56, declared-check part)

**Status:** approved in conversation 2026-09-28; this document records it.
**Tracker:** S56 (its last remaining part). Workstream 2, piece 2 of 4 (after grading
provenance; before delayed probes and transfer).

## Problem

- A check the tutor declares in chat (`[[CHECK: component :: question]]`, S15) becomes a SHORT
  item with **no rubric** (`_materialise_declared_check` in `app/services/chat.py`). Its answers
  are graded against `grade_open`'s fallback, "grade on correctness and completeness" — a
  standard the grader improvises per answer.
- The same item is recorded at **difficulty 0.0**, the column default. `item_generation` stopped
  making that unearned claim for generated items (S12); declared checks still make it, so the
  tracer scores every one as if pitched at the population average.
- A plan-posed check whose generated criteria failed to parse has the same missing rubric.

## Decisions (from the design conversation)

1. **Criteria are written lazily (B):** one model call when a check is first *attempted*, not
   when it is declared (pays for unanswered checks) and not inside the tutor's marker (the reply
   streams to the learner, so criteria — which usually contain the answer — would flash on
   screen).
2. **Difficulty, both ways (3):** the tutor is *aimed* with a level from the learner's subject
   roll-up; the level *recorded* is the one the criteria call rates the question as asked.
3. **No Jev here.** Jev never writes text, so it cannot write criteria; a band rating rides free
   on the criteria call, so a Jev band question would save nothing. A shadow-only Jev band
   question is noted as a possible later study.

## Design

### Flow

1. **Aiming.** Where `declared_check.INSTRUCTION` is added to the tutor's notes (subject-scoped,
   a check may be posed), one sentence is appended: "Pitch any check at this level: <band>."
   The band is `difficulty.describe(session_runner.practice_target(mastery.rollup_subject(...)))`
   — the same practice target plan-driven items use, over the whole subject because the
   component is not known until the tutor names it. No model call; nothing recorded.
2. **Declaring.** `_materialise_declared_check` is unchanged: SHORT, private to the learner, no
   rubric, difficulty untouched. Nothing has judged the question yet.
3. **First attempt.** In `_resolve_check`, after the intent gate reads the message as an
   `ATTEMPT` and before `answer_item`, `assessment.ensure_criteria(session, llm, learner_id,
   item)` runs when `item.rubric_id` is null. It applies to any rubric-less check, declared or
   plan-posed.
4. **Later attempts** reuse the stored rubric and difficulty: no further call. Grading
   provenance (piece 1) snapshots both on every graded event.

### The criteria call — `item_generation.write_criteria`

`write_criteria(llm, *, stem, component_name, component_description) -> (criteria, band, usage)`:

- Role `GENERATION_ROLE` (FAST), like `generate_short_item`, which already writes criteria.
- JSON only: `{"criteria": ["<what a full-credit answer must show>", ...], "level": "<band>"}`,
  two to four specific, checkable criteria; `level` one of the five band names in
  `app/learning/difficulty.py` (`introductory` … `demanding`), each listed with its meaning.
- Sees the question and the component's name and description only — **never the learner's
  answer**, so the standard cannot bend to the answer it will grade.
- Parsing is tolerant: `criteria` is the non-empty strings (possibly none); `band` is `None`
  unless `level` names a band exactly (case-insensitive).

### Storing — `assessment.ensure_criteria`

`ensure_criteria(session, llm, learner_id, item) -> Item`, `@metered("check_criteria",
learner="learner_id")`:

- Locks the item row (`SELECT … FOR UPDATE`) and re-reads `rubric_id`; if another answer wrote
  criteria meanwhile, returns the item reloaded with that rubric and makes no call.
- Calls `write_criteria` with the item's stem and its first component's name and description.
- Criteria present → a `Rubric(owner_learner_id=learner_id, kc_id=<first component>, name="Criteria
  for a conversational check", criteria={"criteria": [...]})`, linked by `item.rubric_id` (the
  shape generated items use, so `_components_of` attaches it to that component).
- Band present → `item.difficulty = difficulty.midpoint(band)`; a new
  `difficulty.midpoint(band) -> float` returns −2, −1, 0, 1, 2 for the five bands.
- Flushes; `answer_item` commits it with the grade. Returns the item reloaded with its rubric.

## Errors

- The criteria call refused by the spend guard: the refusal propagates like any other paid call
  in a turn (`refusal_ends_turn`); nothing is recorded, and the check stays open.
- A provider failure or an unusable reply never blocks grading: the answer is graded the old way
  (no rubric, difficulty unchanged) and `check.criteria_unavailable` is logged with ids only.
- Criteria without a valid level: criteria kept, difficulty left as it was.
- Concurrent first attempts: the row lock makes the second wait and reuse the first's rubric;
  never two rubrics for one item.

## Testing

All with the fake LLM.

- A declared check's first attempt: one criteria call; a rubric on its component; difficulty at
  the rated band's midpoint; the event's `grading.rubric` names a rubric snapshot.
- A second attempt at the same item makes no criteria call.
- A deferral or a withdrawal makes no criteria call.
- An unusable reply: graded as before, no rubric, difficulty unchanged.
- Criteria with an unknown level: criteria stored, difficulty unchanged.
- The criteria call's messages never contain the learner's answer.
- A check that already has a rubric is left alone.
- The declared-check instruction carries the level sentence, and a weaker subject roll-up gives
  an easier band.
- `difficulty.midpoint` for each band; `band(midpoint(b)) == b`.

## Docs

Tracker S56: declared-check criteria and difficulty done, S56 closes; the Jev difficulty-band
shadow question noted as optional later work. `_materialise_declared_check`'s docstring stops
saying "No rubric". No RUNBOOK change (no new operator action).

## Out of scope

Measuring item difficulty from answers (calibration, S18); a Jev band question; criteria for
checks nobody answers; editing a rubric once written.
