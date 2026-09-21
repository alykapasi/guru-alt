# Goal policy: achievement, closure, and evidence sufficiency

**Slice 2 of 4, V0 workstream 2.** Tracker items: **S01** (goal-specific achievement and
closure), **S14** (independent delayed retention evidence — the *consume* half), **S63** (show
the complete goal beyond the planning window).

Builds on slice 1 (S56 evidence kinds), branch `feat/evidence-kinds`, unmerged. Slice 1 is a
hard dependency: it is what made `last_seen_at` mean "when we last had *ability* evidence", and
this slice reads that meaning in three new places.

---

## 1. What this slice decides

`docs/V0_DECISIONS.md` names four things a goal model must keep apart:

1. the learner choosing to finish or archive a goal;
2. current evidence meeting its target;
3. a historical achievement established across independent checks over time;
4. current evidence becoming stale and requiring reassessment.

Today the system expresses none of them. There is no goal entity, no status, no closure. There
is one rule — `mastered_kc_ids` in `app/services/lesson_plan.py`, `ability >= 1.0 and
uncertainty <= 0.5` — that answers a different question (should the planner keep teaching this
component?) and is the only thing resembling an achievement test.

This slice adds the four, and fixes the retention rule they would otherwise be built on.

**Out of scope, deferred to slice 2b:** delayed independent *probes* — the scheduling side of
S14, which generates a fresh unassisted check rather than waiting for one to occur. This slice
consumes whatever independent evidence exists; 2b arranges for more of it.

---

## 2. Two questions, one estimate

The planner and the learner ask different questions and must not share an answer.

- **The planner asks "stop teaching this now?"** It has just received evidence and needs a
  decision this sitting. If it required demonstrations spread across days, a step could never
  complete — the planner would loop on a component the learner just answered correctly three
  times.
- **The goal asks "have you achieved what you set out to learn?"** That claim is about durable
  knowledge, so it requires independent demonstrations separated in time, and it is allowed to
  take days to become true.

So: `mastered_kc_ids` keeps its job and its name, and the goal gets its own rule. They share the
conservative estimate and nothing else. Conflating them is the obvious mistake here and the
design is arranged to make it hard: the retention and freshness inputs are not in the planner's
call path at all.

This is also what makes the two honest together. The planner finishes the window; FSRS schedules
the component to come back; the delayed review, answered unassisted, is the second independent
demonstration that earns retention. The mechanism that produces the goal's evidence already
exists — it just was not being read.

---

## 3. The conservative estimate, and the guard it needs

V0_DECISIONS accepts the conservative estimate. It is a lower confidence bound: the ability we
are willing to claim given how unsure we are.

**New in `app/learning/tracer.py`**, beside `Estimate`:

```python
CONSERVATIVE_K: float = 1.0
"""How many standard deviations below the point estimate we are willing to claim."""
```

and a property on `Estimate`:

```python
@property
def conservative(self) -> float:
    """The ability this estimate supports even if we are wrong by one standard deviation."""
    return self.ability - CONSERVATIVE_K * self.uncertainty
```

It lives in `tracer.py` because it is estimate arithmetic, not policy. The bar it is compared
against is policy and lives with the caller.

**In `app/core/config.py`**, `MASTERY_ABILITY_THRESHOLD` and `MASTERY_UNCERTAINTY_THRESHOLD`
(today module constants in `app/services/lesson_plan.py`) are replaced by one setting:

```python
# The lower confidence bound a component must clear to count as mastered. v1-arbitrary,
# the same standing as the two thresholds it replaces and as retention_min_days above it:
# not calibrated against outcome data. Chosen to sit exactly on the old rule's corner —
# ability 1.0 with uncertainty 0.5 scores 0.5 — so this is a change of shape, not of
# strictness at the point where both rules agree.
mastery_conservative_bar: float = 0.5
```

It is a setting rather than a module constant in `services/lesson_plan.py` because
`mastery.record_observation` needs it too (§6.1), and `app/learning/` importing from
`app/services/` is the wrong direction. `mastery.py` already reads `get_settings()`.

