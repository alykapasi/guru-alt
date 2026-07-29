# Phase 8 — Notes: the durable, personalized learning artifact (design spec)

**Date:** 2026-07-27 · **Status:** approved in brainstorming, pending spec review
**Governs:** ROADMAP Phase 8 · MASTERPLAN §4.9

## 0. Decisions made (brainstorming summary)

| Fork | Decision |
|---|---|
| Ownership | **Living summary, system re-merges** — Guru authors and keeps restructuring the whole note; learner edits are folded into future rewrites, never fenced off into frozen blocks. |
| Merge safety | **Revision history + restore** — every change appends a revision; nothing is ever unrecoverable. Plus a structural invariant protecting learner-contributed atoms (§4.3). |
| Distillation trigger | **Catch-up on read** — a per-note watermark; one merge covers a whole study burst, triggered when the learner opens their notes. No background jobs, no derived-session machinery. |
| Note content | **Covered material + struggle callouts** — the distiller sees both study transcripts and the learner's graded outcomes, producing "you specifically confused X with Y" callouts. |
| Format architecture | **Meta-wiki substrate + projection** — a format-neutral substrate holds *what* the learner covered; the formatted note is a cheap *projection* of it. Format selection: learner control → learned profile dimension → heuristic defaults. The system learns each learner's best format from real usage. |

## 1. Product intent

Notes are the primary long-term learning artifact: per-learner, per-topic, KC-tagged,
cumulative. They grow **unprompted** as the learner studies — a session on linear algebra grows
that topic's notes without anyone asking — and their *form* adapts to how that learner best
consumes written material.

**DoD (from ROADMAP):** studying a topic across multiple sessions produces a growing, readable
set of notes in a format suited to that learner, browsable/editable in a dedicated notes
section, without needing to explicitly ask for them.

**Distinctness guardrails** (do not blur these):

- `Note` — per-learner *learned content*, distilled from that learner's own study.
- `ContentBlock` — shared/cached generated content, reusable **across** learners.
- `Memory` — facts *about* the learner (preferences, context), not learned content.

## 2. Core concept: substrate and projection

The load-bearing idea (the "meta-wiki"): **separate the note's content from its form.**

- The **substrate** is a format-neutral, KC-tagged collection of *atoms* — the facts, concept
  explanations, worked examples, struggle callouts, and learner-contributed content for one
  `(learner, topic)`. Distillation and learner-edit absorption update the substrate. It is the
  single source of truth and the thing revision history protects.
- The **rendered note** is a projection of the substrate into one of four named formats,
  produced by an LLM render pass and cached. Switching formats re-renders; it never
  re-distills. Format experimentation is therefore free — which is exactly what generates the
  honest usage data the format-learning loop (§6) needs.

This mirrors the masterplan's mastery-model / learner-profile split: *what you know* and *how
you like it presented* are separate concerns with separate machinery.

## 3. Domain model

Three new tables (Alembic migration `0017_notes`), all UUID-PK + timestamp mixins like every
other model.

### 3.1 `Note` — one per (learner, topic)

| Column | Type | Notes |
|---|---|---|
| `learner_id` | FK → learners, CASCADE | |
| `topic_id` | FK → topics, CASCADE | `UniqueConstraint(learner_id, topic_id)` |
| `substrate` | JSONB | current atom list (§3.3) |
| `watermark` | timestamp (naive, UTC) | last-distilled activity cutoff; see the `LearningEvent.created_at` tz-naive precedent — comparisons strip tzinfo, documented inline |
| `format` | text, nullable | explicit learner choice (`outline` \| `narrative` \| `mnemonic` \| `worked_examples`); `NULL` = auto (cascade, §6.1) |
| `revision_ordinal` | int | ordinal of the current revision |

### 3.2 `NoteRevision` — append-only history

| Column | Type | Notes |
|---|---|---|
| `note_id` | FK → notes, CASCADE | |
| `ordinal` | int | `UniqueConstraint(note_id, ordinal)`, monotonically increasing |
| `substrate` | JSONB | full snapshot at this revision |
| `cause` | text | `distill` \| `learner_edit` \| `restore` |

