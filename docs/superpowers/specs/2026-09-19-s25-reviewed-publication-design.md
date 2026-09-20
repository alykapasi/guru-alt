# S25b — Reviewed publication — Design

**Status:** Approved design (2026-09-19), amended 2026-09-20 with D8 (per-owner subject slugs),
carried over from S25a's review, which deferred it for needing a migration that slice forbade,
and with D4's flag moved off the client onto a server-side record. Feeds an implementation plan
under `docs/superpowers/plans/`. Third of three V0 workstream-1 slices. It depends on
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
  that is set whenever source material reaches the graph and is never cleared. Nothing in a
  request body can clear it, and no route offers an override: an override would be a way around
  V03.

  **The flag is never taken from the client.** The original design had the curriculum proposal
  carry a `grounded_in_sources` bit through the browser and back on commit, which is not a control
  at all — a bit the client is handed is a bit the client can drop. `/onboarding/curriculum`
  records what it actually did in a server-side row and returns that row's id; `/subjects/commit`
  requires the id and reads grounding from the row. See *Setting the source-derived flag*.

  **What this does not stop, stated plainly.** An author who deliberately requests a clean
  proposal and then pastes a source-derived curriculum in as their own edits evades the flag,
  because commit accepts an edited graph and no server-side comparison can tell a heavy edit from
  a substitution. Closing that would mean the server committing only the graph it generated, which
  removes the learner's review step — the wrong trade. So the flag is a guardrail against
  publishing source-derived material *by accident*, and the administrator's review of the full
  snapshot (D3) is the control that does not depend on the author's cooperation. The two are
  layered deliberately; neither is claimed to be the whole answer.
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
- **D8 — Subject slugs are unique per owner, not globally.** Carried here from S25a's review,
  which deferred it for needing a migration that slice forbade. Today `subjects.slug` is globally
  unique and `create_subject_with_graph` de-duplicates against *every* slug in the table, so naming
  a subject something another learner already has privately yields `name_2` — and that suffix
  answers "does a stranger have a subject by this name?" for any name the asker cares to try. It is
  a probe anyone can repeat: create, read the slug, delete. S25a closed listing and writing across
  the ownership boundary; this is the last reader of another learner's private material left in the
  graph API, and it belongs to this slice because `0056` is already reshaping `subjects`.

## Data — migration `0056_publication`

- `subjects.private_source_derived` (bool, not null, default false). The backfill sets it true for
  every row with a non-NULL `owner_learner_id` (D5).
- `subjects.publication_id` (FK publications, SET NULL): set on published copies.
- `subjects.superseded_by_id` (FK subjects, SET NULL).
- `subjects.withdrawn_at` (timestamptz, nullable) and `withdrawn_reason` (text, nullable).
- **Slug uniqueness moves from global to per-owner (D8).** Drop the unique index on
  `subjects.slug` and replace it with two constraints that together say what the old one meant to:
  - `UniqueConstraint("owner_learner_id", "slug")` — one learner's own subjects have distinct
    slugs. Postgres does not treat NULLs as equal, so this constrains *owned* rows only and says
    nothing about curated ones.
  - a partial unique index on `slug` `WHERE owner_learner_id IS NULL` — curated subjects, which
    are the shared library and are seen by everyone, stay unique among themselves. Without it the
    first constraint would let two curated subjects share a slug.

  No backfill and no collision risk: this only *loosens* uniqueness, so every existing row already
  satisfies both constraints. A non-unique index on `slug` stays for the ordering in
  `list_subjects`.
- `curriculum_proposals` (D4): `id`, `learner_id` (FK learners, CASCADE, indexed),
  `grounded_in_sources` (bool, not null), `created_at`, `updated_at`. CASCADE rather than SET NULL
  because this row is scaffolding for one learner's commit, not a record anybody audits later.
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

## Scoping the slug de-duplication

The constraint change is half the fix; the query that picks a slug is the half that leaks. In
`create_subject_with_graph`, `select(Subject.slug)` must gain the matching owner filter — the
caller's own rows when creating an owned subject, `owner_learner_id IS NULL` when creating a
curated one — so the suffix counts only subjects the caller can already see. This also stops the
function loading every slug in the table to create one subject.

Two traps for the implementer:

- Write the curated filter as `Subject.owner_learner_id.is_(None)`. `== None` compiles to
  `= NULL`, which is never true, and the filter would silently match no rows — a de-duplication
  loop that always thinks the name is free, and an `IntegrityError` on the second curated subject
  of the same name.
- `create_subject` (the direct path) takes `data.slug` from its caller and de-duplicates nothing.
  It is unreachable by learners today, but approval in this slice calls it with a curated slug, so
  approval owns picking one that does not collide.

The subject-name uniqueness the learner *experiences* changes with this: two learners may now both
have a subject slugged `calculus`. That is the point — it is what makes the slug stop reporting on
somebody else's library.

## Setting the source-derived flag

Every path that lets source material reach a subject sets
`private_source_derived = true`, in the same transaction:

1. **Onboarding curriculum generation that passed `materials` to `generate_curriculum`**, recorded
   server-side. `/onboarding/curriculum` writes a `curriculum_proposals` row — `learner_id`,
   `grounded_in_sources` (whether any excerpt was actually retrieved and sent to the model, not
   merely whether `source_ids` was non-empty), `created_at` — and returns its id alongside the
   proposal. `/subjects/commit` **requires** `proposal_id`, resolves it for the calling learner
   (anyone else's, or an unknown one, is the ordinary 404), and takes grounding from the row.
   Required rather than optional because an optional id is one the client can simply omit; the
   endpoint has exactly one caller, the onboarding commit, so requiring it costs nothing real.
   Rows CASCADE with the learner; they are small and are not swept at this scale.
2. Subject creation with `source_ids`, which reassigns sources. Independent of (1), so a commit
   that moves sources in is flagged whatever its proposal row says.
3. Source upload with a `subject_id`/`topic_id`, and any later source reassignment into the subject.

The flag is a latch: every trigger sets it, nothing clears it, and re-running a trigger on an
already-flagged subject is a no-op rather than an error.

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
  - a commit whose body claims nothing about grounding is still flagged when its proposal row
    says the generation used materials — the client cannot opt out by omission;
  - a commit carrying another learner's `proposal_id`, or an unknown one, is a 404;
  - a generation that passed `source_ids` but retrieved no excerpts records
    `grounded_in_sources = false`: no source text reached the model, so the subject is not
    source-derived and saying otherwise would make honest subjects unpublishable;
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

**Slug scope (D8):**

- A and C both create a subject named "Calculus". Both get the slug `calculus` — the second is not
  suffixed, which is the leak closed: C learns nothing about A from the name they were given.
- A creating a second "Calculus" of their own still gets `calculus_2`: per-owner uniqueness is
  still uniqueness.
- Two curated subjects cannot share a slug — the partial index refuses the second.
- The migration round-trips on data that the old global constraint permitted.

## Delivery

One commit per step, each tagged `[S25]`, in this order:

1. migration `0056` (including the slug constraint swap and `curriculum_proposals`), the
   source-derived flag at every trigger including the server-side proposal record, and the
   scoped slug de-duplication;
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