**Both threshold constants have six consumers, not five.** Besides the five call sites of
`mastered_kc_ids`, `app/services/analytics.py` imports the two constants directly and has its
own copy of the comparison, `_is_mastered`. It must move to the new rule in the same commit or
the dashboard and the planner will disagree about what "mastered" means — the exact failure
`mastered_kc_ids`'s docstring says it exists to prevent.

`analytics._is_mastered` also needs §3.1's guard, and can have it for free: `subject_mastery`
already computes an `assessed` set from `last_seen_at.is_not(None)` two statements earlier, so
the call becomes `mastered=kc.id in assessed and _is_mastered(estimates[kc.id])` with no extra
query.

### 3.1 The guard: a conservative estimate is not evidence that anything was measured

Replacing an `AND` of two thresholds with a single linear trade is more permissive along both
axes, and that opens a hole the old rule closed by accident.

`app/services/placement.py` seeds `"strong"` as `Estimate(ability=1.75, uncertainty=0.6)`.

- Old rule: `1.75 >= 1.0` ✓ but `0.6 <= 0.5` ✗ → **not mastered**. Correct, and only because
  the seed's uncertainty happens to sit above the threshold. Had `"strong"` been tuned to
  `0.45`, today's rule would silently call a self-reported background claim "mastered".
- Conservative estimate alone: `1.75 - 0.6 = 1.15` ✓ → **mastered, never assessed.** A
  regression, and no choice of `k` and bar fixes it while preserving the old corner — placement
  sits on the permissive side of the trade by construction.

So the conservative estimate is necessary and not sufficient. It is paired with a requirement
that the estimate rests on something the learner actually did:

> **A component is mastered when we have ability evidence for it *and* its conservative
> estimate meets the bar.**

"We have ability evidence" is `LearnerKCState.last_seen_at is not None` — the meaning slice 1
pinned, and the same test `app/services/analytics.py` already uses for its `assessed` flag.
`seed_prior` writes a state row with `last_seen_at` left `None` and logs a `placement_seed`
event, which is outside `ATTEMPT_EVENTS`; so a seeded component fails the guard however strong
the seed, and the planner and analytics now agree on what "measured" means instead of agreeing
by coincidence.

This is strictly stronger than today's rule against unassessed components, and it is the reason
the conservative estimate can be adopted without loosening anything that matters.

### 3.2 One accessor, one query

`mastered_kc_ids` currently calls `estimate_kc` in a loop — one query per component, the pattern
S62 removed everywhere else. Both it and the goal status need more than an estimate: whether the
component was measured at all, when, and what it looked like *at* that measurement as against
now. One new accessor in `app/learning/mastery.py` serves all of it in one query:

```python
class KCStanding(BaseModel):
    """Everything the mastery *state row* knows about one component.

    Both estimates are carried because they answer different questions and the difference is
    the whole of §5: ``current`` has uncertainty decayed to now and is what "do they know this
    now" means, while ``at_measurement`` is what we actually saw and is what "we measured them
    at the bar, a long time ago" means. Deriving one from the other at each call site is how
    two callers come to disagree about staleness.
    """

    kc_id: uuid.UUID
    # None when the component has no *ability* evidence — the meaning S56 pinned. A
    # placement seed leaves this unset, which is what §3.1 relies on.
    measured_at: datetime | None
    at_measurement: Estimate
    current: Estimate
    achieved_at: datetime | None


async def kc_standings(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, KCStanding]
```

Like `estimate_kcs`, a component with no state row still appears, at the unknown prior with
`measured_at=None`: absent from the table is a fact about the learner, not a reason to leave it
out of the answer.

`mastered_kc_ids` becomes `measured_at is not None and current.conservative >= bar` over this
one query. Its five call sites (`lesson_plan.py:188/230/258`, `checkpoints.py:122`, and via
`learning/lesson_plan.py:466/499`) are unchanged — the signature does not move.

---

## 4. Retention fixed, transfer dropped

### 4.1 The bug

`KCEvidence.span_days` is documented as "days between the first attempt at this KC and the most
recent *unassisted* one", and `retention_shown(min_days)` is `span_days >= min_days`.