Retention: keep all revisions (substrates are small text). No pruning in v1.

### 3.3 Atom schema (inside `substrate`)

```json
{
  "id": "a-3f2c",            // stable short id, assigned server-side on first appearance
  "kind": "concept",          // concept | example | callout | learner
  "kc_ids": ["<uuid>", ...],  // KCs this atom is about (may be empty for topic-level framing)
  "md": "…markdown content…",
  "provenance": {"conversation_id": "<uuid|null>", "item_id": "<uuid|null>"}
}
```

- `concept` — distilled explanation of covered material.
- `example` — a worked example, ideally one the learner actually worked through.
- `callout` — struggle-derived warning ("you answered X when the answer was Y — the
  distinction is …"), built from this learner's wrong answers / hint usage / `error_type`.
- `learner` — content the learner contributed via edits. **Protected** (§4.3).

### 3.4 `NoteRender` — projection cache

| Column | Type | Notes |
|---|---|---|
| `note_id` | FK → notes, CASCADE | |
| `revision_ordinal` | int | |
| `format` | text | `UniqueConstraint(note_id, revision_ordinal, format)` |
| `content_md` | text | the rendered note |

On a new revision, renders for older revisions of that note are deleted (only current-revision
renders are cached; historical revisions get the mechanical view, §7).

## 4. Distillation pipeline (catch-up on read)

### 4.1 Staleness check (cheap, no LLM)

A note is **stale** when, after its `watermark`, there exists either:

- a `LearningEvent` on any of the topic's KCs (graded answers, hints — already KC-tagged and
  indexed), or
- a `Message` in a conversation scoped to the topic's subject (`Conversation.subject_id`),

**or** when the current revision has no cached render for the effective format (the
render-failure recovery case, §11 — `refresh` then performs a render-only pass, no distill).

The subject-level message check can false-positive for sibling topics (a conversation about
Topic A marks Topic B stale). Accepted: the distiller handles it via the no-change path
(§4.4) so at most one wasted call per burst, and precision improves later if needed
(citation-chunk → `ChunkKC` attribution exists but is not required for v1).

### 4.2 Gather (on refresh)

Input to the distiller, all capped by config:

- **Transcript excerpts**: messages after `watermark` from subject-scoped conversations
  (both `kind=chat` and `kind=session`), capped at `note_distill_max_messages` (default 150).
- **Graded outcomes**: the learner's `LearningEvent`s on the topic's KCs after `watermark` —
  correctness, scores, hint counts — plus the stems/answers of items involved, so callouts can
  name the actual confusion.
- **Current substrate**: the full atom list.
- **Profile hints**: `reading_level` (register), `error_type` (which callouts to emphasize).

### 4.3 Distill (SMART role)

One completion call: *given the current atoms, the new material, and the learner's outcomes,
return the updated complete atom list as JSON* — restructure freely for coherence, fold in new
material, add/update callouts, carry every `learner` atom forward (content may be lightly
edited for flow, never dropped).

- Tolerant parsing per the `curriculum.py` idiom: extract JSON, validate shape, return
  `None` on failure → **keep the old substrate, do not advance the watermark** (the next
  refresh retries).
- **Structural invariant (the merge-safety guarantee):** every atom id with
  `kind: "learner"` present in the input MUST be present in the output. If any is missing,
  the merge is rejected (old substrate kept, failure logged). This is enforced in code, not
  just prompted — and it is the key regression test of the phase.
- The distiller may return `{"no_change": true}` when the new activity contains nothing for
  this topic (the sibling-topic case): **advance the watermark, create no revision.**

On success: new `NoteRevision` (`cause=distill`), `substrate` + `revision_ordinal` +
`watermark` updated, then render (§6.2).

### 4.4 Read flow (API-level)

