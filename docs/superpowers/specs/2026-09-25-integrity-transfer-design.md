# Integrity and transfer: concurrent writes, graph conflicts, confirmed cross-subject links

**Slice 4 of 4, V0 workstream 2.** Tracker items: **S34** (concurrency-safe evidence and retries),
**S23** (graph integrity under concurrent edits), **S24** (confirmed cross-subject concept
transfer). Accepted decision: **V04 — Transfer** in `docs/V0_DECISIONS.md`.

Builds on slices 1–3 on branch `feat/evidence-kinds` (unmerged). Slice 2's mastery rule
(`ability − 2·uncertainty ≥ mastery_conservative_bar` with measured evidence) and slice 3's
detour machinery (guidance, proposal/accept/skip, outcome events, the pass-run check) are hard
dependencies.

---

## 1. What this slice decides

V04: *Link genuinely equivalent concepts across subjects, preserve the context of the original
evidence, and use a short confirmation when the new application demands it. Names alone do not
establish equivalence or merge estimates.*

Today:

- **S34.** `mastery._get_or_create_state` reads a `LearnerKCState` row without a lock, updates it
  in Python and writes it back. Two *different* attempts on the same component in flight at once
  both read the same prior, and the second write erases the first. Duplicate submissions of *one*
  attempt are already safe (unique `attempt_id` index); distinct attempts are not. Chat and
  workflow grading pass no `attempt_id`, so a retried turn is not deduplicated at the evidence
  layer.
- **S23.** `POST /kcs/{id}/prerequisites` runs `would_create_cycle` and then inserts, with nothing
  holding the two together. A→B and B→A submitted together both pass the check and both commit —
  a cycle. Cycle reports (`GET /subjects/{id}/prerequisite-conflicts`) and cross-subject prerequisite
  reports exist in the API but no screen shows them.
- **S24.** A `Concept` row groups components whose names normalize to the same key. It is
  reported (`met_elsewhere`) and never acted on. A plan drops every prerequisite that lives in
  another subject.

**Out of scope:** finding equivalents whose names differ (a candidate needs a shared concept key);
traversing a foreign prerequisite's own prerequisites; transfer between two learners' private
material (never — a pair only ever spans subjects one learner can see); retroactive re-seeding
when a source is measured after the link was accepted.

## 2. S34 — concurrency-safe evidence

### 2.1 Row lock

`_get_or_create_state` reads the state row `FOR UPDATE`. Its insert-on-conflict path is unchanged,
then the re-read also takes the lock. A second writer on the same (learner, KC) blocks until the
first commits and then reads the committed row.

`record_observation` iterates `obs.kc_weights` **sorted by KC id**, so two multi-KC observations
always take their locks in the same order and cannot deadlock. The same ordering applies to any
other path that updates more than one state row in one transaction (`seed_prior` callers, the
transfer seed in §5).

### 2.2 Attempt identity across retries

Chat and workflow turns already carry `client_turn_id` (S51). Every observation graded during a
turn with a `client_turn_id` gets
`attempt_id = uuid5(client_turn_id, "attempt")` — one id per turn's answer, shared by its per-KC
fan-out exactly as `record_observation` already does for a caller-supplied id. A retried turn then
hits the existing unique index and the existing replay path instead of recording a second answer.
A turn without `client_turn_id` behaves as today.

## 3. S23 — graph integrity

### 3.1 Serialized edge writes

`add_prerequisite` (service) takes one transaction-scoped `pg_advisory_xact_lock` before running
`would_create_cycle` and inserting — one lock for every prerequisite-edge insert, not one per
subject. A cycle can run through three or more subjects, and two concurrent inserts touching
disjoint subject sets would each hold "their" locks and could still close a ring between them.
Edge edits are rare, so a single lock is both correct and uncontended. The API's cycle check
moves inside the service so the check and the insert run under the same lock. Curriculum commit
and publication need no lock: they only connect components created in the same transaction, which
no stored edge can reach. Deletion takes no lock either: removing a constraint cannot create a
cycle.

### 3.2 Showing conflicts to the learner

The subject page gains a **Curriculum issues** panel, shown only to the subject's owner and only
when there is something to show. Each cycle from the existing conflict report reads "*X* and *Y*
each require the other", names which prerequisite the planner is currently ignoring to break it,
and offers **Remove this prerequisite** (the existing `DELETE /kcs/{id}/prerequisites/{prereq}`)
on each edge of the ring. After a removal the panel and the plan refetch.

Cross-subject prerequisites are no longer listed here — §6 plans them.

## 4. S24 — links and agreement

