# Goal Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a learner's goal an honest status — what they have demonstrated, when, whether
that evidence is still current, and whether they have chosen to close it — replacing a single
two-threshold mastery test that answers none of those questions.

**Architecture:** Three separable pieces. (1) `KCEvidence` stops calling one unassisted answer
"retention" and stops calling two item ids "transfer". (2) A conservative estimate
(`ability − k·uncertainty`) replaces the two thresholds, paired with a guard that the estimate
rests on real ability evidence. (3) Achievement is recorded per *component* on
`LearnerKCState.achieved_at`, and a goal's status is derived from its objective's components,
with learner closure a separate column on `LessonPlan`.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, Pydantic v2, pytest;
React + TypeScript + Vite, vitest, Tailwind/daisyUI.

**Spec:** `docs/superpowers/specs/2026-09-22-goal-policy-design.md` (committed at `134e611`)

## Global Constraints

- Python 3.13; ruff line-length 100.
- `uv run poe check` (lint + type-check + test) green before every commit.
- `uv run poe format-check` green before every commit.
- `uv run poe api-contract` green on any commit that changes an API schema — it regenerates
  `frontend/src/api/schema.d.ts` and fails on any difference, so the regenerated file must be
  staged in that same commit.
- Frontend type gate is `npm run build` from `frontend/`. **`npx tsc --noEmit` checks nothing in
  this repo — do not use it.**
- Frontend tests: `npx vitest run` from `frontend/`.
- One tracker item id per commit subject: `[S01]`, `[S14]`, or `[S63]`.
- Every commit message ends with:
  `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`
- Stage only the files the task names, then verify with `git status` after staging.
- **Never** `git reset`, `git commit --amend`, `git rebase`, or `git push --force`.
- **Do NOT open a pull request.** One PR covers all four slices, later.
- 3 failures in `frontend/src/components/auth/RequireLearner.test.tsx` are environment-dependent
  (a developer `.env.local` Clerk key), are green in CI, and are already fixed on the unmerged
  `fix/auth-ui` branch. **Do not fix them here** — a duplicate fix conflicts at merge. Confirm
  this is what you are seeing with
  `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/auth/RequireLearner.test.tsx`,
  which should pass.

---

## File Structure

| File | Responsibility | Tasks |
| --- | --- | --- |
| `app/learning/tracer.py` | `CONSERVATIVE_K`, `Estimate.conservative` — pure estimate arithmetic | 3 |
| `app/learning/mastery.py` | `KCEvidence` + `kc_evidence`; new `KCStanding` + `kc_standings`; achievement write in `record_observation` | 1, 2, 3, 4 |
| `app/core/config.py` | `mastery_conservative_bar`, `goal_evidence_max_age_days` | 3, 5 |
| `app/models/learning.py` | `LearnerKCState.achieved_at` | 4 |
| `app/models/lesson_plan.py` | `LessonPlan.goal_closed_at` | 4 |
| `db/migrations/versions/0058_goal_policy.py` | Both new columns, one migration | 4 |
| `app/services/lesson_plan.py` | `mastered_kc_ids` on the new rule; `goal_status`; `plan_read`; closure write | 3, 5, 6 |
| `app/services/analytics.py` | `_is_mastered` on the new rule + the measured guard | 2, 3 |
| `app/schemas/lesson_plan.py` | `GoalStatusRead`, `LessonPlanClosureSubmit` | 5, 6 |
| `app/schemas/analytics.py` | drop `transfer_shown` | 2 |
| `app/api/v1/lesson_plan.py` | assemble `LessonPlanRead`; closure route | 5, 6 |
| `frontend/src/components/lessons/GoalStatusBar.tsx` | **New.** Presentational status line + bar | 7 |
| `frontend/src/components/lessons/LessonPlanPanel.tsx` | Render `GoalStatusBar` | 7 |
| `frontend/src/components/dashboard/MasteryEvidence.tsx` | drop the transfer line | 2 |

**Why `GoalStatusBar` is a separate file (Task 7):** `LessonPlanPanel` is hook-wired
(`useLessonPlan`, `useCreateConversation`, `useNavigate`) and has no test file, which is why the
copy rules in the spec have nothing holding them. A presentational child taking `goal_status` as
a plain prop is testable without mocking anything — the same split the directory already uses
for `LessonStepRow`.

---

## Task 1: Retention needs two unassisted demonstrations

**Spec:** §4.1, §4.2.

**Files:**
- Modify: `app/learning/mastery.py` — `KCEvidence` (~line 876-892), `kc_evidence` query
  (~line 930-968)
- Modify: `app/services/analytics.py:36` — the `_NO_EVIDENCE` sentinel
- Test: `tests/test_item_exposure.py`, `tests/test_evidence_kinds.py`

**Interfaces:**
- Produces, for Tasks 4 and 5:
  - `KCEvidence.unassisted_attempts: int`
  - `KCEvidence.unassisted_span_days: float | None` — replaces `span_days`, **None** when
    `unassisted_attempts < 2`
  - `KCEvidence.retention_shown(*, min_days: float) -> bool` — unchanged signature, new rule

### The bug

`span_days` is computed from `min(any demonstrated attempt)` to `max(unassisted attempt)`. A
learner walked through a hinted question in January who answers one unaided question in March
scores a 60-day span and `retention_shown = True` on a **single** demonstration. Both endpoints
must be unassisted.

- [ ] **Step 1: Write the failing regression test**

Append to `tests/test_item_exposure.py`:

```python
async def test_one_unaided_answer_after_a_hinted_one_is_not_retention(
    db_session: AsyncSession,
) -> None:
    """A span needs two unaided endpoints, not one.

    The span ran from the first *any* attempt to the last *unassisted* one, so being walked
    through a question in January and answering one unaided in March scored sixty days of
    retention on a single demonstration — "a time span alone", which is exactly what S14 says
    does not establish retention.
    """
    learner = await _learner(db_session)
    _subject, kc = await _kc(db_session)
    a = await _item(db_session, kc, "a")
    b = await _item(db_session, kc, "b")
    await _answer(db_session, learner, kc, a, when=T0, hints=2)
    await _answer(db_session, learner, kc, b, when=T0 + timedelta(days=60))

    (ev,) = (await mastery.kc_evidence(db_session, learner.id, [kc.id])).values()

    assert ev.unassisted_attempts == 1
    assert ev.unassisted_span_days is None
    assert not ev.retention_shown(min_days=1.0)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_item_exposure.py::test_one_unaided_answer_after_a_hinted_one_is_not_retention -v`

Expected: FAIL with `AttributeError: 'KCEvidence' object has no attribute 'unassisted_attempts'`.
That is the right failure — the field does not exist yet. After Step 3 it must pass; if you
want to see the *behavioural* failure this guards, temporarily assert
`ev.span_days is None` instead and watch it fail with `span_days == 60.0`.

- [ ] **Step 3: Change the `KCEvidence` fields and the retention rule**

In `app/learning/mastery.py`, replace the `span_days` field declaration and the two properties
below it. Delete the `span_days` line and put in its place:

```python
    # Distinct unassisted attempts, and the days between the first and the last of them.
    # Both endpoints are unassisted on purpose: a span measured from the first attempt of
    # *any* kind reported a hinted January and an unaided March as sixty days of retention,
    # on one demonstration. The span is None below two attempts, because one demonstration
    # has no span to measure and a 0.0 would read as "measured, and it was zero".
    unassisted_attempts: int
    unassisted_span_days: float | None
```

and replace `retention_shown` with:

```python
    def retention_shown(self, *, min_days: float) -> bool:
        """Demonstrated unaided at least twice, at least ``min_days`` apart.

        Both conditions are stated although a positive ``min_days`` implies the count: a
        configured 0 would otherwise silently collapse this back to "was ever answered
        unaided", which is the bug this rule replaced.
        """
        return (
            self.unassisted_attempts >= 2
            and self.unassisted_span_days is not None
            and self.unassisted_span_days >= min_days
        )
```

- [ ] **Step 4: Change the query**

In `kc_evidence`, the `select(...)` currently reads (in order) `kc_id`, attempts,
distinct_items, unassisted_items, `func.min(case((demonstrated, when)))`,
`func.max(case((and_(demonstrated, unassisted), when)))`, self_reported.

Add a named expression above the `select`, beside the existing `demonstrated`/`self_rated`:

```python
    # Every retention endpoint is an unassisted demonstration; see KCEvidence.
    unassisted_when = case((and_(demonstrated, unassisted), when))
```

Then replace the two aggregate columns so the select reads:

```python
            func.count(distinct(case((and_(demonstrated, unassisted), attempt_key)))),
            func.min(unassisted_when),
            func.max(unassisted_when),
```

in place of the single `func.min(case((demonstrated, when)))` and the existing
`func.max(...)` — i.e. the tuple gains one column and both span endpoints become unassisted.

- [ ] **Step 5: Change the row unpacking**

Replace the unpacking loop's variable list and the span computation:

```python
    for (
        kc_id,
        attempts,
        items,
        unassisted_items,
        unassisted_attempts,
        first_unassisted_at,
        last_unassisted_at,
        self_reported,
    ) in rows:
        if kc_id is None:
            continue
        unassisted_attempts = int(unassisted_attempts or 0)
        span = None
        if unassisted_attempts >= 2:
            span = (last_unassisted_at - first_unassisted_at).total_seconds() / _SECONDS_PER_DAY
        out[kc_id] = KCEvidence(
            kc_id=kc_id,
            attempts=int(attempts or 0),
            distinct_items=int(items or 0),
            unassisted_items=int(unassisted_items or 0),
            unassisted_attempts=unassisted_attempts,
            unassisted_span_days=span,
            self_reported_attempts=int(self_reported or 0),
        )
```

- [ ] **Step 6: Update the `_NO_EVIDENCE` sentinel**

In `app/services/analytics.py`, the `_NO_EVIDENCE` literal constructs a `KCEvidence`. Replace
its `span_days=None,` line with:

```python
    unassisted_attempts=0,
    unassisted_span_days=None,
```

- [ ] **Step 7: Update the three existing tests that name `span_days`**

These assert the old field. Each keeps its intent; only the field and the expected value change.

`tests/test_item_exposure.py` — in `test_a_re_look_at_the_same_question_does_not_extend_the_span`
(the one asserting `ev.span_days == 0.0`), replace that assertion with:

```python
    assert ev.unassisted_attempts == 1
    assert ev.unassisted_span_days is None, "one unaided attempt has no span to measure"
```

`tests/test_item_exposure.py` — in `test_an_unaided_answer_days_later_shows_retention`, replace
`assert ev.span_days is not None and ev.span_days > 8` with:

```python
    assert ev.unassisted_span_days is not None and ev.unassisted_span_days > 8
```

`tests/test_item_exposure.py` — in the test asserting `ev.span_days is None` for a KC never
answered unaided, rename the field only:

```python
    assert ev.unassisted_span_days is None
```

`tests/test_evidence_kinds.py` — replace `assert evidence[kc.id].span_days == 0.0` with:

```python
    assert evidence[kc.id].unassisted_span_days is None
```

