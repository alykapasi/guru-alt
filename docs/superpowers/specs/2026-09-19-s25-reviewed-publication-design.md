# S25b — Reviewed publication — Design

**Status:** Approved design (2026-09-19). Feeds an implementation plan under
`docs/superpowers/plans/`. Third of three V0 workstream-1 slices. It depends on
[S25a visibility](2026-09-19-s25-visibility-sweep-design.md) for the boundary it publishes across,
and uses the admin tier as it stands after [S21](2026-09-19-s21-clerk-identity-design.md).

## Context and goal

V03: learner-generated curricula, graphs, questions and teaching material are private by default.
Sharing requires an explicit, reviewed publication path, and **private-source-derived material
stays private**. S33 made items and rubrics private and handed "assessment publication" to this
row. Today a subject is either the learner's own (`owner_learner_id`) or curated (NULL). Nothing
moves a curriculum from the first state to the second, so learners have no way to share one.

The goal: a learner requests publication, an administrator reviews exactly what would ship, and
approval creates an immutable shared copy. Nothing private travels with it.

## Decisions

- **D1 — Publish a snapshot, never the original.** The author keeps a private subject they can go
  on editing. Learners studying the published copy never see its graph change underneath them.
- **D2 — Curriculum + items.** The snapshot carries the subject's topics, KCs and within-subject
  prerequisite edges, plus the author's own items and rubrics on those KCs. It never carries:
  - learner evidence, mastery, FSRS state or lesson plans;
  - notes, content blocks, sources, chunks, conversations or memories;
  - edges to KCs in other subjects.
- **D3 — Review what ships.** The snapshot is frozen as JSON when the request is made. The reviewer
  sees that JSON, including answer keys, and approval materializes that JSON, not the subject's
  state at approval time.
- **D4 — Private-source-derived subjects cannot be published.** This is tracked by a sticky flag
  that is set whenever source material reaches the graph and is never cleared. There is no
  override: an override would be a way around V03.
- **D5 — Existing subjects are treated as source-derived.** Nothing recorded whether they were
  generated from uploads. Guessing in the permissive direction is what V03 forbids, so the backfill
  sets the flag on every learner-owned subject. To publish one, recreate it without uploads.
  Curated subjects (NULL owner) are unaffected.
- **D6 — Author anonymity.** Other learners are not shown who authored a published subject. The
  author is recorded for the admin audit. Showing credit is a possible later choice.
- **D7 — Versions supersede; withdrawal unlists.** Re-publication creates a new shared subject and
  unlists the previous one. Withdrawal also unlists. Neither removes access for learners who
  already have a lesson plan on the subject: it was reviewed as shareable, so reaching it by id is
  not a privacy leak.

## Data — migration `0056_publication`

- `subjects.private_source_derived` (bool, not null, default false). The backfill sets it true for
  every row with a non-NULL `owner_learner_id` (D5).
- `subjects.publication_id` (FK publications, SET NULL): set on published copies.
- `subjects.superseded_by_id` (FK subjects, SET NULL).
- `subjects.withdrawn_at` (timestamptz, nullable) and `withdrawn_reason` (text, nullable).
- `publications`:
  - `id`, `created_at`;
  - `source_subject_id` (FK subjects, SET NULL);
  - `author_id` (FK learners, SET NULL) and `author_handle` (text);
  - `status` ∈ `pending`, `approved`, `rejected`, `cancelled`;
  - `author_note` (nullable), `snapshot` (JSONB);
  - `reviewer_id` (SET NULL), `reviewer_handle`, `reviewed_at`, `review_note`;
  - `excluded_item_ids` (JSONB list);
  - `published_subject_id` (FK subjects, SET NULL).
  - A partial unique index allows one `pending` publication per `source_subject_id`.

The foreign keys are SET NULL, and the handles are kept as text, so the record outlives the
accounts and subjects involved, like `impersonations`.

## Setting the source-derived flag

Every path that lets source material reach a subject sets
`private_source_derived = true`, in the same transaction:

1. Onboarding curriculum generation that passed `materials` (source excerpts) to
   `generate_curriculum`. The proposal carries a `grounded_in_sources` bit through to subject
   creation.
2. Subject creation with `source_ids`, which reassigns sources.
3. Source upload with a `subject_id`/`topic_id`, and any later source reassignment into the subject.