`GET` is pure — it returns the current render (or `content_md: null` for a never-distilled
note) plus the `stale` flag. The **frontend** then calls `POST …/refresh`, which runs
gather → distill → render synchronously (seconds) and returns the fresh note. This avoids
repeating the reviews-due "GET with generation side-effects" compromise. The UI shows the
current note instantly with an "updating your notes…" banner, then swaps in the result.

## 5. Learner edits (absorb)

The learner edits the **rendered markdown** (the only surface they see). `PUT` sends the full
edited markdown. The **absorb** step (SMART role, one call): *given the current atoms, the
previous render they started from, and their edited version, return the updated atom list* —
new content they added becomes `learner` atoms; changes/deletions to existing content update
or remove the corresponding atoms (learner intent wins over machine content).

- Same tolerant-parse + learner-atom invariant as distill — with the nuance that an edit may
  legitimately *delete* a learner atom (the learner removing their own earlier note); the
  invariant therefore protects learner atoms in **distill** merges only; absorb trusts the
  learner's edit as the authority.
- On success: new revision (`cause=learner_edit`) + re-render.
- On failure: error response, note unchanged; the frontend keeps the learner's text in the
  editor so nothing is lost client-side.

## 6. Formats and the learning loop

### 6.1 Selection cascade

Effective format for a note, first match wins:

1. **Per-note explicit choice** (`Note.format` non-null).
2. **Learned dimension**: `note_format` in the profile (below), if confident.
3. **Heuristic default** from existing real dimensions: `reading_level` sets register/
   complexity; high conceptual `error_type` share or strong `format_effectiveness` for
   applied item types biases toward `worked_examples`; otherwise —
4. **Fallback:** `outline`.

Four v1 formats: `outline` (dense structured bullets), `narrative` (flowing prose),
`mnemonic` (memory-hook-heavy), `worked_examples` (example-led).

### 6.2 Render (SMART role)

One completion call: atoms + effective format + `reading_level` hint → coherent markdown
(not a mechanical atom dump — the renderer writes an actual document; atoms are its source
material, ordered/grouped as the format demands). Result cached in `NoteRender`. Format
switch = cache hit or one render call; never a distill.

Render uses **SMART, not FAST** — the rendered note *is* the product; prose quality is the
point. (Phase 9 sweeps can revisit.)

### 6.3 Learning the learner's format — `note_format` profile dimension

A new entry in `DIMENSION_SPECS` (`app/learning/profile_estimators.py`) — code change only,
zero migration, exactly what the EAV profile design was built for. The v1 estimator is
deliberately simple and honest: read the learner's **current explicit `Note.format` choices
across all their notes**; with ≥ 3 explicit choices and a majority format, emit that format —
value = the majority format, confidence = its share of the explicit choices, evidence count =
the number of explicit choices; otherwise emit nothing (cascade falls through to heuristics). No fake inference from zero data — the format toggle itself is the capture
surface that earns the dimension. A switch-event log is deferred until an estimator actually
needs switch *history* rather than settled state.

## 7. Revision history and restore

- `GET …/note/revisions` — list `{ordinal, cause, created_at}`.
- `GET …/note/revisions/{ordinal}` — the revision's substrate rendered **mechanically**
  (deterministic markdown: atoms grouped by kind, no LLM call) — labeled a source view.
  Free, honest, sufficient for "what did I lose?".
- `POST …/note/revisions/{ordinal}/restore` — copies that revision's substrate forward as a
  **new** revision (`cause=restore`; history is never rewritten), then re-renders.

## 8. API surface (all under `/api/v1`, learner-scoped via `CurrentLearner`)

