# S25a — Visibility on every id-taking caller — Design

**Status:** Approved design (2026-09-19). Feeds an implementation plan under
`docs/superpowers/plans/`. First of three V0 workstream-1 slices; followed by
[S21 Clerk identity](2026-09-19-s21-clerk-identity-design.md) and
[S25b reviewed publication](2026-09-19-s25-reviewed-publication-design.md), which depends on this.

## Context and goal

V03 makes learner curricula private by default. `Subject.owner_learner_id` draws the line (NULL is
curated/shared, a learner id is private) and `knowledge.is_visible_to` / `is_writable_by` express
it, but several callers accept a subject, topic or KC id and never ask. The tracker's S25 row names
two; the audit below found more. The goal is that **no route or service entry point accepts a graph
id the caller cannot see**, provably, and that the property survives new routes.

Out of scope: publication (S25b), source-scope policy for generation (S26), identity (S21).

## Audit (repository at `78e4b60`)

Every OpenAPI operation with a `subject_id`, `topic_id`, `kc_id`, `prereq_kc_id` or `item_id`, in
the path, the query, or anywhere in the request body (34 operations), was classified.

**Confirmed gaps:**

| Operation | Current check | Consequence |
| --- | --- | --- |
| `POST /subjects/{id}/lesson-plan` | existence only | Plans over another learner's private graph; the plan names its KCs |
| `GET /subjects/{id}/lesson-plan` | existence only | 404 wording distinguishes "no subject" from "no plan": an existence oracle |
| `GET /subjects/{id}/mastery` | existence only | Returns another learner's private graph structure |
| `POST /content/generate` | none | Generates teaching material about another learner's private KC |
| `GET /content/kc/{kc_id}` | none (filters to own blocks) | No content leak, but inconsistent: foreign id answers 200 `[]` |
| `POST /sources/upload` with `subject_id`/`topic_id` | `resolve_source_scope` checks consistency, not visibility | Attaches the caller's source to a foreign private topic, triggering a retag against its KCs; the `ScopeConflict` text names a foreign topic's subject |
| `POST /retrieve` with `subject_id`/`topic_id` | filters own chunks | No content leak; foreign id answers 200 `[]` |
| `GET /sources?subject_id=` | filters own sources | Same as above |

**Already gated (to be proven, not assumed):** the knowledge graph reads/writes
(`_writable_subject_of_topic/_kc`, `is_visible_to`), `POST /items` (`_kcs_authorized`),
`GET|POST /items/{id}` (`get_item_for`), placement, `POST /conversations`, and the note routes
(service-level `notes._require_visible_topic`).

## Design

### One gate

`app/services/knowledge.py` gains three resolvers, the only way a request-derived graph id becomes
a row:

```python
async def require_visible_subject(session, subject_id, learner_id) -> Subject
async def require_visible_topic(session, topic_id, learner_id) -> tuple[Subject, Topic]
async def require_visible_kc(session, kc_id, learner_id) -> tuple[Subject, KC]
```

Each resolves up to the owning subject and applies `is_visible_to`. A missing row and a foreign
private row raise the **same** `NotVisible` exception, which routes map to one 404 body per kind
(`"subject not found"`, `"topic not found"`, `"kc not found"`). There is no second message that
could distinguish "exists but not yours".

Existing per-route helpers (`_writable_subject_of_*`, `notes._require_visible_topic`) keep their
names but are re-expressed on top of these resolvers, so there is one definition of visibility.

### Applying it

- Lesson plan (both routes), subject mastery, both content routes: call the resolver before any
  work. The lesson-plan GET's "no plan yet" 404 is only reachable after visibility passes.
- `resolve_source_scope` takes `learner_id` and resolves through the gate; a foreign or missing
  subject/topic raises `ScopeConflict` with text naming **only the ids the caller supplied**.
  Every caller (upload, subject creation's source reassignment, any reassign path) passes the
  learner.
- `POST /retrieve` and `GET /sources` validate a supplied `subject_id`/`topic_id` through the gate
  (404) instead of silently filtering to nothing.

No schema change, no migration, no new configuration.

## Verification

**Cross-learner table test (`tests/test_visibility_sweep.py`), through the real API.**
Learner A owns a private subject/topic/KC (and an item, via the existing fixtures). Learner B is
signed in with `conftest.sign_in`. One parametrized case per operation in the inventory calls the
route as B with A's private id and asserts:

1. the status and JSON body equal those returned for a freshly generated random UUID;
2. nothing was created: no lesson plan, content block, source, or chunk-tag row attributable to B
   referencing A's graph (checked by direct queries after the call);
3. for write routes, A's graph is unchanged.

Each case also calls the same route as A to prove the fixture reaches the handler (a test that
404s for everyone proves nothing).

**Drift guard (`test_every_graph_id_route_is_covered`).** Walks `app.openapi()`, including nested
request-body schemas via `$ref`/arrays/`anyOf`, collecting every operation with a parameter or
field named `subject_id`, `topic_id`, `kc_id`, `prereq_kc_id` or `item_id`. Fails naming any
operation missing from the cross-learner table. A new route cannot skip the check silently.

**Scrubbed errors.** A test uploads with A's topic id and asserts the response body contains no id
other than the ones B sent.

## Delivery

One commit per route family, each tagged `[S25]`: the gate + resolver refactor; lesson plan and
analytics; content; source scope, retrieval and listing; the table test + drift guard (landing with
the first family and extended by each). `poe check`, `poe format-check` and `poe api-contract`
green at each commit (no contract change expected beyond error text).

## What this does not establish

That generation respects *source* scope (S26), or that curated subjects are appropriate to share
(S25b's review). It establishes that graph ids are visibility-checked at every current API entry
point, and that future ones must be.