The first endpoint is **any** attempt; the second is an **unassisted** attempt. So a learner who
was walked through a hinted attempt in January and gave one unassisted answer in March records a
60-day span and `retention_shown = True` — on a **single** demonstration. That is exactly what
the tracker says must not count: *"Two item IDs or a time span alone do not establish transfer
or retention."*

No existing test catches it: `test_an_unaided_answer_days_later_shows_retention` seeds two
unaided answers, so both endpoints are unassisted either way.

### 4.2 The fix

Retention means **two or more unassisted demonstrations, at least `min_days` apart.** Both
endpoints become unassisted:

```python
unassisted_attempts: int
"""Distinct unassisted attempts — no hints, no earlier look at that question this sitting."""

unassisted_span_days: float | None
"""Days between the first and last *unassisted* demonstration. None when fewer than two."""

def retention_shown(self, *, min_days: float) -> bool:
    """Demonstrated unaided twice, at least ``min_days`` apart.

    Both conditions are stated even though a positive ``min_days`` implies the count: a
    configuration of 0 would otherwise collapse this to "was ever answered unaided", which
    is the bug this replaced.
    """
    return (
        self.unassisted_attempts >= 2
        and self.unassisted_span_days is not None
        and self.unassisted_span_days >= min_days
    )
```

In the `kc_evidence` query, `func.min(case((demonstrated, when)))` becomes
`func.min(case((and_(demonstrated, unassisted), when)))`, and a count of
`distinct(case((and_(demonstrated, unassisted), attempt_key)))` is added. `span_days` is
renamed rather than kept alongside: two spans differing in one endpoint is a thing to confuse,
and nothing consumes "time since first exposure" today.

Both existing retention tests still pass unchanged, for the right reason — verify this rather
than assume it, then add the regression test the rename exists for (hinted January, unassisted
March → `retention_shown` is False).

### 4.3 Transfer is removed, not repaired

```python
@property
def transfer_shown(self) -> bool:
    """Solved more than one different problem for this KC, unaided."""
    return self.unassisted_items >= 2
```

Two distinct item IDs for one component, frequently generated from one template by one
generator, are not "genuinely different applications". The property is deleted. `unassisted_items`
stays — as a count it is honest; it was only the name `transfer_shown` that made a claim the
data cannot support.