| Endpoint | Behavior |
|---|---|
| `GET /subjects/{subject_id}/notes` | Index: `[{topic_id, topic_name, has_note, stale, updated_at}]` |
| `GET /topics/{topic_id}/note` | Pure read: `{content_md \| null, format, effective_format, stale, revision_ordinal, updated_at}` |
| `POST /topics/{topic_id}/note/refresh` | Catch-up distill + render (creates the note on first call); returns the GET shape |
| `PUT /topics/{topic_id}/note` | Absorb learner edit `{content_md}`; returns the GET shape |
| `PATCH /topics/{topic_id}/note/format` | `{format: str \| null}` (null = back to auto); re-render; returns the GET shape |
| `GET /topics/{topic_id}/note/revisions` | Revision list |
| `GET /topics/{topic_id}/note/revisions/{ordinal}` | Mechanical source view of that revision |
| `POST /topics/{topic_id}/note/revisions/{ordinal}/restore` | Restore-as-new-revision |

Module layout mirrors existing patterns: `app/models/note.py`, `app/learning/note_distill.py`
(prompts + parsing + invariant — the policy layer, `curriculum.py`-shaped),
`app/services/notes.py` (orchestration: staleness, gather, transactions),
`app/api/v1/notes.py`, schemas in `app/schemas/note.py`.

## 9. Frontend

New top-nav section **Notes** (`/app/notes`):

- **Index**: subject picker (reuse `SubjectPicker`) → topic list with stale badges and
  last-updated.
- **Note page**: rendered markdown (add `react-markdown` — first real markdown surface in the
  app), format switcher (four formats + "Auto", DaisyUI select), **Edit** mode (plain
  markdown textarea v1, Save → `PUT`, Cancel), **History** drawer (revision list → source
  view → restore with confirm), stale → "updating your notes…" banner while `refresh` runs,
  then swap-in.

Desktop-only, consistent with the Phase 7 scope.

## 10. Roles, config, cost

- All three LLM steps (distill, absorb, render) go through the role registry as **SMART** —
  never a provider SDK, never a model name (CLAUDE.md rule). Phase 9's experiment suite can
  sweep these choices later precisely because they're role-keyed.
- New settings (`GURU_` env pattern): `note_distill_max_messages` (default 150),
  `note_distill_max_outcome_events` (default 50).
- Cost shape: at most one distill per topic per study burst (watermark), one render per
  (revision, format) (cached), one absorb per learner save. No per-turn calls anywhere.

## 11. Error handling

| Failure | Behavior |
|---|---|
| Distill parse failure / invariant violation | Keep old substrate, watermark NOT advanced, `refresh` returns the old note + `stale: true`; retried on next refresh |
| Distill no-change | Watermark advanced, no revision, old render returned |
| Absorb failure | Error response; note unchanged; frontend keeps the editor content |
| Render failure | Substrate/revision already committed; return error; next read retries render only (substrate is never held hostage by a render) |
| Restore of nonexistent ordinal | 404 |

## 12. Testing

- **Unit (FakeProvider, canned JSON atom lists):** staleness math (both triggers; watermark
  respected); no-change path advances watermark without a revision; **learner-atom
  invariant** — a distill output missing a `learner` atom id is rejected and the old
  substrate survives (the phase's key regression test); absorb creates `learner` atoms from
  additions and may delete learner atoms; cascade resolution (explicit > learned > heuristic >
  `outline`); `note_format` estimator thresholds; render caching (format switch = no distill).
- **API integration:** first `refresh` creates note + revision 1; edit → revision 2
  (`learner_edit`); restore → revision 3 (`restore`) with revision-1 content; format PATCH
  re-renders without distilling; GET is side-effect-free.
- **Live (skippable, Ollama):** one end-to-end distill → render smoke, mirroring the existing
  live-test pattern.
- Transactional-rollback DB fixtures as everywhere; no DB mocking.

## 13. Explicitly out of scope (v1)

- Per-KC note pages (topic granularity only; KC tags enable it later).
- Notes as a retrieval/RAG source (interesting later: your own notes grounding chat).
- Embedded practice elements inside notes (study aids + FSRS already own that loop).
- Format switch-event history log (settled state suffices for the v1 estimator).
- Export/download, sharing, collaborative notes.
- Precision staleness via citation-chunk KC attribution (accepted false-positive + no-change
  path instead).
