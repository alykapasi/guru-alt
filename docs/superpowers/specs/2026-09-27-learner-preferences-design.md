# Explicit learner preferences (S02; V09)

**Status:** approved in conversation 2026-09-27; this document records it.
**Tracker:** S02 (owns the remaining preference work from S16/S44). Workstream 4, slice C.

## Problem

- Guidance (guided / exploration) exists only per lesson plan; there is no global default.
- The profile infers teaching parameters (hint density, pacing, preferred item type, target
  challenge, interests) and a note format, and the learner can reset an inference but cannot
  say what they want instead.
- Presentation level has no home: the reading-level inference was removed (S44) because it
  misfired, and the code records that it "belongs to an explicit learner preference, which does
  not exist yet".
- Inferred pacing is computed and stored on the plan but read by nothing.

V09: "Explicit learner settings win. Provide global defaults and subject overrides. Adapt within
the selected guidance level; inferred preferences remain inspectable and resettable."

## Decisions (from the design conversation)

1. **Scope (choice B):** five settings — guidance, explanation level, note format, hints, pace.
   Item type and challenge stay evidence-driven: pinning them invites the failure S44 warned
   about, practising only what already feels easy.
2. **A setting pins (choice A):** every setting defaults to "Adapt to me"; choosing a value
   pins that parameter, so inference stops steering it (it is still computed and shown).
   Clearing the setting hands it back.
3. **One preferences table, resolved in one function (approach 1).**

## Design

### Catalog — `app/learning/preferences.py`

Lives in code, like the profile's dimension catalog. Each key has fixed values and a default:

| Key | Values | Default | Pins |
| --- | --- | --- | --- |
| `guidance` | `guided`, `exploration` | `guided` | detour behaviour (V07); nothing infers it, so there is no `auto` |
| `explanation_level` | `auto`, `introductory`, `standard`, `advanced` | `auto` | how the tutor and lessons pitch explanations; `auto` gives no instruction (today's behaviour) |
| `note_format` | `auto`, `outline`, `narrative`, `mnemonic`, `worked_examples` | `auto` | the note format when the note has none of its own |
| `hints` | `auto`, `fewer`, `some`, `more` | `auto` | overrides the inferred hint density (`fewer`→`low`, `some`→`medium`, `more`→`high`) |
| `pace` | `auto`, `brisk`, `standard`, `unhurried` | `auto` | a pace instruction to the tutor |

Only these catalog values ever reach a prompt; nothing free-text.

### Data — migration 0068

- `learner_preferences`: `id uuid pk`, `learner_id` (FK, cascade), `subject_id` (FK to
  subjects, cascade; NULL = global), `key text`, `value text`, `created_at`, `updated_at`.
  Unique on `(learner_id, subject_id, key)` with NULLs not distinct (Postgres 15+
  `NULLS NOT DISTINCT`), so there is one global row per key.
- A row exists only for an explicit choice. Globally, the catalog default (`auto`, or `guided`
  for guidance) needs no row. A subject stores whatever it is given, the default included:
  "guided here" or "adapt to me here" while the global setting says otherwise is a real choice
  (amended after the final review — deleting on the default made it impossible, and per-subject
  guided worked before this slice). Clearing a level (`null`) deletes its row.
- Backfill: every `lesson_plans` row whose `guidance` is not `guided` becomes a subject
  override `(learner, subject, 'guidance', value)`. Plans on the default need no row: they
  resolve to `guided` either way. The old column cannot tell a plan explicitly switched back
  to guided from one never touched, so neither gets a row: such a subject follows a later
  global `exploration` default. Accepted — the toggle is recent and the learner can pin it.
- `lesson_plans.guidance` stays as a column in this slice but is no longer read.
- `RETENTION` gains `learner_preferences` (deleted, cascades from the learner); the export
  includes it.

### Resolution

`effective(session, learner_id, subject_id: uuid | None) -> dict[str, Resolved]`, where
`Resolved(value, source)` and `source` is `subject | global | default`. Order: subject override
→ global → default. One query, no model calls. With no subject: global → default.

A stored row whose key or value is no longer in the catalog is ignored during resolution, never
an error. A failed read in a turn falls back to the defaults and is logged; preferences never
fail a turn.

### Where each preference takes effect

Applied when instructions are assembled, so a change works on the next turn or generation with
no plan rebuild — except inside a guided-practice question, whose prompt is fixed when the
question starts; there the change applies from the next question.

- **Tutor, all three modes** (`app/services/learner_context.py`): `gather` reads
  `effective(learner, conversation.subject_id)`; `LearnerContext` carries it. `compose` adds a
  settings line after the plan focus with the pinned explanation level and pace
  instructions. A pinned `hints` replaces the plan step's inferred "Hint density" in
  `plan_note`; on `auto` the inferred value stands exactly as today. Pace on `auto` adds
  nothing — the inferred pacing has never reached the tutor and wiring an unvalidated inference
  is not this slice's job; it stays visible on the Dashboard.
- **Lessons** (`app/services/content.py` `generate_block`): a pinned explanation level joins the
  system prompt for the KC's subject. The cache key already covers the rendered system prompt,
  so a block cached at a different level is never served.
- **Notes** (`app/services/notes.py`): the format order becomes the note's own format →
  `note_format` preference → inferred `note_format` → heuristic → `outline`. A per-note choice
  still beats a general preference.
- **Guidance:** detour decisions read the effective guidance for the plan's subject.
  `PATCH /subjects/{id}/lesson-plan/guidance` becomes a thin alias that writes the subject
  override; `LessonPlanRead.guidance` reports the effective value.
- **Adapt within the guidance level (V09):** detours are the only adaptation guidance governs,
  and they already follow it; nothing new.

### API — `app/api/v1/preferences.py`

`CurrentLearner` routes, so a pending-deletion account is refused like any learner route.

- `GET /preferences?subject_id=` → every catalog key: `key`, `value`, `source`, `options`,
  `global_value` (the global/default value, when reading a subject), and `inferred` (the
  profile's current value for `hints`, `pace`, `note_format`, else null).
- `PUT /preferences/{key}` body `{value, subject_id?}` → the same entry for that key. `value:
  null` clears that level; "Use my default" sends it.
- 422: unknown key, or a value outside that key's options. 404: a subject the learner cannot see
  (`is_visible_to`; curated subjects are allowed). An administrator's visit is sudo, not
  read-only (workstream 1): it may read and write preferences like any learner route, and the
  write is recorded by the existing admin-action audit.

### Frontend

- **Account page:** a "Preferences" section — one control per key for the global defaults,
  each with "Adapt to me" and the inferred value in muted text where there is one.
- **Lesson plan panel:** the guided/exploration toggle becomes "Settings for this subject" with
  the same five controls; each shows "Using your default (X)" until overridden, then offers
  "Use my default".
- **Dashboard profile section:** a dimension overridden by a setting says "Your setting
  overrides this". Resetting an inferred value is unchanged.

## Testing

- Migration: a non-default plan guidance becomes a subject override; a default plan resolves
  to guided.
- Resolution: subject over global over default; a subject can pin the default against a
  different global; `null` clears a level; unknown keys/values ignored.
- API: 422 cases; 404 for another learner's private subject; a write during an admin visit
  audited; visibility sweep coverage for `subject_id`.
- Consumers: the composed tutor prompt carries a pinned level and pace; pinned hints replace
  the inferred density and `auto` leaves it; a lesson prompt carries the level and its cache
  key changes; the note format order including a note's own format beating the preference;
  detours follow the subject override and fall back to global; the old guidance endpoint
  writes the override and the plan reports the effective value.
- Retention: preferences exported, deleted with the account.
- Frontend (vitest): Account preferences; subject override with "Use my default"; the
  Dashboard override label.

## Out of scope

Explicit settings for item type and challenge; wiring inferred pacing into the tutor; a
free-text "anything else about how you like to learn" field; removing the
`lesson_plans.guidance` column (a later cleanup once nothing reads it).

## Tracker updates on completion

- S02 → **Implemented**: global defaults and subject overrides for guidance, explanation level,
  note format, hints and pace; explicit settings pin their parameter; inferred values stay
  visible and resettable.