Audit at `78e4b60`: every topic, KC and edge row is written through `app/services/knowledge.py`,
and the only generation feeding it source excerpts is (1). Item generation
(`app/learning/item_generation.py`) takes no source material, so published items carry none. A
future generator that grounds graph or item writes in chunks must set the flag. Each trigger above
gets its own test.

## Flow and API

**Author** (owner only: S25a's gate plus `is_writable_by`; anything else is a 404):

- `POST /subjects/{id}/publications {note?}` → 201 `pending`, with the snapshot frozen.
  - 422 `"This subject was built from your uploaded material, which stays private."` when the flag
    is set.
  - 409 when a request is already pending.
  - 422 when the subject has no KCs.
- `GET /subjects/{id}/publications` returns the history: status, review note, and a link to the
  published subject if approved.
- `POST /publications/{id}/cancel` → `cancelled` (author only, pending only).

**Snapshot shape:**

- `{subject: {name, description}, topics: [...], kcs: [...], edges: [...], items: [...], rubrics: [...]}`
- Each entry is keyed by the source id, so the reviewer can exclude items by id.
- Items: the author's own rows (`owner_learner_id == author`) tagged only to this subject's KCs,
  with stem, type, answer key, requested difficulty, rubric reference and KC weights.

**Administrator** (`CurrentAdmin`; impersonated and suspended sessions are refused):

- `GET /admin/publications?status=pending` and `GET /admin/publications/{id}` (the full snapshot).
- `POST /admin/publications/{id}/approve {excluded_item_ids?, note?}`. In one transaction:
  - create a Subject with NULL owner (curated), a unique slug from the name plus a short suffix,
    and `publication_id`;
  - create its topics and KCs through the ordinary creation paths, so concept identity follows the
    existing rules (S24 owns any change), and its edges;
  - create items and rubrics as `CURATED` with a NULL owner, preserving `origin` and
    `author_learner_id` and remapping KC links;
  - if the author has a previously published version, set its `superseded_by_id`;
  - mark the publication `approved`.
- `POST /admin/publications/{id}/reject {note}` requires a note of at least 8 characters.
- `POST /admin/subjects/{id}/withdraw {reason}` works on published subjects only; the reason is
  required.

**Catalog:** `list_subjects(learner)` returns:

- the learner's own subjects;
- curated subjects that are neither superseded nor withdrawn;
- superseded or withdrawn curated subjects on which the learner has a lesson plan.

`is_visible_to` is unchanged: curated remains visible by id.

**Frontend:**

- The author gets a "Publish" action with status and review note on their subject. It shows the
  source-derived reason when the action is unavailable.
- The admin portal gets a Publications queue: an outline of the graph, items with keys and
  per-item exclusion, and approve/reject with a note. Published subjects have a withdraw action.

## Verification

**Through the real API, with three learners (author A, reviewer admin R, learner C):**

- Only A can request. B and C get the S25a 404, which is identical to a random id.
- The snapshot contains only A's subject, A's items on its KCs, and within-subject edges. It
  contains nothing from another learner (seeded decoys on the same KC names), and none of A's
  evidence, notes, content blocks, sources or cross-subject edges.
- Refusals:
  - the source-derived flag is refused, and each trigger path in *Setting the source-derived flag*
    sets it;
  - backfilled legacy subjects are refused;
  - a second pending request → 409.
- Approval:
  - C sees the published subject in the catalog and can create a lesson plan and practise.
  - Curated items are served, and public item schemas still withhold answer keys.
  - Excluded items are absent.
  - A's later edits to the original do not reach the copy.
- Republishing supersedes and unlists v1. C, who has a plan on v1, still reaches it; a new learner
  does not see v1 in the catalog.
- Withdrawal unlists with the same rule as superseding.
- Rejection requires a note. The author sees status and note.
- Non-admin and impersonated admin → 403 on every admin route.
- Migration round-trip with data: the backfill marks existing owned subjects and leaves curated
  ones.

## Delivery

One commit per step, each tagged `[S25]`, in this order:

1. migration `0056` and the source-derived flag at every trigger;
2. author request, snapshot and cancel;
3. admin review and approval materialization;
4. supersede, withdraw and catalog;
5. frontend;
6. tracker and docs.

Gates at every commit: `poe check`, `poe format-check`, `poe api-contract`, `poe db-check`, and
the frontend build, lint and vitest.

## What this does not establish

That published curricula or items are pedagogically sound: the review is a human judgment at
alpha scale, and item quality evaluation belongs to S59. That published items have calibrated
difficulty: the stored value is the requested difficulty (S18). Or that learners want to share at
all (S65/S66).