(The self-rating is not a demonstration, so the one demonstrated attempt leaves nothing to
span. The test's docstring about the span not starting at the self-rating still holds.)

- [ ] **Step 8: Run the full backend gate**

Run: `uv run poe check && uv run poe format-check`
Expected: all pass. Any remaining `span_days` reference is a compile-time error from `ty`.

- [ ] **Step 9: Prove the new guard is not vacuous**

Temporarily revert the `func.min(unassisted_when)` back to `func.min(case((demonstrated, when)))`
and drop the `unassisted_attempts >= 2` condition from `retention_shown`. Run:

`uv run pytest tests/test_item_exposure.py::test_one_unaided_answer_after_a_hinted_one_is_not_retention -v`

Expected: FAIL. Then restore both and confirm with `git diff` that the file is back as written,
and re-run the test to see it pass. **Report the test-summary line from the failing run** — a
mutation that did not actually apply reports as a survivor.

- [ ] **Step 10: Commit**

```bash
git add app/learning/mastery.py app/services/analytics.py \
        tests/test_item_exposure.py tests/test_evidence_kinds.py
git status
git commit -m "$(cat <<'EOF'
fix(learning): retention needs two unaided demonstrations, not a span [S14]

`span_days` ran from the first attempt of any kind to the last unassisted
one, so a learner walked through a hinted question in January who answered
one unaided question in March scored a sixty-day span and
`retention_shown = True` — on a single demonstration. That is "a time span
alone", which S14 says does not establish retention.

Both endpoints are now unassisted, and the rule requires two attempts
explicitly rather than relying on a positive `min_days` to imply it.

No existing test caught this: the retention test seeds two unaided answers,
so both endpoints were unassisted either way and it passed regardless.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

Verify `git status` is clean and `git log --oneline -1` shows your commit.

---

## Task 2: Two item ids are not transfer

**Spec:** §4.3.

**Files:**
- Modify: `app/learning/mastery.py` — delete the `transfer_shown` property
- Modify: `app/schemas/analytics.py:32` — delete the `transfer_shown` field
- Modify: `app/services/analytics.py` — delete the `transfer_shown=` kwarg
- Modify: `frontend/src/components/dashboard/MasteryEvidence.tsx:22-26`
- Modify: `frontend/src/components/dashboard/MasteryEvidence.test.tsx`
- Modify: `frontend/src/api/schema.d.ts` (regenerated, not hand-edited)
- Test: `tests/test_item_exposure.py`

**Interfaces:**
- Consumes from Task 1: `KCEvidence.unassisted_attempts`, `.unassisted_span_days`.
- Produces: `KCMasteryRead` no longer has `transfer_shown`. `unassisted_items` stays.

### Why it goes rather than gets fixed

```python
@property
def transfer_shown(self) -> bool:
    """Solved more than one different problem for this KC, unaided."""
    return self.unassisted_items >= 2
```

Two distinct item ids for one component — frequently generated from one template by one
generator — are not "genuinely different applications". The tracker says so in as many words:
*"Two item IDs or a time span alone do not establish transfer or retention."* `unassisted_items`
stays, because as a count it is honest; it was the name that made the claim.

- [ ] **Step 1: Delete the property**

In `app/learning/mastery.py`, delete the whole `transfer_shown` property from `KCEvidence`
(decorator, signature, docstring, return). Leave `unassisted_items` and its comment in place.

- [ ] **Step 2: Delete the schema field**

In `app/schemas/analytics.py`, delete the line `transfer_shown: bool = False` from
`KCMasteryRead`. Leave `retention_shown: bool = False`.

- [ ] **Step 3: Delete the service kwarg**

In `app/services/analytics.py`, delete the line
`transfer_shown=_ev(evidence, kc.id).transfer_shown,` from the `KCMasteryRead(...)`
construction. Leave the `retention_shown=` line.

- [ ] **Step 4: Update the backend tests**

In `tests/test_item_exposure.py`, delete every `transfer_shown` assertion. Three are affected —
the ones currently reading `assert not ev.transfer_shown`, `assert ev.transfer_shown`, and
`assert not ev.transfer_shown, "being walked through a second question is not transfer"`.

In the test named `test_two_different_questions_unaided_show_transfer`, the remaining assertions
are `ev.unassisted_items == 2` and `not ev.retention_shown(min_days=1.0)`. Rename it to
`test_two_different_questions_unaided_are_two_items_not_retention` and replace its body's
transfer assertion so it reads:

```python
    assert ev.unassisted_items == 2
    assert not ev.retention_shown(min_days=1.0), "same sitting is not retention"
```

In `tests/test_item_exposure.py`, the API-level test asserting `kc_read.transfer_shown` must
drop that line, keeping `kc_read.retention_shown`.

In `tests/test_evidence_kinds.py`, delete the line
`assert evidence[kc.id].transfer_shown is False`.

- [ ] **Step 5: Run the backend gate**

Run: `uv run poe check && uv run poe format-check`
Expected: pass. `ty` will flag any `transfer_shown` reference you missed.

- [ ] **Step 6: Regenerate the frontend contract**

Run: `uv run poe api-contract`
Expected: it rewrites `frontend/src/api/schema.d.ts` and then passes. The file must be staged in
this commit. **Do not hand-edit `schema.d.ts`.**

- [ ] **Step 7: Update the dashboard component**

In `frontend/src/components/dashboard/MasteryEvidence.tsx`, delete the `<span>` that renders
the transfer clause:

```tsx
      <span className={kc.transfer_shown ? "text-success" : undefined}>
        {kc.transfer_shown ? "solved a different one" : "no different problem yet"}
      </span>
```

Delete any now-orphaned separator text between it and the retention span, and leave the
retention span exactly as it is. Read the surrounding JSX before cutting: the separator may be a
sibling text node rather than part of either span.

- [ ] **Step 8: Update the component test**

In `frontend/src/components/dashboard/MasteryEvidence.test.tsx`, remove `transfer_shown` from
the `kc()` fixture's defaults, delete the test that asserts the "no different problem yet" copy,
and change the test that renders `kc({ transfer_shown: true, retention_shown: true })` to
`kc({ retention_shown: true })`, dropping any assertion on the transfer copy. Also fix the test
passing `kc({ mastered: true, distinct_items: 1, transfer_shown: false })` to
`kc({ mastered: true, distinct_items: 1 })`.

- [ ] **Step 9: Run the frontend gates**

```bash
cd frontend && npm run build && npx vitest run
```

Expected: build clean; tests pass apart from the 3 known `RequireLearner.test.tsx` failures
described in Global Constraints. Confirm those 3 are the environment ones with
`VITE_CLERK_PUBLISHABLE_KEY= npx vitest run` — that run must be fully green.

- [ ] **Step 10: Commit**

```bash
git add app/learning/mastery.py app/schemas/analytics.py app/services/analytics.py \
        tests/test_item_exposure.py tests/test_evidence_kinds.py \
        frontend/src/api/schema.d.ts \
        frontend/src/components/dashboard/MasteryEvidence.tsx \
        frontend/src/components/dashboard/MasteryEvidence.test.tsx
git status
git commit -m "$(cat <<'EOF'
fix(learning): stop calling two item ids transfer [S14]

`transfer_shown` was `unassisted_items >= 2` — two distinct item ids for
one component, frequently generated from one template by one generator.
That is not "genuinely different applications", and the tracker says so
directly: two item IDs or a time span alone establish neither transfer nor
retention.

Removed rather than repaired, and not replaced: a transfer claim needs
items that differ in a way the system can point at, which is the probe work
S14 still has ahead of it. `unassisted_items` stays — as a count it is
honest; it was the name that made the claim.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: The conservative estimate, and the guard it needs

**Spec:** §3, §3.1, §3.2.

**Files:**
- Modify: `app/learning/tracer.py` — `CONSERVATIVE_K`, `Estimate.conservative`
- Modify: `app/core/config.py` — `mastery_conservative_bar` (near `retention_min_days`, ~line 281)
- Modify: `app/learning/mastery.py` — new `KCStanding` + `kc_standings`
- Modify: `app/services/lesson_plan.py:36-37, 66-84` — constants and `mastered_kc_ids`
- Modify: `app/services/analytics.py` — `_is_mastered` and its import
- Test: `tests/test_mastery.py`, `tests/test_lesson_plan.py`, `tests/test_analytics.py`

**Interfaces:**
- Produces, for Tasks 4 and 5:
  - `tracer.CONSERVATIVE_K: float`
  - `Estimate.conservative -> float` (property)
  - `mastery.KCStanding` with fields `kc_id: uuid.UUID`, `measured_at: datetime | None`,
    `at_measurement: Estimate`, `current: Estimate`, `achieved_at: datetime | None`
  - `async mastery.kc_standings(session, learner_id, kc_ids, *, now=None, estimator=DEFAULT_ESTIMATOR) -> dict[uuid.UUID, KCStanding]`
  - `get_settings().mastery_conservative_bar: float`
- **Note:** `achieved_at` does not exist on `LearnerKCState` until Task 4. In this task,
  `KCStanding.achieved_at` is populated as `None` from a literal, with the one-line comment
  given in Step 4. Task 4 wires it to the column.

### Why the estimate alone is not enough

`app/services/placement.py` seeds `"strong"` as `Estimate(ability=1.75, uncertainty=0.6)`.

- Old rule: `1.75 >= 1.0` ✓ but `0.6 <= 0.5` ✗ → not mastered. Correct only because the seed's
  uncertainty happens to sit above the threshold.
- `ability − uncertainty = 1.15` ✓ → mastered, never assessed. A regression, and no choice of
  `k` and bar avoids it while keeping the old corner.

So the rule is **measured AND conservative ≥ bar**, where measured is
`LearnerKCState.last_seen_at is not None` — the meaning S56 pinned, and the test
`analytics.subject_mastery` already uses for `assessed`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lesson_plan.py`. It already defines `_graph(session) -> (Learner, Subject,
KC, KC)` and already imports `get_settings`, `LearnerKCState`, `datetime`/`UTC`. Add
`from app.learning import mastery` and `from app.learning.tracer import Estimate` to its imports.

```python
async def test_a_placement_seed_alone_is_never_mastered(db_session: AsyncSession) -> None:
    """A background claim is not a demonstration, however strong.

    The old rule excluded a "strong" seed only because that seed's uncertainty happened to
    sit above the uncertainty threshold — tuned to 0.45 it would have counted. The
    conservative estimate on its own is *more* permissive here, so the rule is paired with a
    requirement that the estimate rests on ability evidence at all.
    """
    learner, _subject, root, _dependent = await _graph(db_session)
    seeded = Estimate(ability=1.75, uncertainty=0.6)
    await mastery.seed_prior(db_session, learner.id, root.id, seeded)

    mastered = await svc.mastered_kc_ids(db_session, learner.id, [root.id])

    assert root.id not in mastered
    # The guard is what excluded it, not the arithmetic: assert the estimate would have
    # passed the bar on its own, so a later loosening of the guard fails this test.
    assert seeded.conservative >= get_settings().mastery_conservative_bar


async def test_a_measured_component_at_the_old_corner_is_still_mastered(
    db_session: AsyncSession,
) -> None:
    """The bar sits on the old rule's corner, so the two agree where both had an opinion."""
    learner, _subject, root, _dependent = await _graph(db_session)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=root.id,
            ability=1.0,
            uncertainty=0.5,
            last_seen_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    assert root.id in await svc.mastered_kc_ids(db_session, learner.id, [root.id])
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_lesson_plan.py -k "placement_seed_alone or old_corner" -v`
Expected: FAIL — `Estimate` has no attribute `conservative`, and `Settings` has no
`mastery_conservative_bar`.

- [ ] **Step 3: Add the estimate arithmetic**

In `app/learning/tracer.py`, beside `MIN_UNCERTAINTY`:

```python
CONSERVATIVE_K = 1.0
"""How many standard deviations below the point estimate we are willing to claim."""
```

and add to `Estimate`, after the two fields:

```python
    @property
    def conservative(self) -> float:
        """The ability this estimate supports even if we are wrong by one standard deviation.

        A lower confidence bound. This is the number to compare against a bar, because the
        point estimate alone says the same thing about a learner measured once and one
        measured thirty times.
        """
        return self.ability - CONSERVATIVE_K * self.uncertainty
```

- [ ] **Step 4: Add the settings**

In `app/core/config.py`, directly below `retention_min_days`:

```python
    # The lower confidence bound a component must clear to count as mastered. v1-arbitrary,
    # the same standing as the two thresholds it replaces and as retention_min_days above:
    # not calibrated against outcome data. Chosen to sit exactly on the old rule's corner —
    # ability 1.0 with uncertainty 0.5 scores 0.5 — so this is a change of shape, not of
    # strictness at the point where both rules agree. A setting rather than a module
    # constant in services/lesson_plan.py because mastery.record_observation needs it too,
    # and app/learning/ importing from app/services/ is the wrong direction.
    mastery_conservative_bar: float = 0.5
```

- [ ] **Step 5: Add `KCStanding` and `kc_standings`**

In `app/learning/mastery.py`, directly after `estimate_kcs`:

```python
class KCStanding(BaseModel):
    """Everything the mastery *state row* knows about one component.

    Both estimates are carried because they answer different questions, and the difference is
    load-bearing: ``current`` has uncertainty decayed to now and is what "can they do this
    today" means, while ``at_measurement`` is what we actually saw and is what "we measured
    them at the bar, and it was a long time ago" means. Deriving one from the other at each
    call site is how two callers come to disagree about staleness.
    """

    kc_id: uuid.UUID
    # None when the component has no *ability* evidence — the meaning S56 pinned. A placement
    # seed writes a state row and leaves this unset, which is what keeps a background claim
    # from reading as mastery.
    measured_at: datetime | None
    at_measurement: Estimate
    current: Estimate
    achieved_at: datetime | None


async def kc_standings(
    session: AsyncSession,
    learner_id: uuid.UUID,
    kc_ids: Sequence[uuid.UUID],
    *,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> dict[uuid.UUID, KCStanding]:
    """State-row facts for many components in one query.

    Like ``estimate_kcs``, a component with no row still appears, at the unknown prior with
    ``measured_at=None``: absent from the table is a fact about the learner, not a reason to
    leave it out of the answer.
    """
    now = now or datetime.now(UTC)
    if not kc_ids:
        return {}
    states = (
        await session.scalars(
            select(LearnerKCState).where(
                LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id.in_(kc_ids)
            )
        )
    ).all()
    by_kc = {
        state.kc_id: KCStanding(
            kc_id=state.kc_id,
            measured_at=state.last_seen_at,
            at_measurement=_estimate_of(state),
            current=estimator.decay(
                _estimate_of(state), elapsed_days=_elapsed_days(state.last_seen_at, now)
            ),
            # Wired to LearnerKCState.achieved_at in the task that adds the column.
            achieved_at=None,
        )
        for state in states
    }
    return {
        kc_id: by_kc.get(
            kc_id,
            KCStanding(
                kc_id=kc_id,
                measured_at=None,
                at_measurement=Estimate(),
                current=Estimate(),
                achieved_at=None,
            ),
        )
        for kc_id in kc_ids
    }
```

- [ ] **Step 6: Rewrite `mastered_kc_ids`**

In `app/services/lesson_plan.py`, delete `MASTERY_ABILITY_THRESHOLD` and
`MASTERY_UNCERTAINTY_THRESHOLD` with their shared docstring, and replace `mastered_kc_ids`'s
body:

```python
async def mastered_kc_ids(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of ``kc_ids`` the learner has demonstrably mastered.

    Public because it is the system's one definition of "mastered", and a second caller
    (``app.services.checkpoints``, deciding whether a paused question is still worth asking)
    re-implementing the comparison would give the planner and the resumer the power to
    disagree about whether a learner had finished something.

    Two conditions, and the first is not redundant. The conservative estimate trades ability
    against uncertainty, so a confident-looking placement seed clears it outright; requiring
    ability evidence is what keeps a self-reported background from reading as mastery. This
    asks about *now* on purpose — retention and freshness belong to the goal's question, not
    the planner's, and a planner that waited days for a second demonstration could never
    finish a step.
    """
    ids = list(kc_ids)
    if not ids:
        return set()
    bar = get_settings().mastery_conservative_bar
    standings = await mastery.kc_standings(session, learner_id, ids)
    return {
        kc_id
        for kc_id, standing in standings.items()
        if standing.measured_at is not None and standing.current.conservative >= bar
    }
```

This also removes an N+1: the old body called `estimate_kc` once per component.

- [ ] **Step 7: Move analytics onto the same rule — all three call sites**

`_is_mastered` is called **three times** in `subject_mastery`, not once: per KC, per topic on
the aggregate `topic_estimate`, and once on the aggregate `subject_estimate`. All three change.

In `app/services/analytics.py`, delete the import line
`from app.services.lesson_plan import MASTERY_ABILITY_THRESHOLD, MASTERY_UNCERTAINTY_THRESHOLD`
and replace `_is_mastered` with:

```python
def _is_mastered(estimate: Estimate, *, bar: float) -> bool:
    """The planner's rule, minus the coverage guard each caller supplies.

    The guard is the caller's because what counts as "measured" differs by level: one KC has
    a `last_seen_at`, a topic has a count of assessed components. Both are already in hand at
    each call site, so agreeing with the planner costs no extra query — and disagreeing with
    it is precisely what ``mastered_kc_ids`` exists to prevent.
    """
    return estimate.conservative >= bar
```

In `subject_mastery`, read the bar once beside `retention_min_days`:

```python
    conservative_bar = get_settings().mastery_conservative_bar
```

Change the `mastered=` kwarg in the `KCMasteryRead(...)` construction to:

```python
                mastered=kc.id in assessed and _is_mastered(estimates[kc.id], bar=conservative_bar),
```

Change the `mastered=` kwarg in the `TopicMasteryRead(...)` construction to:

```python
                # A topic is not mastered on the strength of components nobody measured: the
                # aggregate averages an unseen KC in at the prior, which is a real number
                # standing in for no evidence.
                mastered=topic_assessed == len(kc_reads)
                and _is_mastered(topic_estimate, bar=conservative_bar),
```

Change the `mastered=` kwarg in the final `SubjectMasteryRead(...)` to:

```python
        mastered=subject_assessed == subject_total
        and _is_mastered(subject_estimate, bar=conservative_bar),
```

- [ ] **Step 8: Fix the two fixtures that seed mastery without measuring it**

Both predate the guard and would now fail — correctly. Neither is a test to loosen: a component
with no `last_seen_at` has no ability evidence, so a fixture claiming it is mastered is
describing something the system should not believe.

In `tests/test_lesson_plan.py`, `_mastered_state` (used by 4 tests) seeds a row with no
`last_seen_at`. Add one:

```python
async def _mastered_state(session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID) -> None:
    session.add(
        LearnerKCState(
            learner_id=learner_id,
            kc_id=kc_id,
            ability=1.5,
            uncertainty=0.3,
            # Mastery now requires ability evidence, not just a confident row — a placement
            # seed writes a row too. A fixture for "mastered" has to have been measured.
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.flush()
```

In `tests/test_analytics.py`, `test_subject_mastery_flags_mastered_at_every_level` seeds
`LearnerKCState(..., ability=1.5, uncertainty=0.2)` with no `last_seen_at` and asserts
`mastered is True` at all three levels. Add `last_seen_at=datetime.now(UTC),` to that
constructor. The three assertions stay as they are — with the component actually measured, all
three levels are still mastered, and the test now also pins that the roll-up's coverage guard
passes when coverage is complete.

- [ ] **Step 9: Run the tests**

Run: `uv run pytest tests/test_lesson_plan.py tests/test_analytics.py tests/test_mastery.py -v`
Expected: the two new tests pass. Existing tests that assert mastery outcomes may need their
seeded ability/uncertainty adjusted — **only** where the old rule and the new one genuinely
disagree. If a test fails, work out which rule it was encoding before changing it, and say so in
your report; a test that silently flips to match new behaviour is how a regression ships.

- [ ] **Step 10: Run the full gate**

Run: `uv run poe check && uv run poe format-check`
Expected: pass.

- [ ] **Step 11: Prove the guard is not vacuous**

Temporarily drop `standing.measured_at is not None and` from `mastered_kc_ids`. Run:
`uv run pytest tests/test_lesson_plan.py::test_a_placement_seed_alone_is_never_mastered -v`
Expected: FAIL. Restore, confirm with `git diff`, re-run to see it pass. **Report the
test-summary line from the failing run.**

- [ ] **Step 12: Commit**

```bash
git add app/learning/tracer.py app/core/config.py app/learning/mastery.py \
        app/services/lesson_plan.py app/services/analytics.py \
        tests/test_lesson_plan.py tests/test_analytics.py tests/test_mastery.py
git status
git commit -m "$(cat <<'EOF'
feat(learning): judge mastery on a conservative estimate plus real evidence [S01]

Replaces `ability >= 1.0 and uncertainty <= 0.5` with a lower confidence
bound, `ability - k*uncertainty`, as V0_DECISIONS accepts.

The bound alone would have been a regression. Placement seeds "strong" as
ability 1.75 / uncertainty 0.6, which the old rule excluded only because
that seed's uncertainty happens to sit above the threshold — tuned to 0.45
it would have counted — and which the conservative estimate clears
outright at 1.15. No choice of k and bar avoids that while keeping the old
corner, so the rule is paired with a requirement that the estimate rests on
ability evidence: `last_seen_at is not None`, the meaning S56 pinned and
the test analytics already used for `assessed`.

analytics moves onto the same rule in the same commit. It imported both
constants and kept its own copy of the comparison, so leaving it behind
would have made the dashboard and the planner disagree about "mastered" —
exactly what `mastered_kc_ids` exists to prevent.

Also removes an N+1: `mastered_kc_ids` called `estimate_kc` per component.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Record achievement per component

**Spec:** §6, §6.1, §6.2, §7 (column only), §10.

**Files:**
- Create: `db/migrations/versions/0058_goal_policy.py`
- Modify: `app/models/learning.py` — `LearnerKCState.achieved_at`
- Modify: `app/models/lesson_plan.py` — `LessonPlan.goal_closed_at`
- Modify: `app/learning/mastery.py` — `kc_standings` reads the column; achievement write in
  `record_observation`
- Test: `tests/test_evidence_kinds.py`

**Interfaces:**
- Consumes from Task 1: `KCEvidence.retention_shown(min_days=...)`.
- Consumes from Task 3: `Estimate.conservative`, `get_settings().mastery_conservative_bar`,
  `KCStanding.achieved_at`.
- Produces, for Tasks 5 and 6: `LearnerKCState.achieved_at: datetime | None`,
  `LessonPlan.goal_closed_at: datetime | None`, and `KCStanding.achieved_at` now populated.

### Why per component

Regenerating a plan **replaces `goal` and `objective_kc_ids` in place on the same row**, so a
goal-level achievement record needs a key tying it to "its" goal and every choice is wrong
somewhere: match on goal text and a graph change orphans a real achievement; match on the
objective set and re-wording the goal loses it. A fact about a component has no such problem,
and a new goal inheriting a shared component's achievement is correct — that component genuinely
was demonstrated.

- [ ] **Step 1: Write the migration**

Create `db/migrations/versions/0058_goal_policy.py`:

```python
"""Record per-component achievement and learner goal closure (S01).

``learner_kc_state.achieved_at`` is when a component first met the achievement bar — a
conservative estimate at or above the bar with retention demonstrated. Set once and never
cleared: later evidence can lower the current estimate, which is what the estimate is for,
but it cannot unmake a demonstration that happened.

``lesson_plans.goal_closed_at`` is the learner's own choice to finish or archive the goal. It
has no path to any estimate — closing a goal does not fabricate assessment evidence.

No backfill, for two reasons. ``achieved_at`` means "the moment this was first earned", and
there is no such moment in the record for existing rows: the retention rule they would be
judged against is the one S14 just replaced, and applying the new rule retroactively would
date every achievement to this migration. A NULL on a component the learner has genuinely
mastered is self-correcting — the next unassisted demonstration sets it. The cost of not
backfilling is a date that starts late for existing learners; the cost of backfilling is a
date that is wrong for all of them.

Neither column is indexed: both are read as part of rows already fetched by the
``(learner_id, kc_id)`` unique constraint or by primary key, and neither is a filter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058_goal_policy"
down_revision: str | Sequence[str] | None = "0057_self_report_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learner_kc_state",
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lesson_plans",
        sa.Column("goal_closed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("lesson_plans", "goal_closed_at")
    op.drop_column("learner_kc_state", "achieved_at")
```

- [ ] **Step 2: Add the model columns**

In `app/models/learning.py`, in `LearnerKCState`, after `fsrs_card`:

```python
    # When this component first met the achievement bar: a conservative estimate at or above
    # the bar, with retention demonstrated. Set once by `record_observation` and never
    # cleared — later evidence can lower the current estimate, and that is what the estimate
    # is for, but it cannot unmake a demonstration that happened. NULL on every row predating
    # this column; see migration 0058 for why that is not backfilled.
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
```

In `app/models/lesson_plan.py`, in `LessonPlan`, after `revision_pending`:

```python
    # Set when the learner explicitly finishes or archives this goal. It changes nothing about
    # what was measured: no estimate, no achievement, no evidence — there is deliberately no
    # code path from this column to any of them. Cleared when `goal` changes, because a
    # different goal has not been closed, but not on a plain regenerate of the same goal,
    # which is a revision rather than a new intention.
    goal_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

Add `DateTime` to the `sqlalchemy` import in `app/models/lesson_plan.py` and `datetime` to its
`datetime` import if not already present.

- [ ] **Step 3: Run the migration**

Run: `uv run poe db-upgrade`
Expected: applies `0058_goal_policy`. Confirm with `uv run alembic current` (or the project's
equivalent) that head is `0058_goal_policy`.

- [ ] **Step 4: Write the failing tests**

Append to `tests/test_evidence_kinds.py`:

```python
async def test_a_self_rating_never_records_an_achievement(db_session: AsyncSession) -> None:
    """A rating moves the review schedule and nothing else (S56).

    The exclusion is structural, not a condition: the self-report branch returns before the
    ability assignment, so the achievement check placed after it is unreachable from a
    rating. This test is what proves the structure held.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    for day in (0, 30):
        await mastery.record_observation(
            db_session,
            Observation(
                learner_id=learner.id,
                kc_weights={kc.id: 1.0},
                score=1.0,
                evidence_kind=EvidenceKind.SELF_REPORTED,
            ),
            now=t0 + timedelta(days=day),
        )

    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None and state.achieved_at is None


async def test_an_achievement_survives_the_estimate_falling(db_session: AsyncSession) -> None:
    """Current confidence and historical achievement are different claims.

    V0_DECISIONS asks for both and says a historical achievement does not promise permanent
    knowledge — but it also does not stop having happened.
    """
    learner, (kc,) = await _seed(db_session)
    t0 = datetime.now(UTC)
    for day in (0, 1, 30, 31):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=1.0),
            now=t0 + timedelta(days=day),
        )
    state = await db_session.scalar(
        select(mastery.LearnerKCState).where(mastery.LearnerKCState.kc_id == kc.id)
    )
    assert state is not None
    earned = state.achieved_at
    assert earned is not None

    for day in range(32, 44):
        await mastery.record_observation(
            db_session,
            Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=0.0),
            now=t0 + timedelta(days=day),
        )

    await db_session.refresh(state)
    assert state.achieved_at == earned, "an achievement is not revoked by later evidence"
    assert state.ability - state.uncertainty < get_settings().mastery_conservative_bar
```

Use the `_seed(session, *, n=1) -> (Learner, list[KC])` helper already in
`tests/test_evidence_kinds.py`, and read state rows with the `select(mastery.LearnerKCState)`
idiom that file already uses rather than reaching for a private function. Add `select` from
`sqlalchemy`, `get_settings` from `app.core.config`, and `timedelta` to its imports if missing.

- [ ] **Step 5: Run them and watch them fail**

Run: `uv run pytest tests/test_evidence_kinds.py -k "self_rating_never_records or survives_the_estimate" -v`
Expected: the first passes trivially (the column is always NULL); the second FAILS on
`assert earned is not None`.

- [ ] **Step 6: Populate `KCStanding.achieved_at`**

In `app/learning/mastery.py`, in `kc_standings`, replace the placeholder line

```python
            # Wired to LearnerKCState.achieved_at in the task that adds the column.
            achieved_at=None,
```

with:

```python
            achieved_at=state.achieved_at,
```

Leave the `achieved_at=None` in the *default* `KCStanding` for a component with no row — a
component with no state row has no achievement.

- [ ] **Step 7: Write the achievement recorder**

In `app/learning/mastery.py`, directly above `record_observation`:

```python
async def _record_achievements(
    session: AsyncSession,
    learner_id: uuid.UUID,
    states: Sequence[LearnerKCState],
    *,
    now: datetime,
) -> None:
    """Stamp ``achieved_at`` on components that have just earned it, once and for good.

    Called after ``record_observation``'s flush rather than inside its loop: retention comes
    from ``kc_evidence``, which queries the event log, and the event that earns the
    achievement is still pending in the session until that flush. Inside the loop this would
    either miss the demonstration that just completed the span, or depend on autoflush firing
    mid-iteration — which works until someone sets ``autoflush=False``.
    """
    pending = [state for state in states if state.achieved_at is None]
    if not pending:
        return
    settings = get_settings()
    evidence = await kc_evidence(session, learner_id, [state.kc_id for state in pending])
    for state in pending:
        found = evidence.get(state.kc_id)
        if found is None or not found.retention_shown(min_days=settings.retention_min_days):
            continue
        # The state was written moments ago, so `last_seen_at` is `now` and decay is the
        # identity — the stored estimate *is* the current one here. Said explicitly so a
        # later reader neither adds a redundant `kc_standings` call nor reaches for the
        # decayed estimate in a context where that distinction does not yet exist.
        if _estimate_of(state).conservative >= settings.mastery_conservative_bar:
            state.achieved_at = now
```

- [ ] **Step 8: Call it from `record_observation`**

In `record_observation`, declare a second list beside `updated: list[LearnerKCState] = []`:

```python
    # Only the ability path appends here. The self-report branch appends to `updated` and
    # returns before the ability assignment, so a rating is structurally unable to reach the
    # achievement check — the same way it cannot reach `last_seen_at`.
    demonstrated_states: list[LearnerKCState] = []
```

At the very end of the ability branch, immediately before the existing `updated.append(state)`
that closes the loop (the one *after* the `LearningEvent` with `event_type="observation"`), add:

```python
        demonstrated_states.append(state)
```

Then replace the function's final two lines:

```python
    await session.flush()
    await _record_achievements(session, obs.learner_id, demonstrated_states, now=now)
    await session.flush()
    return updated
```

- [ ] **Step 9: Run the tests**

Run: `uv run pytest tests/test_evidence_kinds.py -v`
Expected: both new tests pass, and every existing test in the file still passes.

- [ ] **Step 10: Run the full gate**

Run: `uv run poe check && uv run poe format-check`
Expected: pass.

- [ ] **Step 11: Prove the self-report exclusion is not vacuous**

Temporarily change `demonstrated_states.append(state)` to append inside the self-report branch
as well (add the same line just before that branch's `continue`). Run:
`uv run pytest tests/test_evidence_kinds.py::test_a_self_rating_never_records_an_achievement -v`

Expected: **this may still pass**, because a self-report writes no `observation` event and so
shows no retention. If it passes, that is a second, independent reason the exclusion holds —
record that in your report, restore the code, and instead prove the *ordering* guard: move the
`_record_achievements` call to before the first `await session.flush()` and run
`uv run pytest tests/test_evidence_kinds.py::test_an_achievement_survives_the_estimate_falling -v`,
which must FAIL on `assert earned is not None`. Restore, confirm with `git diff`, re-run green.
**Report the test-summary line from whichever mutation produced the failure.**

- [ ] **Step 12: Commit**

```bash
git add db/migrations/versions/0058_goal_policy.py app/models/learning.py \
        app/models/lesson_plan.py app/learning/mastery.py tests/test_evidence_kinds.py
git status
git commit -m "$(cat <<'EOF'
feat(learning): record achievement per component, once and for good [S01]

V0_DECISIONS asks the goal model to distinguish current evidence meeting
its target from a historical achievement across independent checks. Current
confidence is recomputed on every read and so can lapse, which is right for
confidence and wrong for history — so achievement is recorded.

Recorded per *component*, not per goal. Regenerating a plan replaces `goal`
and `objective_kc_ids` in place on the same row, so a goal-level record
needs a key tying it to its goal and every choice is wrong somewhere:
match on goal text and a graph change orphans a real achievement; match on
the objective set and re-wording the goal loses it. A fact about a
component has no such problem, and a new goal inheriting a shared
component's achievement is correct — that component was demonstrated.

The check runs after the per-KC loop and after the flush, because
`retention_shown` reads the event log and the event that earns the
achievement is pending until then. It takes its own component list, since
`record_observation` appends to `updated` in the self-report branch too.

Adds `lesson_plans.goal_closed_at` in the same migration; it is used next.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Goal status on the API

**Spec:** §5, §8.

**Files:**
- Modify: `app/core/config.py` — `goal_evidence_max_age_days`
- Modify: `app/schemas/lesson_plan.py` — `GoalStatusRead`, `LessonPlanRead.goal_status`
- Modify: `app/services/lesson_plan.py` — `goal_status`, `plan_read`
- Modify: `app/api/v1/lesson_plan.py` — both routes assemble through `plan_read`
- Modify: `frontend/src/api/schema.d.ts` (regenerated)
- Test: `tests/test_lesson_plan.py`

**Interfaces:**
- Consumes from Tasks 3 and 4: `mastery.kc_standings`, `KCStanding.achieved_at`,
  `Estimate.conservative`, `get_settings().mastery_conservative_bar`, `LessonPlan.goal_closed_at`.
- Produces, for Tasks 6 and 7: `GoalStatusRead` with fields `objective_kc_count: int`,
  `achieved_kc_count: int`, `current_kc_count: int`, `stale_kc_count: int`,
  `achieved_at: datetime | None`, `closed_at: datetime | None`; and
  `async svc.plan_read(session, plan, *, now=None) -> LessonPlanRead`.

### Why staleness is its own axis, and why it uses the other estimate

`GlickoEstimator.decay` caps uncertainty at `max_uncertainty` and leaves ability untouched
(`tracer.py:124-125`), so the conservative estimate has a permanent floor of
`ability − k·cap` — a learner who once scored 3.0 sits at 2.0 forever. **The conservative
estimate alone can never lapse.** Freshness has to be a separate gate on `last_seen_at`.

Not `due_at`: a self-rating moves `due_at` (S56 routes flashcard ratings straight to
`scheduler.review`), so keying staleness off it would let a rating suppress the staleness of a
component whose ability was last measured months ago.

`current` and `stale` use **different** estimates, and that is the point. If staleness were
tested against the decayed estimate, a component the learner clearly had and then drifted away
from would fail the bar *because* it is old and fall out of both counts — rendering as though
they had never learned it.

- [ ] **Step 1: Add the setting**

In `app/core/config.py`, below `mastery_conservative_bar`:

```python
    # When ability evidence stops counting as current. Uncalibrated, like the two settings
    # above it: the estimate cannot express this at all — decay caps uncertainty and never
    # lowers ability, so an arbitrarily high past score never lapses on its own — which
    # leaves a flat window as the honest stand-in until S59's delayed-outcome study supplies
    # a real one. Generous on purpose: staleness is reported and never acted on, so erring
    # long costs little.
    goal_evidence_max_age_days: float = 60.0
```

- [ ] **Step 2: Add the schema**

In `app/schemas/lesson_plan.py`, above `LessonPlanRead`:

```python
class GoalStatusRead(BaseModel):
    """What is and is not known about the learner's progress toward this plan's goal.

    Four separate facts, deliberately not collapsed into one enum: a goal can be achieved and
    stale, or closed and unfinished, and an enum would need a member per combination.
    """

    # Every component the goal needs. 0 on plans generated before objectives were recorded,
    # which is why a consumer must read 0 as "unknown" and not as "nothing left to do".
    objective_kc_count: int
    # Components with a recorded achievement. Historical: never decreases. Overlaps both
    # counts below — an achieved component is also current or stale — so these three must
    # never be summed.
    achieved_kc_count: int
    # Measured, evidence still within the window, and the estimate *decayed to now* meets
    # the bar: "they can do this today".
    current_kc_count: int
    # Measured, evidence older than the window, and the estimate *as of that measurement*
    # met the bar: "we saw them do this, and it was a long time ago". Disjoint from
    # `current_kc_count` by the freshness test; the other estimate is used on purpose, or a
    # component that drifted would fail the bar *because* it is old and fall out of both.
    stale_kc_count: int
    # Set when every objective component has been achieved; the latest of their dates.
    achieved_at: datetime | None
    # The learner's own closure. Independent of everything above, and with no path to any
    # estimate: closing a goal does not fabricate assessment evidence.
    closed_at: datetime | None
```

and add to `LessonPlanRead`, after `deferred_kc_count`:

```python
    # Computed, never stored. The default exists so `model_validate(plan)` stays total
    # against an ORM row that has no such attribute; `plan_read` always overwrites it, and
    # an all-zero status renders as nothing at all (objective_kc_count 0 means "unknown").
    goal_status: GoalStatusRead = GoalStatusRead(
        objective_kc_count=0,
        achieved_kc_count=0,
        current_kc_count=0,
        stale_kc_count=0,
        achieved_at=None,
        closed_at=None,
    )
```

- [ ] **Step 3: Write the failing test**

Append to `tests/test_lesson_plan.py`:

```python
async def test_stale_evidence_is_reported_without_dropping_the_component(
    db_session: AsyncSession,
) -> None:
    """A component measured at the bar long ago is stale, not unlearned.

    Staleness is judged on the estimate *as of the measurement*. Judged on the decayed one, a
    component the learner clearly had and drifted away from would fail the bar because it is
    old and fall out of both counts — rendering as though they had never learned it.
    """
    learner, _subject, root, _dependent = await _graph(db_session)
    long_ago = datetime.now(UTC) - timedelta(days=400)
    db_session.add(
        LearnerKCState(
            learner_id=learner.id,
            kc_id=root.id,
            ability=2.0,
            uncertainty=0.3,
            last_seen_at=long_ago,
            achieved_at=long_ago,
        )
    )
    await db_session.flush()

    status = await svc.goal_status(
        db_session, learner_id=learner.id, objective_kc_ids=[str(root.id)], closed_at=None
    )

    assert status.stale_kc_count == 1
    assert status.current_kc_count == 0
    assert status.achieved_kc_count == 1
    assert status.achieved_at == long_ago
```

- [ ] **Step 4: Run it and watch it fail**

Run: `uv run pytest tests/test_lesson_plan.py::test_stale_evidence_is_reported_without_dropping_the_component -v`
Expected: FAIL — `module 'app.services.lesson_plan' has no attribute 'goal_status'`.

- [ ] **Step 5: Implement `goal_status`**

In `app/services/lesson_plan.py`, after `mastered_kc_ids`:

```python
async def goal_status(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    objective_kc_ids: Sequence[str],
    closed_at: datetime | None,
    now: datetime | None = None,
) -> GoalStatusRead:
    """What the learner has demonstrated toward this goal, and how current it still is.

    Achievement deliberately ignores freshness. If it did not, a long objective could never
    be achieved: the first component learned would go stale before the last was reached, and
    the goal would sit permanently one component short with no way to catch up. Historical
    achievement and stale current evidence are two different things V0_DECISIONS asks for,
    and the learner is shown both.
    """
    now = now or datetime.now(UTC)
    kc_ids = [uuid.UUID(kc_id) for kc_id in objective_kc_ids]
    if not kc_ids:
        return GoalStatusRead(
            objective_kc_count=0,
            achieved_kc_count=0,
            current_kc_count=0,
            stale_kc_count=0,
            achieved_at=None,
            closed_at=closed_at,
        )
    settings = get_settings()
    bar = settings.mastery_conservative_bar
    max_age = timedelta(days=settings.goal_evidence_max_age_days)
    standings = await mastery.kc_standings(session, learner_id, kc_ids, now=now)

    achieved_dates = [s.achieved_at for s in standings.values() if s.achieved_at is not None]
    current = 0
    stale = 0
    for standing in standings.values():
        if standing.measured_at is None:
            continue
        if now - standing.measured_at <= max_age:
            # Decayed to now: "can they do this today".
            current += int(standing.current.conservative >= bar)
        else:
            # As of the measurement: "we saw them do this, and it was a long time ago".
            stale += int(standing.at_measurement.conservative >= bar)
    return GoalStatusRead(
        objective_kc_count=len(kc_ids),
        achieved_kc_count=len(achieved_dates),
        current_kc_count=current,
        stale_kc_count=stale,
        # The goal is achieved only when every component is, and it is dated by the last one
        # to arrive — the moment the whole objective was first true at once.
        achieved_at=max(achieved_dates) if len(achieved_dates) == len(kc_ids) else None,
        closed_at=closed_at,
    )
```

Add `GoalStatusRead` to the module's imports from `app.schemas.lesson_plan`, and `Sequence`,
`datetime`/`timedelta`/`UTC` as needed.

- [ ] **Step 6: Add `plan_read` and wire the routes**

In `app/services/lesson_plan.py`, after `goal_status`:

```python
async def plan_read(
    session: AsyncSession, plan: LessonPlan, *, now: datetime | None = None
) -> LessonPlanRead:
    """The API's view of a plan, including a freshly computed goal status.

    Assembled here rather than in the routes so the two that return a plan cannot drift into
    reporting different things, and returned as the schema rather than folded onto the ORM
    object so nothing downstream mistakes a computed status for a stored column.
    """
    status = await goal_status(
        session,
        learner_id=plan.learner_id,
        objective_kc_ids=plan.objective_kc_ids,
        closed_at=plan.goal_closed_at,
        now=now,
    )
    return LessonPlanRead.model_validate(plan).model_copy(update={"goal_status": status})
```

The default given to `goal_status` in Step 2 is what keeps `model_validate(plan)` total against
an ORM row that has no such attribute; `plan_read` then overwrites it.

In `app/api/v1/lesson_plan.py`, change both route bodies to return through it:

```python
    plan = await svc.generate_lesson_plan(
        session, llm, learner_id=learner.id, subject_id=subject_id, goal=data.goal
    )
    return await svc.plan_read(session, plan)
```

and, in the GET route, after the 404 check:

```python
    return await svc.plan_read(session, plan)
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_lesson_plan.py -v`
Expected: the new test passes and the existing plan tests still pass. Tests calling
`svc.generate_lesson_plan` directly get the ORM object as before — only the routes changed.

- [ ] **Step 8: Run the gates and regenerate the contract**

```bash
uv run poe check && uv run poe format-check && uv run poe api-contract
```

Expected: all pass; `frontend/src/api/schema.d.ts` is rewritten and must be staged here.

- [ ] **Step 9: Prove the staleness estimate choice is not vacuous**

Temporarily change the `stale` branch to use `standing.current.conservative` instead of
`standing.at_measurement.conservative`. Run:
`uv run pytest tests/test_lesson_plan.py::test_stale_evidence_is_reported_without_dropping_the_component -v`
Expected: FAIL on `status.stale_kc_count == 1`. Restore, confirm with `git diff`, re-run green.
**Report the test-summary line from the failing run.**

- [ ] **Step 10: Commit**

```bash
git add app/core/config.py app/schemas/lesson_plan.py app/services/lesson_plan.py \
        app/api/v1/lesson_plan.py frontend/src/api/schema.d.ts tests/test_lesson_plan.py
git status
git commit -m "$(cat <<'EOF'
feat(lesson-plan): report goal status as four separate facts [S01]

A goal can be achieved and stale, or closed and unfinished, so the status
is four fields rather than one enum that would need a member per
combination.

Freshness is its own gate on `last_seen_at`, because the conservative
estimate cannot lapse: decay caps uncertainty and leaves ability untouched,
so the bound has a permanent floor and a learner who once scored 3.0 sits
at 2.0 forever. Not keyed on `due_at` — a self-rating moves that, so it
would let a flashcard rating suppress the staleness of a component whose
ability was last measured months ago.

`current` and `stale` use different estimates on purpose. Judged on the
decayed estimate, a component the learner clearly had and drifted away from
would fail the bar *because* it is old and fall out of both counts,
rendering as though they had never learned it.

Achievement ignores freshness: requiring both would leave a long objective
permanently one component short, since the first component learned goes
stale before the last is reached.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Learner closure

**Spec:** §7.

**Files:**
- Modify: `app/schemas/lesson_plan.py` — `LessonPlanClosureSubmit`
- Modify: `app/services/lesson_plan.py` — `set_goal_closed`; clear closure on goal change
- Modify: `app/api/v1/lesson_plan.py` — the PATCH route
- Modify: `frontend/src/api/schema.d.ts` (regenerated)
- Test: `tests/test_lesson_plan.py`

**Interfaces:**
- Consumes from Tasks 4 and 5: `LessonPlan.goal_closed_at`, `svc.plan_read`.
- Produces, for Task 7: `PATCH /subjects/{subject_id}/lesson-plan/closure` with body
  `{"closed": bool}`, returning `LessonPlanRead`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lesson_plan.py`:

```python
async def test_closing_a_goal_changes_no_evidence(db_session: AsyncSession) -> None:
    """Closure is an intention, not a measurement (V0_DECISIONS).

    There is deliberately no code path from `goal_closed_at` to any estimate; this is the
    test that says so out loud.
    """
    learner, subject, root, _dependent = await _graph(db_session)
    state = LearnerKCState(
        learner_id=learner.id,
        kc_id=root.id,
        ability=0.2,
        uncertainty=0.9,
        last_seen_at=datetime.now(UTC),
    )
    db_session.add(state)
    await db_session.flush()
    before = (state.ability, state.uncertainty, state.achieved_at)

    plan = await svc.generate_lesson_plan(
        db_session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    await svc.set_goal_closed(
        db_session, learner_id=learner.id, subject_id=subject.id, closed=True
    )

    await db_session.refresh(state)
    await db_session.refresh(plan)
    assert plan.goal_closed_at is not None
    assert (state.ability, state.uncertainty, state.achieved_at) == before

    status = await svc.goal_status(
        db_session,
        learner_id=learner.id,
        objective_kc_ids=[str(root.id)],
        closed_at=plan.goal_closed_at,
    )
    assert status.closed_at is not None
    assert status.achieved_kc_count == 0, "closing did not fabricate an achievement"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_lesson_plan.py::test_closing_a_goal_changes_no_evidence -v`
Expected: FAIL — `module 'app.services.lesson_plan' has no attribute 'set_goal_closed'`.

- [ ] **Step 3: Add the submit schema**

In `app/schemas/lesson_plan.py`, beside `LessonPlanSubmit`:

```python
class LessonPlanClosureSubmit(BaseModel):
    """Whether the learner considers this goal finished. Reopening is the same call with
    ``false``: closing early and changing your mind must not require destroying the plan."""

    closed: bool
```

- [ ] **Step 4: Implement the service write**

In `app/services/lesson_plan.py`, after `plan_read`:

```python
async def set_goal_closed(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID, closed: bool
) -> LessonPlan | None:
    """Record or withdraw the learner's closure of this plan's goal.

    Touches one column and nothing else — no estimate, no achievement, no step status. A
    closed goal is still computed and still reported in full; the UI leads with the closure,
    the engine does not know about it.
    """
    plan = await _get_plan(session, learner_id, subject_id)
    if plan is None:
        return None
    plan.goal_closed_at = datetime.now(UTC) if closed else None
    await session.commit()
    await session.refresh(plan)
    return plan
```

- [ ] **Step 5: Clear closure when the goal changes**

In `generate_lesson_plan`, the line `plan.goal = goal` replaces the goal in place. Capture the
previous value and clear closure only on a real change:

```python
    # A regenerate of the *same* goal is a revision, not a new intention, so it keeps the
    # learner's closure. A different goal has not been closed by anyone.
    if plan.goal != goal:
        plan.goal_closed_at = None
    plan.goal = goal
```

Place this where `plan.goal = goal` currently is, after the `plan is None` block that may have
just constructed the row (a fresh plan has `goal is None` and `goal_closed_at is None`, so the
comparison is harmless).

- [ ] **Step 6: Add the route**

In `app/api/v1/lesson_plan.py`, after the GET route:

```python
@router.patch("/subjects/{subject_id}/lesson-plan/closure", response_model=LessonPlanRead)
async def set_lesson_plan_closure(
    subject_id: uuid.UUID,
    data: LessonPlanClosureSubmit,
    session: SessionDep,
    learner: CurrentLearner,
):
    await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    plan = await svc.set_goal_closed(
        session, learner_id=learner.id, subject_id=subject_id, closed=data.closed
    )
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no lesson plan for this subject yet")
    return await svc.plan_read(session, plan)
```

Add `LessonPlanClosureSubmit` to the schema import at the top of the file.

- [ ] **Step 7: Run the tests and gates**

```bash
uv run pytest tests/test_lesson_plan.py -v
uv run poe check && uv run poe format-check && uv run poe api-contract
```

Expected: all pass; `schema.d.ts` rewritten and staged here.

- [ ] **Step 8: Prove the goal-change rule is not vacuous**

Add this test first. It drives the real `generate_lesson_plan` — `fake_llm_client()` is how
every other test in this file does it, so there is no reason to reimplement the rule in the
test, and a test that inlines the logic it is checking asserts nothing.

```python
async def test_closure_belongs_to_the_goal_it_was_given_for(db_session: AsyncSession) -> None:
    """Regenerating the same goal is a revision and keeps the closure; changing the goal
    clears it, because a different goal has not been closed by anyone."""
    learner, subject, _root, _dependent = await _graph(db_session)
    await svc.generate_lesson_plan(
        db_session,
        fake_llm_client(),
        learner_id=learner.id,
        subject_id=subject.id,
        goal="learn derivatives",
    )
    await svc.set_goal_closed(
        db_session, learner_id=learner.id, subject_id=subject.id, closed=True
    )

    same = await svc.generate_lesson_plan(
        db_session,
        fake_llm_client(),
        learner_id=learner.id,
        subject_id=subject.id,
        goal="learn derivatives",
    )
    assert same.goal_closed_at is not None, "a regenerate of the same goal is a revision"

    changed = await svc.generate_lesson_plan(
        db_session,
        fake_llm_client(),
        learner_id=learner.id,
        subject_id=subject.id,
        goal="learn integrals",
    )
    assert changed.goal_closed_at is None
```

Run it and see it pass. Then temporarily change `if plan.goal != goal:` to `if False:` and run:
`uv run pytest tests/test_lesson_plan.py::test_closure_belongs_to_the_goal_it_was_given_for -v`

Expected: FAIL on `assert changed.goal_closed_at is None`. Restore, confirm with `git diff`,
re-run green. **Report the test-summary line from the failing run.** Keep the test; it is part
of this commit.

- [ ] **Step 9: Commit**

```bash
git add app/schemas/lesson_plan.py app/services/lesson_plan.py app/api/v1/lesson_plan.py \
        frontend/src/api/schema.d.ts tests/test_lesson_plan.py
git status
git commit -m "$(cat <<'EOF'
feat(lesson-plan): let a learner close a goal without touching the evidence [S01]

Closure is the learner's disposition toward the goal, not a claim about
them: `goal_closed_at` has no code path to any estimate, achievement or
step status, and the test says so explicitly. A closed goal is still
computed and still reported in full — the UI leads with the closure, the
engine does not know about it.

Reopening is the same call with `false`, so closing early and changing your
mind does not require destroying the plan.

Cleared when the goal text changes, kept on a plain regenerate: a
regenerate of the same goal is a revision, not a new intention.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Show the complete goal in the plan panel

**Spec:** §9.

**Files:**
- Create: `frontend/src/components/lessons/GoalStatusBar.tsx`
- Create: `frontend/src/components/lessons/GoalStatusBar.test.tsx`
- Modify: `frontend/src/components/lessons/LessonPlanPanel.tsx`
- Test: `frontend/src/components/lessons/GoalStatusBar.test.tsx`

**Interfaces:**
- Consumes from Tasks 5 and 6: `components["schemas"]["LessonPlanRead"]` now carries
  `goal_status` and the existing `deferred_kc_count`.

### The gap

`LessonPlanPanel` renders `steps` and nothing else. The API has shipped `objective_kc_count` and
`deferred_kc_count` since the objectives work and the panel ignores both — so a learner who
finishes the visible window sees a completed plan with no way to know the goal needs twelve more
components. That is the whole of S63's remaining gap.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/components/lessons/GoalStatusBar.test.tsx`:

```tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { GoalStatusBar } from "./GoalStatusBar";
import type { components } from "../../api/schema";

type GoalStatus = components["schemas"]["GoalStatusRead"];

const status = (over: Partial<GoalStatus> = {}): GoalStatus => ({
  objective_kc_count: 10,
  achieved_kc_count: 4,
  current_kc_count: 4,
  stale_kc_count: 0,
  achieved_at: null,
  closed_at: null,
  ...over,
});

describe("GoalStatusBar", () => {
  it("says how much of the goal is not in the current window", () => {
    render(<GoalStatusBar status={status()} deferredCount={6} />);
    expect(screen.getByText(/6 more components/i)).toBeInTheDocument();
  });

  it("renders nothing when the objective is unknown", () => {
    // 0 means "this plan predates objectives", not "nothing left to do". A full-looking
    // empty bar is the most misleading thing this panel could show.
    const { container } = render(
      <GoalStatusBar status={status({ objective_kc_count: 0 })} deferredCount={0} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("reports stale evidence alongside an achievement, not instead of it", () => {
    render(
      <GoalStatusBar
        status={status({
          achieved_kc_count: 10,
          current_kc_count: 7,
          stale_kc_count: 3,
          achieved_at: "2026-03-01T00:00:00Z",
        })}
        deferredCount={0}
      />,
    );
    expect(screen.getByText(/demonstrated across independent checks/i)).toBeInTheDocument();
    expect(screen.getByText(/3 components? .*worth checking again/i)).toBeInTheDocument();
  });

  it("leads with the learner's closure", () => {
    render(
      <GoalStatusBar
        status={status({ closed_at: "2026-05-01T00:00:00Z" })}
        deferredCount={6}
      />,
    );
    expect(screen.getByText(/you closed this goal/i)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it and watch it fail**

Run from `frontend/`: `npx vitest run src/components/lessons/GoalStatusBar.test.tsx`
Expected: FAIL — the module does not exist.

- [ ] **Step 3: Write the component**

Create `frontend/src/components/lessons/GoalStatusBar.tsx`:

```tsx
import type { components } from "../../api/schema";

type GoalStatus = components["schemas"]["GoalStatusRead"];

/** What the learner has demonstrated toward their goal, and how current it still is (S01/S63).
 *
 * Four facts, not one state. A goal can be achieved and stale, or closed and unfinished, so
 * the headline and the staleness note are rendered independently rather than collapsed into
 * a single label.
 *
 * The copy must not promise knowledge. "Demonstrated across independent checks" is the claim
 * the evidence supports; "you know this" is not — the estimate cannot support it, and S46's
 * honest-display rule is what this panel inherits.
 */
export function GoalStatusBar({
  status,
  deferredCount,
}: {
  status: GoalStatus;
  deferredCount: number;
}) {
  // 0 means this plan predates recorded objectives — "we do not know", not "nothing left".
  // A full-looking empty bar is the most misleading thing this panel could show.
  if (status.objective_kc_count === 0) return null;

  const achieved = status.achieved_kc_count;
  const total = status.objective_kc_count;
  const pct = Math.round((achieved / total) * 100);

  return (
    <div className="border-base-300 flex flex-col gap-2 rounded-box border p-4">
      {status.closed_at ? (
        <p className="text-body">You closed this goal.</p>
      ) : status.achieved_at ? (
        <p className="text-body text-primary">
          Goal complete — demonstrated across independent checks.
        </p>
      ) : (
        <p className="text-body">
          {achieved} of {total} components demonstrated across independent checks.
        </p>
      )}

      <div
        className="bg-base-200 h-2 w-full overflow-hidden rounded-full"
        role="progressbar"
        aria-valuenow={achieved}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label="Components demonstrated"
      >
        <div className="bg-primary h-full" style={{ width: `${pct}%` }} />
      </div>

      {/* Not a segment of the bar: achieved and stale overlap, so stacking them would imply
          they partition. Reported only — nothing reopens a step because evidence aged. */}
      {status.stale_kc_count > 0 && (
        <p className="text-caption text-warning">
          {status.stale_kc_count} component{status.stale_kc_count === 1 ? "" : "s"} last
          checked a while ago — worth checking again.
        </p>
      )}

      {deferredCount > 0 && (
        <p className="text-caption text-base-content/60">
          {deferredCount} more component{deferredCount === 1 ? "" : "s"} are part of this goal
          but not in the current window.
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run the test**

Run from `frontend/`: `npx vitest run src/components/lessons/GoalStatusBar.test.tsx`
Expected: PASS (4 tests).

- [ ] **Step 5: Render it in the panel**

In `frontend/src/components/lessons/LessonPlanPanel.tsx`, import the component:

```tsx
import { GoalStatusBar } from "./GoalStatusBar";
```

and insert it in `LessonPlanPanel`'s returned JSX, between the goal/Start-practice header row
and the `plan.steps.map(...)` list:

```tsx
      <GoalStatusBar status={plan.goal_status} deferredCount={plan.deferred_kc_count} />
```

- [ ] **Step 6: Run the frontend gates**

```bash
cd frontend && npm run build && npx vitest run
```

Expected: build clean; tests pass apart from the 3 known `RequireLearner.test.tsx` failures.
Confirm with `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run` that the full suite is green.

- [ ] **Step 7: Prove the zero-objective guard is not vacuous**

Temporarily delete the `if (status.objective_kc_count === 0) return null;` line. Run:
`npx vitest run src/components/lessons/GoalStatusBar.test.tsx`
Expected: FAIL on "renders nothing when the objective is unknown". Restore, confirm with
`git diff`, re-run green. **Report the test-summary line from the failing run.**

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/lessons/GoalStatusBar.tsx \
        frontend/src/components/lessons/GoalStatusBar.test.tsx \
        frontend/src/components/lessons/LessonPlanPanel.tsx
git status
git commit -m "$(cat <<'EOF'
feat(frontend): show the whole goal, not just the current window [S63]

The API has shipped `objective_kc_count` and `deferred_kc_count` since the
objectives work and the panel ignored both, so a learner who finished the
visible window saw a completed plan with no way to know the goal needed
twelve more components.

Adds a status line, an evidence bar and the deferred count. Presentational
and in its own file so it is testable without mocking `useLessonPlan` —
the same split the directory already uses for LessonStepRow, and the reason
the copy rules now have tests holding them.

The bar shows achievement only; staleness is a separate line, because
achieved and stale overlap and a stacked bar would imply they partition.
An objective count of 0 renders nothing: it means "this plan predates
objectives", and a full-looking empty bar would be the most misleading
thing here.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

After Task 7, run every gate from a clean tree and report the numbers:

```bash
uv run poe check
uv run poe format-check
uv run poe api-contract
cd frontend && npm run build && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run
```

Then confirm the branch is intact:

```bash
git log --oneline $(git merge-base HEAD main)..HEAD
git status
```

Expected: 7 new commits on top of the slice-1 work and the two spec commits, clean tree, no
PR opened.