### 4.1 Candidates

A **candidate** is a pair of KCs in *different* subjects that share a `concept_id`, where both
subjects are visible to one learner. The shared name only makes the pair eligible for review; it
never links anything.

### 4.2 Two parties

Every link needs two agreements: an **endorsement** and the **learner's acceptance**.

- **Curated pair** (both subjects curated, `owner_learner_id IS NULL`): an administrator endorses
  or rejects it from an Admin **Concept links** queue, with a reason. Audited like other admin
  actions.
- **Private pair** (at least one subject owned by a learner): an LLM judge endorses or rejects it.
  - Runs as a taskiq job enqueued when a learner commits a subject, over that learner's
    candidates with no verdict yet.
  - Inputs per side: KC name and description, topic name, subject name, and the names of its
    direct prerequisites and dependents. Never material from a subject the learner cannot see.
  - Output: `endorse | reject` and a one-sentence reason. Role `SMART`, cost logged per call as
    every LLM call is.
  - A reject is final for that pair and never shown to the learner.
  - A failure (timeout, unparseable output, provider down) leaves the pair unjudged for the next
    run; it is never recorded as a reject.

An endorsed pair is a **suggestion** to each learner who can see both sides. The learner
**accepts** or **declines**; an accepted link can later be **revoked**. A decline is final for
that learner and pair.

### 4.3 Data (migration 0061)

`kc_links`:

| column | notes |
|---|---|
| `id` | uuid pk |
| `kc_a_id`, `kc_b_id` | FK `kcs` `ON DELETE CASCADE`; stored with `kc_a_id < kc_b_id`; unique pair |
| `scope` | `curated` \| `private` |
| `owner_learner_id` | FK `learners`, NULL for curated |
| `verdict` | `endorsed` \| `rejected` \| NULL (unjudged) |
| `endorsed_by` | `admin` \| `judge`, NULL until judged |
| `decided_by_admin_id` | NULL unless an administrator decided |
| `reason` | text |
| `decided_at` | timestamp |

`kc_link_decisions`: `learner_id`, `link_id` (FK `ON DELETE CASCADE`), `decision`
(`accepted` \| `declined` \| `revoked`), `decided_at`; unique (`learner_id`, `link_id`).

**One rule, one function:** `links_in_effect(session, learner_id, kc_ids) -> dict[kc_id,
set[kc_id]]` — a link is in effect for a learner when `verdict = 'endorsed'` **and** that
learner's decision is `accepted`. Every consumer (§5, §6, the UI) reads through it.

### 4.4 Endpoints

- `GET /concept-links/suggestions` — endorsed pairs visible to the learner with no decision yet,
  plus their accepted links (for revoke). Each item: both KCs with subject names, the reason.
- `POST /concept-links/{id}/decision` `{decision: accept | decline | revoke}` — 404 when the link
  is not endorsed or either side is not visible (`is_visible_to`); idempotent for a repeated
  decision; `revoke` only from `accepted` (409 otherwise).
- Admin: `GET /admin/concept-links` (curated, unjudged first) and
  `POST /admin/concept-links/{id}` `{verdict, reason}`.

All added to the visibility sweep.

## 5. Head start and confirmation

### 5.1 The seed

On **accept**, for each side of the link: if the *other* side (the source) has measured evidence
(`last_seen_at IS NOT NULL`) and this side (the target) has none, seed the target:

- estimate: the source's current estimate (decayed to now), uncertainty raised to at least
  `transfer_uncertainty_floor` (setting, default **0.6**);
- with several accepted links into one target, the source with the highest conservative estimate;
- a placement seed on the target (no measured evidence) is replaced; real evidence is never.

The seed is written as a `LearningEvent` of type `transfer_seed` on the target, payload
`{link_id, source_kc_id, source_subject_id, ability, uncertainty}`, and sets three new
`LearnerKCState` columns: `transferred_from_kc_id`, `transferred_at`, `transfer_confirmed_at`
(NULL). The source's rows and events are never touched. `transfer_seed` is outside
`ATTEMPT_EVENTS` and `"observation"`, like `placement_seed`.

### 5.2 Provisional until confirmed

A state with `transferred_at` set and `transfer_confirmed_at` NULL is **provisional**:

- it is never mastered — `mastered_kc_ids`, `analytics._is_mastered`'s caller and the achievement
  check in `record_observation` all apply the rule (one helper, `is_provisional(state)`);
- it is confirmed when `mastery.passed_since`'s run check finds `transfer_confirm_passes`
  (setting, default **2**) unassisted, untaught passes on different questions since
  `transferred_at` — `record_observation` sets `transfer_confirmed_at` at that moment;