This removes a field from the analytics API (`app/schemas/analytics.py`) and a line from the
dashboard (`frontend/src/components/dashboard/MasteryEvidence.tsx`, which renders "solved a
different one" / "no different problem yet"). Both go. Nothing replaces it until something can
earn it — a transfer claim needs items that differ in a way the system can point at, which is
S14's probe work and not this slice.

---

## 5. Freshness is its own axis, driven by time

`GlickoEstimator.decay` regrows uncertainty with elapsed time but caps it at `max_uncertainty`
(`DEFAULT_UNCERTAINTY`, 1.0) and returns `Estimate(ability=prior.ability, ...)` — ability is
untouched (`tracer.py:124-125`). So the conservative estimate has a permanent floor of
`ability - k * cap`. A learner who once scored 3.0 sits at a floor of 2.0 forever, above any
sane bar, no matter how long they stay away. **The conservative estimate alone can never lapse.**
V0_DECISIONS asserts this; the code confirms it.

Freshness is therefore a separate gate on the age of the evidence itself:

> A component's evidence is **stale** when `now - last_seen_at` exceeds
> `goal_evidence_max_age_days`.

Not `due_at`. `due_at` is moved by self-ratings (slice 1 routes a flashcard rating straight to
`scheduler.review`), so keying staleness off it would let a self-rating suppress the staleness
of a component whose ability was last measured months ago — reintroducing precisely the
contamination slice 1 removed. `last_seen_at` is the only column that means what this needs.

**New in `app/core/config.py`**, documented in the same voice as the `retention_min_days`
comment directly above it:

```python
# When ability evidence stops counting as current. Uncalibrated, like retention_min_days
# beside it: the estimate cannot express this at all (decay caps uncertainty and never
# lowers ability, so a high past score never lapses on its own), so a flat window is the
# honest stand-in until S59's delayed-outcome study can supply a real one. Generous on
# purpose — staleness is reported, never acted on, so erring long costs little.
goal_evidence_max_age_days: float = 60.0
```

A component that has never been measured (`last_seen_at is None`) is **not stale** — it is
unmeasured. Those are different, and the counts in §8 keep them apart.

**Stale evidence is reported, not acted on.** The planner does not reopen a step, reorder a
plan, or schedule a reassessment because something went stale. It says so; the learner decides.

---

## 6. Achievement is recorded per component, not per goal

Achievement must be a recorded fact — current confidence is recomputed on every read and so can
lapse, which is right for confidence and wrong for history. "A historical achievement does not
promise permanent knowledge," but it also does not stop having happened.

The tempting shape is a goal-level achievement row. It is the wrong one. Regenerating a lesson
plan **replaces `goal` and `objective_kc_ids` in place on the same row** (see the module
docstring of `app/models/lesson_plan.py`), so a goal-level record would need a key tying it to
the goal it was earned for, and every choice of key is wrong somewhere: match on goal text and a
graph change orphans a real achievement; match on the objective set and re-wording the goal
loses it.

Recording it one level down dissolves the problem. Achievement is a fact about a **component**,
and the goal's achievement is derived from its components:

**New column on `LearnerKCState`:**

```python
# When this component first met the achievement bar: a conservative estimate at or above
# the bar, with retention demonstrated. Set once, by record_observation, and never cleared
# — later evidence can lower the current estimate, and that is what the estimate is for,
# but it cannot unmake a demonstration that happened. NULL on every row predating this
# column; see §10 on why that is not backfilled.
achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
```

A goal is **achieved** when every component of `objective_kc_ids` has a non-NULL `achieved_at`,
and the goal's achievement date is the latest of them.

This also settles the plan-regeneration question in the honest direction: a new goal sharing
components with an old one inherits their achievements, because those components genuinely were
demonstrated. That is not laundering — the evidence is per-component and real, and the goal was
only ever a bag of components.

### 6.1 Where it is written

In `mastery.record_observation`, for each component whose state was just updated and whose
`achieved_at` is still NULL: if its conservative estimate meets the bar and
`retention_shown(min_days=retention_min_days)` holds, set `achieved_at = now`.

**It runs after the per-KC loop and after the existing `await session.flush()`, not inside the
loop.** `retention_shown` is derived from `kc_evidence`, which queries the event log — and the
event this observation just wrote is still pending in the session until that flush. Inside the
loop the check would either miss the demonstration that just earned the achievement, or depend
on SQLAlchemy's autoflush firing mid-iteration, which is the kind of thing that works until
someone sets `autoflush=False`. After the flush, one `kc_evidence` call covers every component
the observation touched.

**The component list is collected in the ability branch only.** `record_observation` appends to
`updated` in *both* branches — the `SELF_REPORTED` path appends and then `continue`s — so
`updated` is the wrong input. A second list, appended to only after the ability assignment,
keeps self-ratings structurally unable to reach the achievement check, the same way slice 1's
`continue` keeps them from reaching `last_seen_at`.

The estimate used is the freshly written state, not a re-read through `kc_standings`. They are
the same number — `last_seen_at` was just set to `now`, so elapsed is zero and decay is the
identity — but saying which one is meant keeps a later reader from adding a redundant query to
"be safe" and, worse, from reaching for the decayed estimate in a context where the distinction
that §8 turns on does not yet exist.

Three properties make this the right site:

- **It is correctly dated.** Every transition into achieved is caused by an answer: retention
  completes on a delayed unassisted demonstration, which is an answer, and freshness is not part
  of the achievement test (§6.2). So the moment it is earned is a moment this function runs. A
  read-path write would instead date the achievement when someone next opened the page.
- **Reads stay reads.** No GET mutates.
- **Self-ratings are excluded structurally, again.** Slice 1's `SELF_REPORTED` branch `continue`s
  before the ability update, so a check placed after that update is unreachable from a
  self-rating. As in slice 1, forgetting the exclusion is impossible rather than merely
  discouraged.

Cost is one extra `kc_evidence` call per recorded observation — a single query covering all of
the observation's components, the same query analytics already runs. It is skipped entirely when
every affected component already has `achieved_at` set.

### 6.2 Achievement does not require freshness

If it did, a long objective could never be achieved: the first component learned would go stale
before the last one was reached, and the goal would be permanently one component short with no
way for the learner to catch up.

So: achievement is historical and ignores freshness; freshness is reported alongside it. This is
exactly the split V0_DECISIONS asks for — item 3 and item 4 on its list are different things —
and the learner sees both: *achieved in March; evidence for 3 components is now stale.*

---

## 7. Closure

Closure is the learner's disposition toward the goal, not a claim about them.

**New column on `LessonPlan`:**

```python
# Set when the learner explicitly finishes or archives this goal. It changes nothing about
# what was measured: no estimate, no achievement, no evidence. Cleared when `goal` changes,
# because a different goal has not been closed — but not on a plain regenerate of the same
# goal, which is a revision, not a new intention.
goal_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
```

**New endpoint**, beside the two in `app/api/v1/lesson_plan.py`:

```text
PATCH /subjects/{subject_id}/lesson-plan/closure   body: {"closed": bool}  → LessonPlanRead
```

Reopening is the same call with `false`; a learner who closes a goal early and changes their mind
must not have to destroy their plan to undo it.

A closed goal is still computed and still reported in full. Closing does not freeze the status,
suppress the counts, or stop the planner — it records an intention, and the UI leads with it.
"Closing a goal does not fabricate assessment evidence" is enforced by there being no code path
from this column to any estimate.

---

## 8. The API surface

`LessonPlanRead` gains one nested object. Facts only — no rendered sentence. Copy stays in the
frontend, matching `ItemPanel`'s `DETAIL_COPY`, so the two cannot drift into disagreeing.

```python
class GoalStatusRead(BaseModel):
    """What is and is not known about the learner's progress toward this plan's goal.

    Four separate facts, deliberately not collapsed into one enum: a goal can be achieved and
    stale, or closed and unfinished, and an enum would need a member per combination.
    """

    # Every component the goal needs. 0 on plans generated before objectives were recorded,
    # which is why every consumer must treat 0 as "unknown", not "nothing left to do".
    objective_kc_count: int
    # Components with a recorded achievement (§6). Historical: never decreases. Overlaps
    # both counts below — an achieved component is also current or stale — so these three
    # must never be summed.
    achieved_kc_count: int
    # Measured, evidence still within the window, and the estimate *decayed to now* meets
    # the bar: "they can do this today".
    current_kc_count: int
    # Measured, evidence older than the window, and the estimate *as of that measurement*
    # met the bar: "we saw them do this, and it was a long time ago".
    stale_kc_count: int
    # Set when every objective component has been achieved; the latest of their dates.
    achieved_at: datetime | None
    # The learner's own closure (§7). Independent of everything above.
    closed_at: datetime | None
```

`LessonPlanRead.goal_status: GoalStatusRead`. `objective_kc_count` and `deferred_kc_count` stay
where they are on `LessonPlanRead` — they are already shipped and already consumed by the
generated client; `goal_status` repeats `objective_kc_count` as its own denominator so the
object is readable on its own.

**`current` and `stale` are disjoint and use different estimates**, and that is deliberate. If
staleness were tested against the decayed estimate, a component the learner clearly had and then
drifted away from would fail the bar *because* it is old, and so fall out of both counts —
rendering as though they had never learned it, which is the single most misleading thing this
object could say. Testing staleness against the estimate as of the measurement asks the right
question: we measured you at the bar, and that was a long time ago.

Components that are measured but have never met the bar, and components never measured, are in
neither count. `objective_kc_count - current - stale` is the work outstanding.

Computing the object costs two queries (`kc_standings`, `kc_evidence`) over the objective set,
both of which the plan read path is already in a position to make.

---

## 9. S63: rendering the complete goal

`frontend/src/components/lessons/LessonPlanPanel.tsx` renders `steps` and nothing else. The API
has shipped `objective_kc_count` and `deferred_kc_count` since the objectives work, and the panel
ignores both — so a learner who finishes the visible window sees a completed plan and has no way
to know the goal needs twelve more components. That is the whole of S63's remaining gap.

The panel gains a **status line plus an evidence bar**, above the steps:

- **Status line** — the headline the learner reads first, chosen in this order: closed →
  achieved → in progress. When evidence is stale, the line says so regardless of which headline
  won, because staleness is an axis and not a state.
- **Evidence bar** — `achieved_kc_count` of `objective_kc_count`. A bar, not a percentage: these
  are counted components, and a percentage invites reading it as a confidence. The bar shows
  achievement only; `stale_kc_count` is a separate line beneath it, not a segment of the bar,
  because achieved and stale overlap (§8) and a stacked bar would imply they partition.
- **Deferred count** — "`deferred_kc_count` more components are part of this goal but not in the
  current window", shown whenever `deferred_kc_count > 0`.

When `objective_kc_count` is 0 (a plan predating objectives), the whole block is omitted rather
than rendered as 0-of-0. Zero here means "we do not know", and a full-looking empty bar is the
single most misleading thing this panel could show.

Copy must not describe achievement as a promise. "Demonstrated across independent checks" is the
claim the evidence supports; "you know this" is not.

---

## 10. Migration and backfill

One Alembic migration, two nullable columns: `learner_kc_state.achieved_at` and
`lesson_plans.goal_closed_at`. Both NULL by default, no index — neither is a filter, both are
read as part of rows already being fetched by primary key or by the existing
`(learner_id, kc_id)` unique constraint.

**No backfill**, for the same reason slice 1 recorded and one more:

- `achieved_at` means "the moment this was first earned". There is no such moment in the record
  for existing rows — the retention rule they would be judged against is the one being fixed in
  §4, and applying the new rule retroactively would date every achievement to the migration.
- A NULL `achieved_at` on a component the learner has genuinely mastered is self-correcting: the
  next unassisted demonstration sets it. The cost of not backfilling is a date that starts late
  for existing learners; the cost of backfilling is a date that is wrong for all of them.

Say this in the model comment so the NULLs are not later mistaken for a bug.

`EVENT_SCHEMA_VERSION` stays at 4. This slice adds no event type and changes no payload — it
reads the event log and writes two derived columns, so a replay of existing events produces
exactly what it produced before.

---

## 11. What this slice does not do

- **Delayed independent probes (slice 2b).** This slice reads the independent evidence that
  happens to exist. Arranging for it — scheduling a fresh unassisted check at a chosen delay,
  rather than waiting for FSRS to surface one — is S14's other half.
- **Transfer.** Dropped in §4.3 and not replaced. It returns when items can be shown to differ
  in a way the system can point at.
- **Calibration.** `MASTERY_CONSERVATIVE_BAR`, `retention_min_days` and
  `goal_evidence_max_age_days` are all uncalibrated and all say so. S59's delayed-outcome study
  is what turns them into measurements.
- **Acting on staleness.** Reported only, by explicit decision.

---

## 12. Testing

Beyond the per-behaviour tests, four guards earn their own attention because each covers a way
this design could pass for the wrong reason:

1. **The retention bug, directly.** One hinted attempt, one unassisted attempt sixty days later
   → `retention_shown` is False. This test fails against today's code, which is the point.
2. **Placement cannot buy mastery.** A component seeded `"strong"` (ability 1.75, uncertainty
   0.6) with no attempts is not in `mastered_kc_ids`, and its conservative estimate is above the
   bar — assert both, so the test proves the *guard* is what excludes it rather than the
   arithmetic happening to fail.
3. **A self-rating never sets `achieved_at`.** A component at the bar with retention already
   shown, given only a self-rating, keeps `achieved_at` NULL and keeps its previous value when it
   already had one.
4. **Achievement survives the estimate falling.** Earn `achieved_at`, then record wrong answers
   until the conservative estimate is below the bar; `achieved_at` is unchanged and
   `current_kc_count` has dropped. This is the current-versus-historical distinction, asserted
   rather than assumed.

Each guard is proven non-vacuous by regressing the code it protects and watching the named test
fail, then restoring and confirming with `git diff` — the same bar slice 1 held.