- a failure restarts the run and lowers the estimate like any wrong answer.

Why this is needed: without it a strong source puts the seed above the bar, and one answer —
even a wrong one (2.0/0.6 → 1.69/0.59 after a miss, conservative 0.51) — would count as mastery.

### 5.3 The check

A plan step whose KC is provisional carries `check_first: true`. The session skips the
explanation and goes straight to practice; a miss falls back to normal teaching.

### 5.4 Revoke

Revoking a link whose seeded target has no `observation` since `transferred_at` deletes the seed
(state row reset to no evidence; the `transfer_seed` event stays in the log, followed by a
`transfer_revoked` event). If the target has answers since, the estimate stands and the state
stays provisional until confirmed.

## 6. Planning with foreign prerequisites

For an edge X→K where K is in the plan's subject B and X is in another subject A, at generation
and every revision:

1. **X has a link in effect to X′ in B** → the edge is read as X′→K and ordered with the local
   edges (the existing `acyclic` pass and conflict report handle any cycle this closes).
2. **X is mastered** (§5.2's rule included) → satisfied; nothing planned.
3. **X is visible to the learner** → an **external step** for X, placed before K:
   - `step_type = "external"`, with `source_subject_id` and `source_subject_name`;
   - guidance applies: guided inserts it `pending`/`active`, exploration inserts it `proposed`;
     the existing `POST .../lesson-plan/detours/{prereq_kc_id}` decides it;
   - closes as `mastered` or `skipped` (no `disproved`); outcome events use the detour outcome
     event with `external: true` in the payload, and a skip bars that (X, K) pair from being
     offered again;
   - counts toward the open-step window like a detour; never toward the goal;
   - answers are recorded against X, in its own subject.
4. **X not visible** → dropped and reported, as today.

One level only: X's own prerequisites in A are not traversed. The step's note links to A's plan.

## 7. Frontend

- **Subject page:** *Curriculum issues* (§3.2) and *Connections* — suggestions with reason and
  Accept / Decline; accepted links with Revoke.
- **Plan:** external steps show "from *A*" and detour-style accept/skip controls; a provisional
  step shows "Confirming what you know from *A*".
- **Admin:** *Concept links* queue with Endorse / Reject and a reason field.

## 8. Settings

| setting | default | meaning |
|---|---|---|
| `transfer_uncertainty_floor` | 0.6 | minimum uncertainty on a transfer seed |
| `transfer_confirm_passes` | 2 | pass run that confirms a provisional component |

Both uncalibrated (S18) and added to the reliability knob inventory.

## 9. Errors

- Decision on an unendorsed or invisible link → 404. Repeated identical decision → 200 with the
  current state. Revoke from anything but `accepted` → 409.
- Admin verdict on a private link → 404. Verdict on an already-decided curated link → 409.
- Concurrent edge that would close a cycle → 409 (existing message).
- Judge failure → pair left unjudged; logged; retried next run.

## 10. Tests

**S34**
1. Two distinct attempts on one KC from two connections: two events, and the final state equals
   applying them in sequence. Fails without the lock.
2. Two multi-KC observations with overlapping KCs in opposite orders complete (no deadlock).
3. A retried chat turn and a retried workflow turn with the same `client_turn_id` each record one
   observation.

**S23**
4. A→B and B→A from two connections: exactly one commits, the other gets 409.
5. A cross-subject edge locks both subjects (race across the two subjects is also serialized).
6. Conflict panel: renders cycles for the owner only; Remove calls delete and refetches.

**S24 — agreement**
7. Curated pair: unjudged → not suggested; admin endorse → suggested; accept → in effect.
8. Private pair: judge endorse → suggested; judge reject → never suggested; judge failure →
   unjudged, retried.
9. Decline is final; revoke only from accepted; a link is never in effect without both.
10. The judge's input for a private pair contains nothing from a subject the learner cannot see.

**S24 — seed**
11. Seeded only when the target has no measured evidence; placement seed replaced; strongest
    source wins; source rows untouched.
12. Provisional: a wrong first answer is not mastery; one pass is not; two passes on different
    questions confirm; a failure restarts; the same question twice counts once.
13. Achievement is not stamped while provisional.
14. Revoke before answers removes the seed; after answers the estimate stands.
15. `transfer_seed` is ignored by attempts, evidence, retention and analytics counts.

**S24 — planning**
16. The four §6 cases; guided vs exploration; a skip is remembered; external steps don't count
    toward goal status.

**Access**
17. Visibility-sweep cases for every new route.
