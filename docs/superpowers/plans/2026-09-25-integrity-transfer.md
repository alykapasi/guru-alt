# Integrity and Transfer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make evidence and graph writes safe under concurrency (S34, S23), show graph conflicts to their owner, and let a learner carry what they know across subjects through links two parties agree on (S24).

**Architecture:** S34 locks the learner's state row inside `mastery._get_or_create_state` and derives attempt ids from the chat turn's idempotency key. S23 serializes every prerequisite insert behind one transaction-scoped advisory lock with the cycle check inside it. S24 adds `concept_links` (endorsed by an admin or an LLM judge) plus per-learner decisions; an accepted link seeds a *provisional* head start that only a pass run confirms, and the planner turns an unmastered cross-subject prerequisite into a detour step marked `external`.

**Tech Stack:** FastAPI, SQLAlchemy async + Postgres, Alembic, taskiq, pytest; React + TypeScript + React Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-25-integrity-transfer-design.md`

## Global Constraints

- Python 3.13; ruff line length 100.
- Every commit green on `uv run poe check`, `uv run poe format-check` and `uv run poe api-contract`.
- Frontend gate is `npm run build` (run in `frontend/`), **not** `npx tsc --noEmit`. Frontend tests: `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run` (3 `RequireLearner` failures are environmental and pre-existing).
- Regenerate `frontend/src/api/schema.d.ts` with `npm run gen:api` (in `frontend/`) after any API change, and commit it with that change.
- One tracker id per commit subject: `[S34]`, `[S23]` or `[S24]`.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; run `git status` after staging.
- Never reset, amend, rebase, squash or force-push. Do not push. Do not open a PR.
- Settings defaults: `transfer_uncertainty_floor = 0.6`, `transfer_confirm_passes = 2`.
- LLM access only through `app/llm/` by role; the judge uses `ModelRole.SMART`, and every call is logged with `log_llm_call`.

## Deviations from the spec (rulings made while planning)

Each is applied by the task named, which also amends the spec text in the same commit.

1. **One global edge lock, not one per subject (Task 3).** A cycle can run through three or more subjects; two concurrent inserts touching disjoint subject sets would each hold "their" locks and could still close a ring. Edge edits are rare, so a single `pg_advisory_xact_lock` is both correct and uncontended. Curriculum commit and publication need no lock: they only connect components created in the same transaction, which have no stored edges to close a cycle with.
2. **Conflict repair offers the reported edge (Task 4).** The conflict report names the edge the planner is ignoring, not the whole ring, so "Remove this prerequisite" is offered on that edge.
3. **Tables are `concept_links` / `concept_link_decisions` (Task 5).** `Item.kc_links` already means item-to-component tags.
4. **Cross-subject steps are detour steps with `detour_reason = "external"` (Task 9), not a new `step_type`.** Everything the spec asks of them — guidance-based insertion, accept/skip through the detour endpoint, outcome events, a skip barring the pair, pruning, counting toward the window — is exactly the detour lifecycle. A new step type would duplicate it. They differ in three ways, all in the engine: they sort just before the step that needs them, they are never disproved, and they are not recorded as detour trips (so `detour_max_repeats` is unaffected). Their outcome events carry no extra flag; the prerequisite's subject identifies them.
5. **The provisional badge says "Confirming what you already know" (Task 11)** — the plan step does not carry the source subject's name.

## Review Focus

1. **A learner answers on a component whose state row another session already loaded.** SQLAlchemy's identity map returns the stale object even under `FOR UPDATE` unless `populate_existing` is set; the second answer must build on the first's posterior. Pinned in Task 1.
2. **A strong source seeds above the bar and the learner then answers wrong.** Must not count as mastered, and must not stamp `achieved_at`. Pinned in Task 7.
3. **A link is accepted, then revoked before any answer.** The target must go back to having no evidence, and planning must stop treating it as provisional. Pinned in Task 7 and Task 8.
4. **An external prerequisite in a subject the learner cannot see.** Must be dropped, never planned or named. Pinned in Task 9.
5. **Two concurrent edge inserts that together close a ring across subjects.** Only one may commit. Pinned in Task 3 (the global lock makes the two-subject and N-subject cases the same test).

---

## File Structure

**Backend — modified**
- `app/learning/mastery.py` — row lock, sorted iteration, transfer seed/revoke, provisional confirmation, `KCStanding.provisional`, `provisional_kc_ids`.
- `app/services/turn.py` — `attempt_id_for_turn`.
- `app/services/chat.py`, `app/services/workflow.py`, `app/agent/workflow.py`, `app/agent/state.py`, `app/api/v1/chat.py` — attempt id plumbing; check-first practice.
- `app/services/turn_lock.py` — `LOCK_NAMESPACE` made public.
- `app/services/knowledge.py`, `app/api/v1/knowledge.py` — locked edge insert, `WouldCreateCycle`.
- `app/services/lesson_plan.py`, `app/learning/lesson_plan.py`, `app/schemas/lesson_plan.py` — provisional mastery rule, external detours, linked edges, `check_first`.
- `app/services/analytics.py` — provisional components are not mastered.
- `app/core/config.py`, `tests/eval/reliability/knobs.py`, `tests/eval/datasets/mine.py`.
- `app/models/knowledge.py`, `app/models/learning.py` — new models/columns.
- `app/api/deps.py`, `app/workers/tasks.py`, `app/api/v1/admin.py`, `app/api/v1/__init__.py`.

**Backend — created**
- `db/migrations/versions/0061_concept_links.py`
- `app/services/concept_links.py` — candidates, `links_in_effect`, admin verdicts, judging, learner decisions, suggestions.
- `app/learning/link_judge.py` — the LLM judge (prompt, call, parse).
- `app/schemas/concept_links.py`, `app/api/v1/concept_links.py`
- `tests/test_concept_links.py`, `tests/test_transfer.py`, `tests/test_link_judge.py`

**Frontend**
- `frontend/src/api/hooks.ts`, `frontend/src/api/admin.ts`, `frontend/src/api/schema.d.ts`
- `frontend/src/components/lessons/CurriculumIssuesPanel.tsx` (+ test), `ConnectionsPanel.tsx` (+ test), `LessonStepRow.tsx` (+ test)
- `frontend/src/components/ConceptLinkQueue.tsx` (+ test)
- `frontend/src/pages/Lessons.tsx`, `frontend/src/pages/Admin.tsx`

---

### Task 1: Lock the state row, in a fixed order [S34]

**Files:**
- Modify: `app/learning/mastery.py:186-215` (`_get_or_create_state`), `app/learning/mastery.py` (`record_observation` loop header)
- Test: `tests/test_cross_connection.py`, `tests/test_tracer.py` (or the file holding `record_observation` unit tests — `grep -l "record_observation" tests/test_*.py`)

**Interfaces:**
- Produces: `_get_or_create_state(session, learner_id, kc_id) -> LearnerKCState` now returns a row locked `FOR UPDATE` with attributes refreshed from the database. `record_observation` processes `obs.kc_weights` in ascending KC-id order and returns states in that order.

- [ ] **Step 1: Write the failing race test** — append to `tests/test_cross_connection.py`:

```python
# --- two different answers on one component at once (S34) --------------------------------------


async def test_a_second_answer_waits_for_the_first_and_builds_on_it(
    engine: AsyncEngine, live_learner: Learner
) -> None:
    """Distinct attempts on one component must serialize on its state row.

    Unlocked, the second writer read the same prior as the first and its write erased the
    first's update. The second session also reads the state *before* answering — as the chat
    check does (`estimate_kcs` for the priors) — so this fails without `populate_existing`
    too: the identity map would hand back the stale object even under `FOR UPDATE`.
    """
    subject, kc = await _seed_kc(engine)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            await mastery.record_observation(
                setup, Observation(learner_id=live_learner.id, kc_weights={kc.id: 1.0}, score=0.4)
            )
            await setup.commit()

        async with (
            AsyncSession(engine, expire_on_commit=False) as first,
            AsyncSession(engine, expire_on_commit=False) as second,
        ):
            await mastery.estimate_kcs(second, live_learner.id, [kc.id])
            await mastery.record_observation(
                first, Observation(learner_id=live_learner.id, kc_weights={kc.id: 1.0}, score=1.0)
            )
            pending = asyncio.create_task(
                mastery.record_observation(
                    second,
                    Observation(learner_id=live_learner.id, kc_weights={kc.id: 1.0}, score=1.0),
                )
            )
            await asyncio.sleep(0.3)
            assert not pending.done(), "the second answer did not wait for the first's row lock"
            await first.commit()
            await asyncio.wait_for(pending, timeout=5)
            await second.commit()

        async with AsyncSession(engine) as session:
            events = (
                await session.scalars(
                    select(LearningEvent)
                    .where(
                        LearningEvent.kc_id == kc.id,
                        LearningEvent.event_type == "observation",
                    )
                    .order_by(LearningEvent.created_at)
                )
            ).all()
        assert len(events) == 3
        earlier, later = events[1].payload, events[2].payload
        assert later["prior_ability"] == pytest.approx(earlier["posterior_ability"])
    finally:
        await _drop_subject(engine, subject)
```

Add the imports this needs at the top of the file: `import pytest`, `from app.learning import mastery`, `from app.learning.mastery import Observation`.

- [ ] **Step 2: Write the failing ordering test** — in the `record_observation` unit-test file:

```python
async def test_components_are_updated_in_id_order(db_session: AsyncSession) -> None:
    """Two multi-component answers must take their row locks in the same order, or each can
    hold one row the other needs. Sorting by id is that order."""
    learner, kcs = await _learner_with_kcs(db_session, 3)  # use the file's existing helper
    ids = sorted(kc.id for kc in kcs)
    states = await mastery.record_observation(
        db_session,
        Observation(learner_id=learner.id, kc_weights={kc_id: 1.0 for kc_id in reversed(ids)}, score=1.0),
    )
    assert [s.kc_id for s in states] == ids
```

If the file has no helper that makes a learner with several KCs, build them inline the way its other tests do (Learner, Subject, Topic, KCs, flush).

- [ ] **Step 3: Run both and watch them fail**

Run: `uv run pytest tests/test_cross_connection.py -k "waits_for_the_first" -v` → FAIL on `assert not pending.done()`.
Run: `uv run pytest -k test_components_are_updated_in_id_order -v` → FAIL on the order assertion.

- [ ] **Step 4: Implement** — replace `_get_or_create_state`:

```python
async def _get_or_create_state(
    session: AsyncSession, learner_id: uuid.UUID, kc_id: uuid.UUID
) -> LearnerKCState:
    """The learner's state row for this KC, locked for this transaction, created on first
    sighting.

    Locked because every caller reads the row, updates it in Python and writes it back: two
    *different* answers on one component in flight together otherwise both read the same
    prior, and the second write erases the first (S34). ``FOR UPDATE`` makes the second wait
    for the first to commit. ``populate_existing`` makes it then see the committed values: a
    session that already loaded this row — the chat check reads the priors before grading —
    would otherwise be handed its stale identity-map object, lock or no lock.

    Creation goes through ``ON CONFLICT DO NOTHING`` against the (learner, KC) unique
    constraint: two answers arriving together on a KC the learner has never been assessed
    on would both read "no state" and both insert, and one would fail the whole
    transaction. Losing the race here is not an error — it just means someone else created
    the row, so re-read it (locked, like the first read).
    """
    locked = (
        select(LearnerKCState)
        .where(LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == kc_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state = await session.scalar(locked)
    if state is not None:
        return state
    await session.execute(
        pg_insert(LearnerKCState)
        .values(learner_id=learner_id, kc_id=kc_id)
        .on_conflict_do_nothing(index_elements=["learner_id", "kc_id"])
    )
    created = await session.scalar(locked)
    assert created is not None  # the row exists now: we inserted it, or the other writer did
    return created
```

and in `record_observation` change the loop header:

```python
    # Ascending id, so two multi-component answers take their row locks in the same order and
    # neither can hold a row the other is waiting on (S34).
    for kc_id, raw_w in sorted(obs.kc_weights.items()):
```

- [ ] **Step 5: Run the two tests, then the suite**

Run: `uv run pytest tests/test_cross_connection.py -k "waits_for_the_first" -v` and `uv run pytest -k test_components_are_updated_in_id_order -v` → PASS.
Run: `uv run poe check` → green.

- [ ] **Step 6: Commit**

```bash
git add app/learning/mastery.py tests/test_cross_connection.py <the unit-test file>
git status
git commit -m "fix(mastery): lock a component's state while an answer updates it [S34]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Keep a turn's attempt id across retries [S34]

**Files:**
- Modify: `app/services/turn.py`, `app/api/v1/chat.py` (`_build_stream`), `app/services/chat.py` (`run_tutor_turn`, `_resolve_check`), `app/services/workflow.py` (`run_workflow_turn`), `app/agent/workflow.py` (`await_response`, `grade`), `app/agent/state.py` (`WorkflowState`)
- Test: `tests/test_turn.py` (or the file testing `app/services/turn.py`), `tests/test_chat.py`, `tests/test_workflow.py`

**Interfaces:**
- Produces: `turn.attempt_id_for_turn(client_turn_id: uuid.UUID | None) -> uuid.UUID | None`. `run_tutor_turn(..., attempt_id: uuid.UUID | None = None)`, `run_workflow_turn(..., attempt_id: uuid.UUID | None = None)`. `WorkflowState.attempt_id: NotRequired[str | None]`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_turn.py
def test_a_turn_id_maps_to_one_stable_attempt_id() -> None:
    turn_id = uuid.uuid4()
    assert turn_svc.attempt_id_for_turn(turn_id) == turn_svc.attempt_id_for_turn(turn_id)
    assert turn_svc.attempt_id_for_turn(turn_id) != turn_svc.attempt_id_for_turn(uuid.uuid4())
    assert turn_svc.attempt_id_for_turn(turn_id) != turn_id
    assert turn_svc.attempt_id_for_turn(None) is None
```

```python
# tests/test_chat.py — reuse the file's existing setup for a conversation with an open check
# (search for an existing test that calls `_resolve_check` or answers a posed check).
async def test_a_retried_check_answer_is_recorded_once(db_session: AsyncSession) -> None:
    learner, conversation, item = await _conversation_with_open_check(db_session)  # existing helper
    attempt_id = uuid.uuid4()
    llm = fake_llm_client(script=[FakeTurn(text='{"intent": "attempt"}'), FakeTurn(text=_GRADE_JSON)])
    await chat_svc._resolve_check(
        db_session, llm, learner_id=learner.id, conversation=conversation,
        user_content="my answer", attempt_id=attempt_id,
    )
    # The turn failed after grading committed; the retry reaches the same check again.
    conversation.phase, conversation.active_item_id = ConversationPhase.AWAITING_ANSWER, item.id
    llm = fake_llm_client(script=[FakeTurn(text='{"intent": "attempt"}'), FakeTurn(text=_GRADE_JSON)])
    await chat_svc._resolve_check(
        db_session, llm, learner_id=learner.id, conversation=conversation,
        user_content="my answer", attempt_id=attempt_id,
    )
    count = await db_session.scalar(
        select(func.count()).select_from(LearningEvent).where(LearningEvent.attempt_id == attempt_id)
    )
    assert count == len(item.kc_links)
```

Use whatever the file already uses for a scripted grade (`_GRADE_JSON` above stands for that; if the check item is SHORT and graded by rubric, reuse the existing scripted rubric reply). The assertion is the point: one observation per tagged component, not two.

```python
# tests/test_workflow.py — the grade node must pass the attempt id through.
async def test_grading_uses_the_turns_attempt_id(db_session, monkeypatch) -> None:
    seen: list[AnswerSubmit] = []
    real = assessment_svc.answer_item

    async def capture(session, learner_id, item, submission, **kwargs):
        seen.append(submission)
        return await real(session, learner_id, item, submission, **kwargs)

    monkeypatch.setattr(assessment_svc, "answer_item", capture)
    attempt_id = uuid.uuid4()
    # Start guided practice and resume it once, exactly as the file's existing resume test
    # does, passing attempt_id=attempt_id to the resumed run_workflow_turn call.
    ...
    assert [s.attempt_id for s in seen] == [attempt_id]
```

Fill the `...` by copying the start-then-resume sequence from the existing resume test in `tests/test_workflow.py` verbatim, adding only `attempt_id=attempt_id` to the resume call.

- [ ] **Step 2: Run them — FAIL** (`attempt_id_for_turn` missing; unexpected keyword `attempt_id`).

- [ ] **Step 3: Implement**

`app/services/turn.py`:

```python
def attempt_id_for_turn(client_turn_id: uuid.UUID | None) -> uuid.UUID | None:
    """The attempt id for an answer graded during this turn (S34).

    Derived, not stored: a retried turn carries the same ``client_turn_id`` (S51), so it
    derives the same attempt id and ``answer_item``'s idempotency key replays the first grade
    instead of recording the answer twice. A turn has at most one graded answer, so one id per
    turn is enough. ``None`` when the client sent no turn id — exactly today's behaviour.
    """
    return None if client_turn_id is None else uuid.uuid5(client_turn_id, "attempt")
```

`app/api/v1/chat.py` `_build_stream`: compute `attempt_id = turn_svc.attempt_id_for_turn(data.client_turn_id)` once and pass `attempt_id=attempt_id` to both `workflow_svc.run_workflow_turn(...)` and `svc.run_tutor_turn(...)`.

`app/services/chat.py`: add `attempt_id: uuid.UUID | None = None` to `run_tutor_turn` and `_resolve_check`; `run_tutor_turn` passes it to `_resolve_check`; `_resolve_check` builds `AnswerSubmit(response=..., hints_used=..., attempt_id=attempt_id)`.

`app/services/workflow.py` `run_workflow_turn`: add `attempt_id: uuid.UUID | None = None`; in the resume payload add

```python
                # This turn's answer id (S34): a retried turn resumes with the same one, so the
                # grade replays instead of recording the answer twice.
                "attempt_id": str(attempt_id) if attempt_id is not None else None,
```

`app/agent/state.py` `WorkflowState`: add

```python
    # The idempotency key for the answer this round grades (S34), carried in on the resume
    # payload. NotRequired: absent on the opening round and on older checkpoints.
    attempt_id: NotRequired[str | None]
```

`app/agent/workflow.py`: `await_response` returns `"attempt_id": reply.get("attempt_id")` alongside the other fields; `grade` passes

```python
                attempt_id=uuid.UUID(raw) if (raw := state.get("attempt_id")) else None,
```

into its `AnswerSubmit(...)`.

- [ ] **Step 4: Run the three tests, then `uv run poe check`** → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/turn.py app/api/v1/chat.py app/services/chat.py app/services/workflow.py app/agent/workflow.py app/agent/state.py tests/test_turn.py tests/test_chat.py tests/test_workflow.py
git status
git commit -m "fix(chat): give a retried turn's answer the same attempt id [S34]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Serialize prerequisite inserts behind one lock [S23]

**Files:**
- Modify: `app/services/turn_lock.py` (rename `_LOCK_NAMESPACE` → `LOCK_NAMESPACE`, every use), `app/services/knowledge.py` (`add_prerequisite`, new `WouldCreateCycle`), `app/api/v1/knowledge.py:363-371`, `docs/superpowers/specs/2026-09-25-integrity-transfer-design.md` §3.1
- Test: `tests/test_cross_connection.py`, `tests/test_graph_validation.py`

**Interfaces:**
- Produces: `knowledge.add_prerequisite(session, kc_id, prereq_kc_id, weight) -> KCEdge` raises `knowledge.WouldCreateCycle` instead of saving an edge that closes a cycle. `turn_lock.LOCK_NAMESPACE: int`.

- [ ] **Step 1: Failing race test** — `tests/test_cross_connection.py`:

```python
# --- two prerequisite edits that together close a cycle (S23) -----------------------------------


async def test_an_edge_closing_a_cycle_waits_and_is_refused(engine: AsyncEngine) -> None:
    """The cycle check and the insert must not be separable.

    Session one is mid-flight: it has checked and inserted A→B but not committed. Session two
    then asks for B→A. Unlocked, it checked a graph without A→B, found no cycle, and saved —
    two commits, one ring. Locked, it waits, sees A→B once session one commits, and refuses.
    """
    subject, a = await _seed_kc(engine)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        b = KC(topic_id=a.topic_id, slug=f"k-{uuid.uuid4().hex[:8]}", name="Other")
        session.add(b)
        await session.commit()
    try:
        async with (
            AsyncSession(engine, expire_on_commit=False) as first,
            AsyncSession(engine, expire_on_commit=False) as second,
        ):
            await knowledge_svc.lock_edges(first)
            first.add(KCEdge(prereq_kc_id=a.id, kc_id=b.id))
            await first.flush()
            pending = asyncio.create_task(
                knowledge_svc.add_prerequisite(second, a.id, b.id, 1.0)
            )
            await asyncio.sleep(0.3)
            assert not pending.done(), "the second edit did not wait for the edge lock"
            await first.commit()
            with pytest.raises(knowledge_svc.WouldCreateCycle):
                await asyncio.wait_for(pending, timeout=5)
        async with AsyncSession(engine) as session:
            edges = await session.scalar(
                select(func.count()).select_from(KCEdge).where(KCEdge.kc_id.in_([a.id, b.id]))
            )
        assert edges == 1
    finally:
        await _drop_subject(engine, subject)
```

(`add_prerequisite(second, a.id, b.id, 1.0)` asks for **B→A**: its signature is `(kc_id, prereq_kc_id)`, so `kc_id=a`, `prereq=b`.) Import `from app.services import knowledge as knowledge_svc`.

Also in `tests/test_graph_validation.py`, a sequential service test:

```python
async def test_the_service_refuses_an_edge_that_closes_a_cycle(db_session: AsyncSession) -> None:
    a, b = await _two_kcs(db_session)  # the file's existing helper, or build inline
    await knowledge_svc.add_prerequisite(db_session, b.id, a.id, 1.0)  # A→B
    with pytest.raises(knowledge_svc.WouldCreateCycle):
        await knowledge_svc.add_prerequisite(db_session, a.id, b.id, 1.0)  # B→A
```

- [ ] **Step 2: Run — FAIL** (`lock_edges` / `WouldCreateCycle` missing).

- [ ] **Step 3: Implement** — `app/services/turn_lock.py`: rename `_LOCK_NAMESPACE` to `LOCK_NAMESPACE` (docstring unchanged) and update its uses in that file.

`app/services/knowledge.py`, in the prerequisite-edges section:

```python
_EDGE_LOCK = 0x45444745
"""The advisory-lock object id for prerequisite-edge writes ("EDGE"), under the shared
``turn_lock.LOCK_NAMESPACE``."""


class WouldCreateCycle(Exception):
    """The requested prerequisite would close a cycle in the stored graph (S23)."""


async def lock_edges(session: AsyncSession) -> None:
    """Serialize prerequisite-edge inserts until this transaction ends (S23).

    One lock for every edge write rather than one per subject. The cycle check walks the whole
    graph, and a ring can pass through any number of subjects: two inserts each locking only
    their own endpoints' subjects could touch disjoint sets and still close one ring between
    them. Edge edits are rare enough that a single lock costs nothing.

    Transaction-scoped: released by the commit or rollback that ends the insert. Curriculum
    commit and publication do not take it — they only connect components created in the same
    transaction, which no stored edge can reach.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": turn_lock.LOCK_NAMESPACE, "key": _EDGE_LOCK},
    )


async def add_prerequisite(
    session: AsyncSession, kc_id: uuid.UUID, prereq_kc_id: uuid.UUID, weight: float
) -> KCEdge:
    """Declare ``prereq_kc_id`` a prerequisite of ``kc_id``, refusing one that closes a cycle.

    The check runs under :func:`lock_edges`, so no other insert can land between the check
    and this one's commit — which is what let A→B and B→A, submitted together, both pass.
    """
    await lock_edges(session)
    if await would_create_cycle(session, kc_id=kc_id, prereq_kc_id=prereq_kc_id):
        raise WouldCreateCycle(f"{prereq_kc_id} already depends on {kc_id}")
    edge = KCEdge(kc_id=kc_id, prereq_kc_id=prereq_kc_id, weight=weight)
    session.add(edge)
    await session.commit()
    await session.refresh(edge)
    return edge
```

Add `from sqlalchemy import text` and `from app.services import turn_lock` if absent.

`app/api/v1/knowledge.py`: delete the `if await svc.would_create_cycle(...)` block and wrap the call:

```python
    # 409 rather than the 400 a self-prerequisite gets, and the difference is real: a
    # self-loop is wrong in isolation, while this edge is only wrong against the graph that
    # happens to be stored. The check lives in the service, under the edge lock, so two
    # requests cannot each pass it against a graph missing the other's edge (S23).
    try:
        async with _conflict_409(session):
            return await svc.add_prerequisite(session, kc_id, data.prereq_kc_id, data.weight)
    except svc.WouldCreateCycle as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "that prerequisite would create a cycle: the proposed prerequisite already "
            "depends on this knowledge component",
        ) from exc
```

Spec: replace §3.1's first paragraph with the ruling in this plan's *Deviations* item 1 (one lock; commit and publication exempt, and why).

- [ ] **Step 4: Run the new tests and the existing cycle API tests** (`uv run pytest tests/test_graph_validation.py tests/test_cross_connection.py -v`), then `uv run poe check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/turn_lock.py app/services/knowledge.py app/api/v1/knowledge.py tests/test_cross_connection.py tests/test_graph_validation.py docs/superpowers/specs/2026-09-25-integrity-transfer-design.md
git status
git commit -m "fix(graph): check and insert a prerequisite under one lock [S23]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Show a subject's owner its prerequisite conflicts [S23]

**Files:**
- Modify: `frontend/src/api/hooks.ts`, `frontend/src/pages/Lessons.tsx`
- Create: `frontend/src/components/lessons/CurriculumIssuesPanel.tsx`, `frontend/src/components/lessons/CurriculumIssuesPanel.test.tsx`

**Interfaces:**
- Consumes: `GET /api/v1/subjects/{subject_id}/prerequisite-conflicts` → `SacrificedEdgeRead[]` (`prereq_kc_id`, `prereq_name`, `kc_id`, `kc_name`, slugs); `DELETE /api/v1/kcs/{kc_id}/prerequisites/{prereq_kc_id}` → 204.
- Produces: `usePrerequisiteConflicts(subjectId)`, `useRemovePrerequisite(subjectId)`, `<CurriculumIssuesPanel subjectId />`.

- [ ] **Step 1: Failing component test** — `CurriculumIssuesPanel.test.tsx`:

```tsx
import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CurriculumIssuesPanel } from "./CurriculumIssuesPanel";

const conflict = {
  prereq_kc_id: "kc-b", prereq_slug: "b", prereq_name: "Vectors",
  kc_id: "kc-a", kc_slug: "a", kc_name: "Dot product",
};

function stub(conflicts: unknown[]) {
  const calls: { method: string; url: string }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(String(input), init);
    calls.push({ method: req.method, url: req.url });
    if (req.method === "DELETE") return new Response(null, { status: 204 });
    return new Response(JSON.stringify(conflicts), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  }));
  return calls;
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}><CurriculumIssuesPanel subjectId="s-1" /></QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("curriculum issues", () => {
  it("renders nothing when the graph has no conflicts", async () => {
    stub([]);
    const { container } = renderPanel();
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("names the conflict and removes the ignored prerequisite on request", async () => {
    const calls = stub([conflict]);
    renderPanel();
    expect(await screen.findByText(/Dot product requires Vectors/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove this prerequisite" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE" && c.url.endsWith("/kcs/kc-a/prerequisites/kc-b"))).toBe(true),
    );
    // Refetched after the removal.
    await waitFor(() =>
      expect(calls.filter((c) => c.url.endsWith("/prerequisite-conflicts")).length).toBeGreaterThan(1),
    );
  });
});
```

- [ ] **Step 2: Run** `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/components/lessons/CurriculumIssuesPanel.test.tsx` → FAIL (module missing).

- [ ] **Step 3: Implement hooks** — `frontend/src/api/hooks.ts`:

```ts
/** Prerequisite cycles in a subject the caller owns, and the edge the planner ignores to
 * break each one (S23). */
export function usePrerequisiteConflicts(subjectId: string | undefined) {
  return useQuery({
    queryKey: ["prerequisite-conflicts", subjectId],
    enabled: !!subjectId,
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/subjects/{subject_id}/prerequisite-conflicts", {
        params: { path: { subject_id: subjectId! } },
      });
      if (error) throw error;
      return data;
    },
  });
}

/** Remove one prerequisite edge — the repair a conflict report offers (S23). The plan is
 * refetched too: the order it was built from just changed. */
export function useRemovePrerequisite(subjectId: string | undefined) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ kcId, prereqKcId }: { kcId: string; prereqKcId: string }) => {
      const { error } = await api.DELETE("/api/v1/kcs/{kc_id}/prerequisites/{prereq_kc_id}", {
        params: { path: { kc_id: kcId, prereq_kc_id: prereqKcId } },
      });
      if (error) throw error;
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["prerequisite-conflicts", subjectId] });
      queryClient.invalidateQueries({ queryKey: ["lesson-plan", subjectId] });
    },
  });
}
```

- [ ] **Step 4: Implement the panel** — `CurriculumIssuesPanel.tsx`:

```tsx
import { AlertTriangle } from "lucide-react";
import { usePrerequisiteConflicts, useRemovePrerequisite } from "../../api/hooks";

/** Prerequisite cycles in this subject, shown to its owner with a way out (S23).
 *
 * A cycle means two components each claim to come first. The planner already copes by
 * ignoring one edge of the ring; this says which, so the plan's order is explained, and offers
 * to remove that edge for good. Which claim is wrong is the owner's call — the graph cannot
 * know — so nothing is removed without them. Renders nothing when there is nothing wrong. */
export function CurriculumIssuesPanel({ subjectId }: { subjectId: string }) {
  const { data: conflicts } = usePrerequisiteConflicts(subjectId);
  const remove = useRemovePrerequisite(subjectId);
  if (!conflicts || conflicts.length === 0) return null;
  return (
    <section className="flex max-w-2xl flex-col gap-2" aria-label="Curriculum issues">
      <h2 className="text-h3 flex items-center gap-2">
        <AlertTriangle size={16} className="text-warning" /> Curriculum issues
      </h2>
      {conflicts.map((c) => (
        <div
          key={`${c.prereq_kc_id}-${c.kc_id}`}
          className="rounded-field bg-warning/10 flex items-center justify-between gap-3 px-3 py-2"
        >
          <p className="text-body">
            {c.kc_name} requires {c.prereq_name}, but {c.prereq_name} already depends on{" "}
            {c.kc_name}. The plan is ignoring this prerequisite for now.
          </p>
          <button
            type="button"
            className="btn btn-ghost btn-xs shrink-0"
            disabled={remove.isPending}
            onClick={() => remove.mutate({ kcId: c.kc_id, prereqKcId: c.prereq_kc_id })}
          >
            Remove this prerequisite
          </button>
        </div>
      ))}
    </section>
  );
}
```

- [ ] **Step 5: Mount it** — `Lessons.tsx`, inside the existing owner-only block, before `PublishPanel`:

```tsx
      {selected && selected.owner_learner_id !== null && (
        <CurriculumIssuesPanel key={`issues-${selected.id}`} subjectId={selected.id} />
      )}
```

- [ ] **Step 6: Verify** — in `frontend/`: `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run build`, `npm run lint` → green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/hooks.ts frontend/src/pages/Lessons.tsx frontend/src/components/lessons/CurriculumIssuesPanel.tsx frontend/src/components/lessons/CurriculumIssuesPanel.test.tsx
git status
git commit -m "feat(frontend): show a subject's owner its prerequisite conflicts [S23]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Concept links — data, candidates, admin verdicts [S24]

**Files:**
- Create: `db/migrations/versions/0061_concept_links.py`, `app/services/concept_links.py`, `tests/test_concept_links.py`
- Modify: `app/models/knowledge.py` (`ConceptLink`, `ConceptLinkDecision`), `app/models/learning.py` (three `LearnerKCState` columns), `app/models/__init__.py` if models are re-exported there

**Interfaces:**
- Produces:
  - `ConceptLink` (`id`, `kc_a_id`, `kc_b_id`, `scope: str` `"curated"|"private"`, `owner_learner_id`, `verdict: str | None` `"endorsed"|"rejected"`, `endorsed_by: str | None` `"admin"|"judge"`, `decided_by_admin_id`, `reason`, `decided_at`).
  - `ConceptLinkDecision` (`learner_id`, `link_id`, `decision: str` `"accepted"|"declined"|"revoked"`, `decided_at`).
  - `LearnerKCState.transferred_from_kc_id`, `.transferred_at`, `.transfer_confirmed_at`.
  - `concept_links.sync_candidates(session, learner_id: uuid.UUID | None) -> int` (None = curated pairs only).
  - `concept_links.links_in_effect(session, learner_id, kc_ids) -> dict[uuid.UUID, set[uuid.UUID]]`.
  - `concept_links.curated_queue(session) -> list[ConceptLink]`.
  - `concept_links.set_admin_verdict(session, link_id, admin_id, *, endorse: bool, reason: str) -> ConceptLink`; raises `LinkNotFound`, `LinkAlreadyDecided`.
  - `concept_links.LinkNotFound`, `LinkAlreadyDecided`, `LinkConflict` exceptions.

- [ ] **Step 1: Failing tests** — `tests/test_concept_links.py`:

```python
"""Concept links: which pairs are candidates, and how a link comes into effect (S24)."""

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, Concept, ConceptLink, ConceptLinkDecision, Subject, Topic
from app.models.learner import Learner
from app.services import concept_links as svc


async def _subject(session, *, name, owner=None) -> Subject:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name=name, owner_learner_id=owner)
    session.add(subject)
    await session.flush()
    return subject


async def _kc(session, subject: Subject, name: str, concept: Concept | None) -> KC:
    topic = Topic(subject_id=subject.id, slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
    session.add(topic)
    await session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{uuid.uuid4().hex[:8]}", name=name,
            concept_id=concept.id if concept else None)
    session.add(kc)
    await session.flush()
    return kc


async def _concept(session, key: str) -> Concept:
    concept = Concept(key=f"{key}-{uuid.uuid4().hex[:6]}", name=key)
    session.add(concept)
    await session.flush()
    return concept


async def _learner(session) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def test_two_curated_presentations_of_one_concept_become_a_curated_candidate(db_session):
    concept = await _concept(db_session, "derivatives")
    calc = await _subject(db_session, name="Calculus")
    phys = await _subject(db_session, name="Physics")
    a = await _kc(db_session, calc, "Derivatives", concept)
    b = await _kc(db_session, phys, "Derivatives", concept)
    assert await svc.sync_candidates(db_session, None) == 1
    (link,) = await svc.curated_queue(db_session)
    assert {link.kc_a_id, link.kc_b_id} == {a.id, b.id}
    assert link.kc_a_id < link.kc_b_id
    assert (link.scope, link.owner_learner_id, link.verdict) == ("curated", None, None)
    assert await svc.sync_candidates(db_session, None) == 0  # idempotent


async def test_a_private_pair_belongs_to_its_learner_and_never_spans_two_learners(db_session):
    concept = await _concept(db_session, "vectors")
    one, two = await _learner(db_session), await _learner(db_session)
    mine = await _subject(db_session, name="Mine", owner=one.id)
    theirs = await _subject(db_session, name="Theirs", owner=two.id)
    curated = await _subject(db_session, name="Library")
    await _kc(db_session, mine, "Vectors", concept)
    await _kc(db_session, theirs, "Vectors", concept)
    await _kc(db_session, curated, "Vectors", concept)
    # One's view: mine+library. Two's view: theirs+library. Never mine+theirs.
    assert await svc.sync_candidates(db_session, one.id) == 1
    assert await svc.sync_candidates(db_session, two.id) == 1
    links = await svc.links_for_learner(db_session, one.id)
    assert [(l.scope, l.owner_learner_id) for l in links] == [("private", one.id)]


async def test_components_in_the_same_subject_are_never_candidates(db_session):
    concept = await _concept(db_session, "limits")
    calc = await _subject(db_session, name="Calculus")
    await _kc(db_session, calc, "Limits", concept)
    await _kc(db_session, calc, "Limits", concept)
    assert await svc.sync_candidates(db_session, None) == 0


async def test_a_link_is_in_effect_only_when_endorsed_and_accepted(db_session):
    learner = await _learner(db_session)
    concept = await _concept(db_session, "sets")
    a = await _kc(db_session, await _subject(db_session, name="A"), "Sets", concept)
    b = await _kc(db_session, await _subject(db_session, name="B"), "Sets", concept)
    await svc.sync_candidates(db_session, None)
    (link,) = await svc.curated_queue(db_session)
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}
    db_session.add(ConceptLinkDecision(learner_id=learner.id, link_id=link.id, decision="accepted"))
    await db_session.flush()
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}  # not endorsed
    admin = await _learner(db_session)
    await svc.set_admin_verdict(db_session, link.id, admin.id, endorse=True, reason="Same idea.")
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {a.id: {b.id}}
    assert await svc.links_in_effect(db_session, learner.id, [b.id]) == {b.id: {a.id}}


async def test_an_admin_decides_a_curated_link_once_and_never_a_private_one(db_session):
    learner, admin = await _learner(db_session), await _learner(db_session)
    concept = await _concept(db_session, "graphs")
    await _kc(db_session, await _subject(db_session, name="A"), "Graphs", concept)
    await _kc(db_session, await _subject(db_session, name="B", owner=learner.id), "Graphs", concept)
    await _kc(db_session, await _subject(db_session, name="C"), "Graphs", concept)
    await svc.sync_candidates(db_session, learner.id)
    private = next(l for l in await svc.links_for_learner(db_session, learner.id) if l.scope == "private")
    curated = (await svc.curated_queue(db_session))[0]
    with pytest.raises(svc.LinkNotFound):
        await svc.set_admin_verdict(db_session, private.id, admin.id, endorse=True, reason="x")
    decided = await svc.set_admin_verdict(db_session, curated.id, admin.id, endorse=False, reason="Different use.")
    assert (decided.verdict, decided.endorsed_by, decided.decided_by_admin_id) == ("rejected", "admin", admin.id)
    with pytest.raises(svc.LinkAlreadyDecided):
        await svc.set_admin_verdict(db_session, curated.id, admin.id, endorse=True, reason="y")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_concept_links.py -v` → FAIL (imports).

- [ ] **Step 3: Models** — `app/models/knowledge.py`, after `KCEdge`:

```python
class ConceptLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A claim that two presentations in different subjects are the same idea (S24).

    A shared concept key makes a pair a *candidate*; it is this row, endorsed and then accepted
    by a learner, that lets evidence carry across. Two agreements, deliberately: the endorser
    (an administrator for a curated pair, the LLM judge for a pair touching a learner's own
    material) says the two are the same idea, and the learner says it applies to them. Neither
    alone links anything — see ``concept_links.links_in_effect``.
    """

    __tablename__ = "concept_links"
    __table_args__ = (
        UniqueConstraint("kc_a_id", "kc_b_id"),
        CheckConstraint("kc_a_id < kc_b_id", name="ck_concept_links_ordered"),
    )

    kc_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    kc_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kcs.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str]  # "curated" | "private"
    # The learner whose subjects a private pair spans; NULL for a curated pair.
    owner_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), default=None, index=True
    )
    verdict: Mapped[str | None] = mapped_column(default=None)  # "endorsed" | "rejected" | NULL
    endorsed_by: Mapped[str | None] = mapped_column(default=None)  # "admin" | "judge"
    decided_by_admin_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), default=None
    )
    reason: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ConceptLinkDecision(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One learner's answer to one endorsed link (S24): accepted, declined, or revoked."""

    __tablename__ = "concept_link_decisions"
    __table_args__ = (UniqueConstraint("learner_id", "link_id"),)

    learner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("learners.id", ondelete="CASCADE"), index=True
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("concept_links.id", ondelete="CASCADE"), index=True
    )
    decision: Mapped[str]  # "accepted" | "declined" | "revoked"
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
```

Add the needed imports (`CheckConstraint`, `DateTime`, `datetime`, `UTC`) if the module lacks them.

`app/models/learning.py` `LearnerKCState`, after `achieved_at`:

```python
    # A head start carried over an accepted concept link (S24): the component it came from,
    # when, and when a run of unaided passes here confirmed it. A state with `transferred_at`
    # set and `transfer_confirmed_at` NULL is *provisional* — see `mastery.is_provisional`.
    transferred_from_kc_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kcs.id", ondelete="SET NULL"), default=None
    )
    transferred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    transfer_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

- [ ] **Step 4: Migration** — `db/migrations/versions/0061_concept_links.py`:

```python
"""Concept links, learner decisions on them, and transfer columns on learner_kc_state (S24).

A link is a claim that two presentations in different subjects are the same idea, endorsed by
an administrator (curated pairs) or the LLM judge (pairs touching a learner's own material),
and in effect for a learner only once that learner accepts it. The three state columns record
a head start carried over such a link, which stays provisional until confirmed.

No backfill: nothing has ever been linked.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_concept_links"
down_revision: str | Sequence[str] | None = "0060_practice_pause"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "concept_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kc_a_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kcs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kc_b_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kcs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("owner_learner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("learners.id", ondelete="CASCADE"), nullable=True),
        sa.Column("verdict", sa.String(), nullable=True),
        sa.Column("endorsed_by", sa.String(), nullable=True),
        sa.Column("decided_by_admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("learners.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("kc_a_id", "kc_b_id"),
        sa.CheckConstraint("kc_a_id < kc_b_id", name="ck_concept_links_ordered"),
    )
    op.create_index("ix_concept_links_kc_a_id", "concept_links", ["kc_a_id"])
    op.create_index("ix_concept_links_kc_b_id", "concept_links", ["kc_b_id"])
    op.create_index("ix_concept_links_owner_learner_id", "concept_links", ["owner_learner_id"])
    op.create_table(
        "concept_link_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("learner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("learners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("link_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("concept_links.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("learner_id", "link_id"),
    )
    op.create_index("ix_concept_link_decisions_learner_id", "concept_link_decisions", ["learner_id"])
    op.create_index("ix_concept_link_decisions_link_id", "concept_link_decisions", ["link_id"])
    op.add_column("learner_kc_state", sa.Column("transferred_from_kc_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("kcs.id", ondelete="SET NULL"), nullable=True))
    op.add_column("learner_kc_state", sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("learner_kc_state", sa.Column("transfer_confirmed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("learner_kc_state", "transfer_confirmed_at")
    op.drop_column("learner_kc_state", "transferred_at")
    op.drop_column("learner_kc_state", "transferred_from_kc_id")
    op.drop_table("concept_link_decisions")
    op.drop_table("concept_links")
```

Before writing it, open the most recent migration that created a table (e.g. `grep -l create_table db/migrations/versions/*.py | tail -1`) and match its `id`/timestamp column spelling exactly — the mixins' defaults must agree with the migration or `poe check`'s schema-drift test fails. Format the long lines with `uv run poe format`.

- [ ] **Step 5: Service** — `app/services/concept_links.py`:

```python
"""Concept links between presentations in different subjects (S24).

A pair of KCs sharing a concept key (``knowledge.concept_key``) is a *candidate* — the name
only makes the pair worth a look. A link comes into effect for a learner only when it has been
**endorsed** (an administrator for a curated pair; the LLM judge for a pair touching the
learner's own material) **and** that learner has **accepted** it. ``links_in_effect`` is the
one reading of that rule; nothing else re-derives it.
"""

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from itertools import combinations

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KC, ConceptLink, ConceptLinkDecision, Subject, Topic

CURATED, PRIVATE = "curated", "private"
ENDORSED, REJECTED = "endorsed", "rejected"
ACCEPTED, DECLINED, REVOKED = "accepted", "declined", "revoked"


class LinkNotFound(Exception):
    """No such link — or not one this caller may see or act on. One answer for both."""


class LinkAlreadyDecided(Exception):
    """An endorser has already ruled on this link."""


class LinkConflict(Exception):
    """The learner's decision does not follow from the one they already made."""


async def sync_candidates(session: AsyncSession, learner_id: uuid.UUID | None) -> int:
    """Record every candidate pair visible from ``learner_id``'s point of view; return how many
    were new.

    ``None`` is the curated library alone. A learner sees the library plus their own subjects,
    so their candidates are library–own and own–own pairs; library–library pairs are recorded
    too (as curated) since they are candidates for everyone. A pair only ever spans subjects one
    learner can see — two learners' private material is never paired.
    """
    visible = Subject.owner_learner_id.is_(None)
    if learner_id is not None:
        visible = or_(visible, Subject.owner_learner_id == learner_id)
    rows = (
        await session.execute(
            select(KC.id, KC.concept_id, Subject.id, Subject.owner_learner_id)
            .join(Topic, KC.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .where(KC.concept_id.is_not(None), visible)
        )
    ).all()
    by_concept: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID, uuid.UUID | None]]] = defaultdict(list)
    for kc_id, concept_id, subject_id, owner in rows:
        by_concept[concept_id].append((kc_id, subject_id, owner))
    values = []
    for members in by_concept.values():
        for (kc1, s1, o1), (kc2, s2, o2) in combinations(members, 2):
            if s1 == s2:
                continue
            a, b = sorted((kc1, kc2))
            curated = o1 is None and o2 is None
            values.append(
                {
                    "id": uuid.uuid4(),
                    "kc_a_id": a,
                    "kc_b_id": b,
                    "scope": CURATED if curated else PRIVATE,
                    "owner_learner_id": None if curated else (o1 or o2),
                }
            )
    if not values:
        return 0
    result = await session.execute(
        pg_insert(ConceptLink)
        .values(values)
        .on_conflict_do_nothing(index_elements=["kc_a_id", "kc_b_id"])
        .returning(ConceptLink.id)
    )
    return len(result.all())


def _visible_link(learner_id: uuid.UUID):
    """A link this learner may see: curated, or private and theirs."""
    return or_(ConceptLink.scope == CURATED, ConceptLink.owner_learner_id == learner_id)


async def links_for_learner(session: AsyncSession, learner_id: uuid.UUID) -> list[ConceptLink]:
    return list(
        await session.scalars(
            select(ConceptLink).where(_visible_link(learner_id)).order_by(ConceptLink.created_at)
        )
    )


async def links_in_effect(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """For each of ``kc_ids`` with at least one link in effect for this learner, the KCs it is
    linked to. In effect means endorsed **and** accepted by this learner — the whole rule."""
    ids = list(kc_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ConceptLink.kc_a_id, ConceptLink.kc_b_id)
            .join(ConceptLinkDecision, ConceptLinkDecision.link_id == ConceptLink.id)
            .where(
                ConceptLink.verdict == ENDORSED,
                ConceptLinkDecision.learner_id == learner_id,
                ConceptLinkDecision.decision == ACCEPTED,
                or_(ConceptLink.kc_a_id.in_(ids), ConceptLink.kc_b_id.in_(ids)),
            )
        )
    ).all()
    wanted = set(ids)
    out: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for a, b in rows:
        if a in wanted:
            out[a].add(b)
        if b in wanted:
            out[b].add(a)
    return dict(out)


async def curated_queue(session: AsyncSession) -> list[ConceptLink]:
    """Curated links, undecided first, oldest first within each — the order a reviewer works in."""
    return list(
        await session.scalars(
            select(ConceptLink)
            .where(ConceptLink.scope == CURATED)
            .order_by(ConceptLink.verdict.is_not(None), ConceptLink.created_at)
        )
    )


async def set_admin_verdict(
    session: AsyncSession, link_id: uuid.UUID, admin_id: uuid.UUID, *, endorse: bool, reason: str
) -> ConceptLink:
    """An administrator's ruling on a curated link. Once only; private links are the judge's."""
    link = await session.get(ConceptLink, link_id)
    if link is None or link.scope != CURATED:
        raise LinkNotFound(str(link_id))
    if link.verdict is not None:
        raise LinkAlreadyDecided(str(link_id))
    link.verdict = ENDORSED if endorse else REJECTED
    link.endorsed_by = "admin"
    link.decided_by_admin_id = admin_id
    link.reason = reason
    link.decided_at = datetime.now(UTC)
    await session.flush()
    return link
```

(`and_` is used by later tasks in this module; drop it from the import if ruff flags it now.)

- [ ] **Step 6: Run** `uv run pytest tests/test_concept_links.py -v` → PASS; `uv run poe db-upgrade` against the dev DB is not required, but `uv run poe check` must be green (its migration test applies 0061).

- [ ] **Step 7: Commit**

```bash
git add db/migrations/versions/0061_concept_links.py app/models/knowledge.py app/models/learning.py app/services/concept_links.py tests/test_concept_links.py
git status
git commit -m "feat(knowledge): record candidate concept links and admin verdicts on them [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The LLM judge for private pairs, run in the background [S24]

**Files:**
- Create: `app/learning/link_judge.py`, `tests/test_link_judge.py`
- Modify: `app/services/concept_links.py` (`judge_pending`), `app/workers/tasks.py`, `app/api/deps.py`, `app/api/v1/knowledge.py` (`commit_subject`), `tests/conftest.py` (default enqueuer override), `tests/test_visibility_sweep.py` (override list)

**Interfaces:**
- Consumes: Task 5's `sync_candidates`, `ConceptLink`.
- Produces: `link_judge.JUDGE_ROLE = ModelRole.SMART`; `link_judge.Side` (`kc_name`, `description`, `topic_name`, `subject_name`, `prerequisites: list[str]`, `dependents: list[str]`); `link_judge.Verdict(endorse: bool, reason: str)`; `link_judge.judge_pair(client, a: Side, b: Side) -> tuple[Verdict | None, Usage]`; `link_judge.parse_verdict(content: str) -> Verdict | None`; `concept_links.judge_pending(session, llm, learner_id) -> int`; `deps.ConceptLinkJudgeEnqueuer`, `deps.get_concept_link_judge_enqueuer`, `deps.ConceptLinkJudgeEnqueuerDep`; `tasks.judge_concept_links_task`.

- [ ] **Step 1: Failing tests** — `tests/test_link_judge.py`:

```python
"""The concept-link judge: what it sees, what it concludes, how it fails (S24)."""

import uuid

from app.learning import link_judge
from app.llm.providers.fake import FakeTurn
from app.llm.registry import fake_llm_client
from app.models.knowledge import ConceptLink, KCEdge
from app.services import concept_links as svc
from tests.test_concept_links import _concept, _kc, _learner, _subject


def test_a_verdict_is_read_out_of_the_models_reply() -> None:
    assert link_judge.parse_verdict('{"verdict": "endorse", "reason": "Same rule."}') == (
        link_judge.Verdict(endorse=True, reason="Same rule.")
    )
    assert link_judge.parse_verdict('noise {"verdict": "reject", "reason": "Different."} tail') == (
        link_judge.Verdict(endorse=False, reason="Different.")
    )


def test_an_unreadable_reply_is_no_verdict_rather_than_a_rejection() -> None:
    for bad in ("", "sure!", '{"verdict": "maybe"}', '{"verdict": "endorse"}', "[1]"):
        assert link_judge.parse_verdict(bad) is None


async def _private_pair(db_session):
    learner = await _learner(db_session)
    concept = await _concept(db_session, "derivatives")
    own = await _subject(db_session, name="My Physics", owner=learner.id)
    lib = await _subject(db_session, name="Calculus")
    a = await _kc(db_session, own, "Derivatives", concept)
    b = await _kc(db_session, lib, "Derivatives", concept)
    return learner, a, b


async def test_an_endorsement_is_recorded_with_its_reason(db_session) -> None:
    learner, a, b = await _private_pair(db_session)
    llm = fake_llm_client(script=[FakeTurn(text='{"verdict": "endorse", "reason": "Same rate of change."}')])
    assert await svc.judge_pending(db_session, llm, learner.id) == 1
    (link,) = await svc.links_for_learner(db_session, learner.id)
    assert (link.verdict, link.endorsed_by, link.reason) == ("endorsed", "judge", "Same rate of change.")


async def test_a_failed_judgement_leaves_the_pair_for_the_next_run(db_session) -> None:
    learner, _a, _b = await _private_pair(db_session)
    assert await svc.judge_pending(db_session, fake_llm_client(script=[FakeTurn(text="??")]), learner.id) == 0
    (link,) = await svc.links_for_learner(db_session, learner.id)
    assert link.verdict is None
    llm = fake_llm_client(script=[FakeTurn(text='{"verdict": "reject", "reason": "Different."}')])
    assert await svc.judge_pending(db_session, llm, learner.id) == 1


async def test_the_judge_is_told_nothing_from_another_learners_material(db_session) -> None:
    learner, a, b = await _private_pair(db_session)
    stranger = await _learner(db_session)
    other = await _subject(db_session, name="Stranger's notes", owner=stranger.id)
    secret = await _kc(db_session, other, "Secret prerequisite", None)
    db_session.add(KCEdge(prereq_kc_id=secret.id, kc_id=b.id))  # a hand-made edge into the library
    await db_session.flush()
    llm = fake_llm_client(script=[FakeTurn(text='{"verdict": "endorse", "reason": "ok"}')])
    await svc.judge_pending(db_session, llm, learner.id)
    prompt = llm.provider.calls[-1]  # adapt to how FakeProvider records calls; see app/llm/providers/fake.py
    assert "Secret prerequisite" not in str(prompt)
    assert "Stranger's notes" not in str(prompt)
```

Check `app/llm/providers/fake.py` for how the fake records the messages it was sent and adjust the `prompt` line to it; if it records nothing, add a `calls: list[...]` attribute that `complete` appends `(system, messages)` to (a test-only affordance on a test-only provider).

- [ ] **Step 2: Run** `uv run pytest tests/test_link_judge.py -v` → FAIL.

- [ ] **Step 3: The judge** — `app/learning/link_judge.py`:

```python
"""Whether two presentations in different subjects are the same idea (S24).

Used only for pairs touching a learner's own material, where there is no administrator to ask
and no administrator should read the material. Its endorsement is half of a link; the
learner's acceptance is the other half, so a wrong endorsement costs one declined suggestion,
not a silently merged estimate.
"""

import json
from dataclasses import dataclass, field

from app.llm.base import LLMClient
from app.llm.registry import ModelRole
from app.llm.types import ChatMessage, ChatRole, Usage

JUDGE_ROLE = ModelRole.SMART
"""A judgement about meaning across contexts, with a learner's trust riding on it — not FAST."""

_SYSTEM_PROMPT = (
    "You decide whether two knowledge components, taught in two different subjects, are the "
    "same underlying idea — so that someone who has demonstrated one has demonstrated most of "
    "the other. Sharing a name is not enough: 'Functions' in calculus and 'Functions' in "
    "programming share a name and little else. Reply with JSON only: "
    '{"verdict": "endorse" | "reject", "reason": "<one sentence a learner will read>"}'
)


@dataclass(frozen=True)
class Side:
    kc_name: str
    description: str | None
    topic_name: str
    subject_name: str
    prerequisites: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    endorse: bool
    reason: str


def _describe(label: str, side: Side) -> str:
    lines = [
        f"{label}: {side.kc_name}",
        f"  Subject: {side.subject_name}; topic: {side.topic_name}",
        f"  Description: {side.description or '(none)'}",
        f"  Builds on: {', '.join(side.prerequisites) or '(nothing listed)'}",
        f"  Leads to: {', '.join(side.dependents) or '(nothing listed)'}",
    ]
    return "\n".join(lines)


def parse_verdict(content: str) -> Verdict | None:
    """The model's verdict, or ``None`` when the reply cannot be read as one. ``None`` is not a
    rejection: the pair stays unjudged and is asked again next run."""
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    verdict, reason = payload.get("verdict"), payload.get("reason")
    if verdict not in ("endorse", "reject") or not isinstance(reason, str) or not reason.strip():
        return None
    return Verdict(endorse=verdict == "endorse", reason=reason.strip())


async def judge_pair(client: LLMClient, a: Side, b: Side) -> tuple[Verdict | None, Usage]:
    """Ask the model. Never raises: a provider failure is no verdict, like an unreadable one."""
    try:
        completion = await client.complete(
            JUDGE_ROLE,
            [ChatMessage(role=ChatRole.USER, content=f"{_describe('A', a)}\n\n{_describe('B', b)}")],
            system=_SYSTEM_PROMPT,
            max_tokens=200,
        )
    except Exception:
        return None, Usage()
    return parse_verdict(completion.content), completion.usage
```

Match the import paths for `LLMClient`, `ModelRole`, `ChatMessage`, `ChatRole`, `Usage` to what `app/learning/conversation_evidence.py` imports.

- [ ] **Step 4: `judge_pending`** — append to `app/services/concept_links.py`:

```python
async def _side(session: AsyncSession, kc_id: uuid.UUID, visible_subjects: set[uuid.UUID]) -> link_judge.Side | None:
    """What the judge is told about one side — its own subject's facts only, and neighbours
    only from subjects the learner can see."""
    row = (
        await session.execute(
            select(KC, Topic, Subject)
            .join(Topic, KC.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .where(KC.id == kc_id)
        )
    ).first()
    if row is None:
        return None
    kc, topic, subject = row

    async def neighbours(near, far) -> list[str]:
        names = await session.scalars(
            select(KC.name)
            .join(KCEdge, far == KC.id)
            .join(Topic, KC.topic_id == Topic.id)
            .where(near == kc_id, Topic.subject_id.in_(visible_subjects))
            .order_by(KC.name)
        )
        return list(names)

    return link_judge.Side(
        kc_name=kc.name,
        description=kc.description,
        topic_name=topic.name,
        subject_name=subject.name,
        prerequisites=await neighbours(KCEdge.kc_id, KCEdge.prereq_kc_id),
        dependents=await neighbours(KCEdge.prereq_kc_id, KCEdge.kc_id),
    )


async def judge_pending(session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID) -> int:
    """Judge this learner's unjudged private candidates; return how many got a verdict.

    Commits after each verdict, so a run that dies halfway keeps what it decided. A pair the
    judge could not decide stays unjudged for the next run — never recorded as a rejection.
    """
    await sync_candidates(session, learner_id)
    await session.commit()
    visible_subjects = set(
        await session.scalars(
            select(Subject.id).where(
                or_(Subject.owner_learner_id.is_(None), Subject.owner_learner_id == learner_id)
            )
        )
    )
    pending = list(
        await session.scalars(
            select(ConceptLink).where(
                ConceptLink.scope == PRIVATE,
                ConceptLink.owner_learner_id == learner_id,
                ConceptLink.verdict.is_(None),
            )
        )
    )
    decided = 0
    for link in pending:
        a = await _side(session, link.kc_a_id, visible_subjects)
        b = await _side(session, link.kc_b_id, visible_subjects)
        if a is None or b is None:
            continue
        verdict, usage = await link_judge.judge_pair(llm, a, b)
        if usage.input_tokens or usage.output_tokens:
            await log_llm_call(
                learner_id=learner_id,
                role=link_judge.JUDGE_ROLE.value,
                spec=llm.spec(link_judge.JUDGE_ROLE),
                usage=usage,
            )
        if verdict is None:
            log.warning("concept_links.judge_undecided", link_id=str(link.id))
            continue
        link.verdict = ENDORSED if verdict.endorse else REJECTED
        link.endorsed_by = "judge"
        link.reason = verdict.reason
        link.decided_at = datetime.now(UTC)
        await session.commit()
        decided += 1
    return decided
```

Imports to add: `structlog` (`log = structlog.get_logger(__name__)`), `from app.learning import link_judge`, `from app.llm.base import LLMClient` (match the codebase path), `from app.models.knowledge import KCEdge`, `from app.services.llm_log import log_llm_call`.

- [ ] **Step 5: Worker and enqueue**

`app/workers/tasks.py`:

```python
async def _judge_concept_links_task(learner_id: str) -> None:
    """Judge a learner's new concept-link candidates (enqueued when they commit a subject)."""
    llm = build_llm_client(get_settings())
    async with SessionFactory() as session:
        await concept_links_svc.judge_pending(session, llm, uuid.UUID(learner_id))
```

and beside the other registrations: `judge_concept_links_task = broker.task(_judge_concept_links_task)`; import `from app.services import concept_links as concept_links_svc`.

`app/api/deps.py`, following the memory write-back pattern:

```python
ConceptLinkJudgeEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def _enqueue_concept_link_judge(learner_id: uuid.UUID) -> None:
    from app.workers.tasks import judge_concept_links_task  # lazy: avoids an import cycle

    await judge_concept_links_task.kiq(str(learner_id))


def get_concept_link_judge_enqueuer() -> ConceptLinkJudgeEnqueuer:
    """Returns the callable that queues concept-link judging for a learner. Overridden in tests."""
    return _enqueue_concept_link_judge


ConceptLinkJudgeEnqueuerDep = Annotated[
    ConceptLinkJudgeEnqueuer, Depends(get_concept_link_judge_enqueuer)
]
```

`app/api/v1/knowledge.py` `commit_subject`: add parameter `judge: ConceptLinkJudgeEnqueuerDep` and, after the retag loop:

```python
    # A new subject can share concepts with the learner's others and the library. Judging a
    # pair is a model call each, so it runs in the background; until it lands there are simply
    # no suggestions yet (S24).
    await judge(learner.id)
```

`tests/conftest.py` — where `api_client` sets `dependency_overrides`, add a no-op default so no test reaches the in-memory broker by accident:

```python
    async def _no_judge(_learner_id: uuid.UUID) -> None:
        return None

    app.dependency_overrides[get_concept_link_judge_enqueuer] = lambda: _no_judge
```

`tests/test_visibility_sweep.py`: add `get_concept_link_judge_enqueuer` to its override tuple the same way it handles `get_retag_enqueuer` (a recording no-op). Also add a test in `tests/test_knowledge_api.py` (or wherever `commit_subject` is tested) that overrides the enqueuer with a recorder and asserts one call with the learner's id after a successful commit.

- [ ] **Step 6: Run** `uv run pytest tests/test_link_judge.py tests/test_concept_links.py -v`, then `uv run poe check` → green.

- [ ] **Step 7: Commit**

```bash
git add app/learning/link_judge.py app/services/concept_links.py app/workers/tasks.py app/api/deps.py app/api/v1/knowledge.py tests/conftest.py tests/test_visibility_sweep.py tests/test_link_judge.py <commit_subject test file> <fake provider if changed>
git status
git commit -m "feat(knowledge): judge a learner's private concept-link candidates in the background [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Head starts, and provisional until confirmed [S24]

**Files:**
- Modify: `app/core/config.py`, `app/learning/mastery.py`, `app/services/lesson_plan.py` (`mastered_kc_ids`, `goal_status`), `app/services/analytics.py`, `tests/eval/reliability/knobs.py`, `tests/eval/datasets/mine.py`
- Create: `tests/test_transfer.py`

**Interfaces:**
- Consumes: Task 5's `LearnerKCState` transfer columns.
- Produces:
  - Settings `transfer_uncertainty_floor: float = 0.6`, `transfer_confirm_passes: int = 2`.
  - `mastery.TRANSFER_SEED_EVENT = "transfer_seed"`, `mastery.TRANSFER_REVOKED_EVENT = "transfer_revoked"`.
  - `mastery.is_provisional(state: LearnerKCState) -> bool`.
  - `mastery.seed_transfer(session, learner_id, *, target_kc_id, source_kc_id, link_id, now=None, estimator=DEFAULT_ESTIMATOR) -> LearnerKCState | None`.
  - `mastery.revoke_transfer(session, learner_id, *, target_kc_id, source_kc_id, link_id) -> bool`.
  - `mastery.provisional_kc_ids(session, learner_id, kc_ids) -> set[uuid.UUID]`.
  - `KCStanding.provisional: bool = False`.

- [ ] **Step 1: Failing tests** — `tests/test_transfer.py`:

```python
"""A head start carried over an accepted concept link, and what confirms it (S24)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning import mastery
from app.learning.mastery import Observation
from app.models.learning import LearnerKCState, LearningEvent
from app.services import lesson_plan as lesson_plan_svc
from tests.test_concept_links import _kc, _learner, _subject


async def _measured(session, learner, kc, ability, uncertainty):
    session.add(LearnerKCState(learner_id=learner.id, kc_id=kc.id, ability=ability,
                               uncertainty=uncertainty, last_seen_at=datetime.now(UTC)))
    await session.flush()


async def _pair(session):
    learner = await _learner(session)
    source = await _kc(session, await _subject(session, name="Calculus"), "Derivatives", None)
    target = await _kc(session, await _subject(session, name="Physics"), "Derivatives", None)
    return learner, source, target


async def _answer(session, learner, kc, score, item=None):
    await mastery.record_observation(
        session, Observation(learner_id=learner.id, kc_weights={kc.id: 1.0}, score=score, item_id=item)
    )


async def _state(session, learner, kc) -> LearnerKCState:
    return await session.scalar(select(LearnerKCState).where(
        LearnerKCState.learner_id == learner.id, LearnerKCState.kc_id == kc.id))


async def test_a_seed_takes_the_source_estimate_with_widened_uncertainty(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    state = await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id,
                                        source_kc_id=source.id, link_id=link)
    assert state is not None
    assert state.ability == 2.0
    assert state.uncertainty == get_settings().transfer_uncertainty_floor
    assert state.last_seen_at is None and mastery.is_provisional(state)
    event = await db_session.scalar(select(LearningEvent).where(
        LearningEvent.kc_id == target.id, LearningEvent.event_type == mastery.TRANSFER_SEED_EVENT))
    assert event.payload["source_kc_id"] == str(source.id) and event.payload["link_id"] == str(link)
    # The source is untouched.
    assert (await _state(db_session, learner, source)).uncertainty == 0.3


async def test_real_evidence_on_the_target_is_never_overwritten(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await _measured(db_session, learner, target, -0.5, 0.7)
    assert await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id,
                                       source_kc_id=source.id, link_id=uuid.uuid4()) is None
    assert (await _state(db_session, learner, target)).ability == -0.5


async def test_an_unmeasured_source_seeds_nothing(db_session):
    learner, source, target = await _pair(db_session)
    assert await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id,
                                       source_kc_id=source.id, link_id=uuid.uuid4()) is None


async def test_the_strongest_source_wins(db_session):
    learner, weak, target = await _pair(db_session)
    strong = await _kc(db_session, await _subject(db_session, name="Mechanics"), "Derivatives", None)
    await _measured(db_session, learner, weak, 1.0, 0.3)
    await _measured(db_session, learner, strong, 2.5, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=strong.id, link_id=uuid.uuid4())
    assert await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=weak.id, link_id=uuid.uuid4()) is None
    assert (await _state(db_session, learner, target)).transferred_from_kc_id == strong.id


async def test_a_wrong_first_answer_is_never_mastery(db_session):
    """Review focus 2: 2.0/0.6 → 1.69/0.59 after a miss still clears the bar numerically."""
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4())
    await _answer(db_session, learner, target, 0.0)
    assert target.id not in await lesson_plan_svc.mastered_kc_ids(db_session, learner.id, [target.id])
    assert (await _state(db_session, learner, target)).achieved_at is None


async def test_one_pass_is_not_enough_and_two_on_different_questions_confirm(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4())
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    assert mastery.is_provisional(await _state(db_session, learner, target))
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    state = await _state(db_session, learner, target)
    assert not mastery.is_provisional(state) and state.transfer_confirmed_at is not None
    assert target.id in await lesson_plan_svc.mastered_kc_ids(db_session, learner.id, [target.id])


async def test_the_same_question_twice_confirms_nothing(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4())
    item = uuid.uuid4()
    await _answer(db_session, learner, target, 1.0, item=item)
    await _answer(db_session, learner, target, 1.0, item=item)
    assert mastery.is_provisional(await _state(db_session, learner, target))


async def test_a_failure_restarts_the_run(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4())
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    await _answer(db_session, learner, target, 0.0, item=uuid.uuid4())
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    assert mastery.is_provisional(await _state(db_session, learner, target))


async def test_revoking_before_any_answer_removes_the_head_start(db_session):
    """Review focus 3."""
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link)
    assert await mastery.revoke_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link)
    state = await _state(db_session, learner, target)
    assert (state.ability, state.uncertainty, state.transferred_at) == (0.0, 1.0, None)
    assert target.id not in await mastery.provisional_kc_ids(db_session, learner.id, [target.id])


async def test_revoking_after_answers_keeps_the_estimate(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    link = uuid.uuid4()
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link)
    await _answer(db_session, learner, target, 1.0, item=uuid.uuid4())
    before = (await _state(db_session, learner, target)).ability
    assert not await mastery.revoke_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=link)
    state = await _state(db_session, learner, target)
    assert state.ability == before and mastery.is_provisional(state)


async def test_a_seed_is_not_evidence(db_session):
    learner, source, target = await _pair(db_session)
    await _measured(db_session, learner, source, 2.0, 0.3)
    await mastery.seed_transfer(db_session, learner.id, target_kc_id=target.id, source_kc_id=source.id, link_id=uuid.uuid4())
    assert await mastery.kc_evidence(db_session, learner.id, [target.id]) == {}
```

Also add to the analytics tests (the file testing `subject_mastery`): a provisional KC with answers (`last_seen_at` set) and a conservative estimate above the bar reports `mastered=False`.

- [ ] **Step 2: Run** `uv run pytest tests/test_transfer.py -v` → FAIL.

- [ ] **Step 3: Settings** — `app/core/config.py`, after `detour_disprove_passes`:

```python
    # A head start carried over an accepted concept link (S24) starts from the source's current
    # estimate, but never more certain than this: the idea was shown elsewhere, in another
    # context, and that is weaker evidence than showing it here. Uncalibrated (S18).
    transfer_uncertainty_floor: float = 0.6

    # How many passes in a row confirm a head start (S24): unassisted, untaught, on different
    # questions since the link was accepted, counted back from the latest attempt. Two rather
    # than detour disproval's three: this confirms an estimate already resting on measured
    # evidence elsewhere, where disproval starts from nothing. Until then the component is
    # provisional and never counts as mastered. Uncalibrated (S18).
    transfer_confirm_passes: int = 2
```

`tests/eval/reliability/knobs.py`: add two `Knob` entries mirroring the neighbours — ids `transfer.uncertainty_floor` (`app.core.config.Settings.transfer_uncertainty_floor`, 0.6) and `transfer.confirm_passes` (`app.core.config.Settings.transfer_confirm_passes`, 2), with `governs`/`settled_by` sentences (e.g. settled by "the floor/run length at which confirmed transfers predict unaided success at the next delayed check"). Match how existing settings-backed knobs spell `where`.

- [ ] **Step 4: Mastery** — `app/learning/mastery.py`:

Constants near `SELF_REPORT_EVENT`:

```python
TRANSFER_SEED_EVENT = "transfer_seed"
"""A head start carried over an accepted concept link (S24). A seed, like ``placement_seed``:
outside ``ATTEMPT_EVENTS`` and never ``"observation"``, so no evidence reader counts it."""

TRANSFER_REVOKED_EVENT = "transfer_revoked"
"""A head start withdrawn because its link was revoked before any answer here (S24)."""
```

Helper:

```python
def is_provisional(state: LearnerKCState) -> bool:
    """A head start not yet confirmed here (S24). Never mastered, whatever the estimate says:
    a strong source seeds above the bar, and without this one answer — even a wrong one —
    would count."""
    return state.transferred_at is not None and state.transfer_confirmed_at is None
```

`KCStanding` gains `provisional: bool = False`, and `kc_standings` sets `provisional=is_provisional(state)` for rows it found.

Seed and revoke (after `seed_prior`):

```python
async def seed_transfer(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    target_kc_id: uuid.UUID,
    source_kc_id: uuid.UUID,
    link_id: uuid.UUID,
    now: datetime | None = None,
    estimator: MasteryEstimator = DEFAULT_ESTIMATOR,
) -> LearnerKCState | None:
    """Give ``target_kc_id`` a provisional head start from ``source_kc_id`` (S24).

    Only when the source has ability evidence and the target has none — a placement guess on
    the target may be replaced, an answer never. The estimate is the source's, decayed to now,
    with uncertainty raised to ``transfer_uncertainty_floor``. With several sources, the one
    giving the higher conservative estimate wins, so a later, weaker link changes nothing.
    Returns the target's state when it was seeded, ``None`` when nothing changed.
    """
    now = now or datetime.now(UTC)
    source = await session.scalar(
        select(LearnerKCState).where(
            LearnerKCState.learner_id == learner_id, LearnerKCState.kc_id == source_kc_id
        )
    )
    if source is None or source.last_seen_at is None:
        return None
    current = estimator.decay(
        _estimate_of(source), elapsed_days=_elapsed_days(source.last_seen_at, now)
    )
    seeded = Estimate(
        ability=current.ability,
        uncertainty=max(current.uncertainty, get_settings().transfer_uncertainty_floor),
    )
    target = await _get_or_create_state(session, learner_id, target_kc_id)
    if target.last_seen_at is not None:
        return None
    if target.transferred_at is not None and _estimate_of(target).conservative >= seeded.conservative:
        return None
    target.ability, target.uncertainty = seeded.ability, seeded.uncertainty
    target.transferred_from_kc_id = source_kc_id
    target.transferred_at = now
    target.transfer_confirmed_at = None
    session.add(
        LearningEvent(
            learner_id=learner_id,
            kc_id=target_kc_id,
            event_type=TRANSFER_SEED_EVENT,
            payload={
                "link_id": str(link_id),
                "source_kc_id": str(source_kc_id),
                "ability": seeded.ability,
                "uncertainty": seeded.uncertainty,
                "schema_version": EVENT_SCHEMA_VERSION,
            },
        )
    )
    await session.flush()
    return target


async def revoke_transfer(
    session: AsyncSession,
    learner_id: uuid.UUID,
    *,
    target_kc_id: uuid.UUID,
    source_kc_id: uuid.UUID,
    link_id: uuid.UUID,
) -> bool:
    """Withdraw the head start ``source_kc_id`` gave ``target_kc_id`` — only if no answer here
    has built on it yet (S24). Once there are answers the estimate rests on them and stands,
    still provisional until confirmed. Returns whether anything was withdrawn."""
    target = await _get_or_create_state(session, learner_id, target_kc_id)
    if target.transferred_from_kc_id != source_kc_id or target.last_seen_at is not None:
        return False
    target.ability, target.uncertainty = DEFAULT_ABILITY, DEFAULT_UNCERTAINTY
    target.transferred_from_kc_id = None
    target.transferred_at = None
    target.transfer_confirmed_at = None
    session.add(
        LearningEvent(
            learner_id=learner_id,
            kc_id=target_kc_id,
            event_type=TRANSFER_REVOKED_EVENT,
            payload={"link_id": str(link_id), "source_kc_id": str(source_kc_id)},
        )
    )
    await session.flush()
    return True


async def provisional_kc_ids(
    session: AsyncSession, learner_id: uuid.UUID, kc_ids: Iterable[uuid.UUID]
) -> set[uuid.UUID]:
    ids = list(kc_ids)
    if not ids:
        return set()
    return set(
        await session.scalars(
            select(LearnerKCState.kc_id).where(
                LearnerKCState.learner_id == learner_id,
                LearnerKCState.kc_id.in_(ids),
                LearnerKCState.transferred_at.is_not(None),
                LearnerKCState.transfer_confirmed_at.is_(None),
            )
        )
    )
```

Import `DEFAULT_ABILITY`, `DEFAULT_UNCERTAINTY` from `app.learning.tracer` and `Iterable` from `collections.abc`.

Confirmation in `record_observation`, between the first `await session.flush()` after the loop and `_record_achievements`:

```python
    await _confirm_transfers(session, obs.learner_id, demonstrated_states, now=now)
```

with

```python
async def _confirm_transfers(
    session: AsyncSession, learner_id: uuid.UUID, states: Sequence[LearnerKCState], *, now: datetime
) -> None:
    """Confirm head starts that a run of passes here has now earned (S24). After the flush, so
    the answer that completes the run is visible to ``passed_since``; before the achievement
    check, so that same answer can also earn the achievement."""
    pending = {state.kc_id: state.transferred_at for state in states if is_provisional(state)}
    if not pending:
        return
    settings = get_settings()
    confirmed = await passed_since(
        session,
        learner_id,
        {kc_id: at for kc_id, at in pending.items() if at is not None},
        threshold=settings.detour_failure_threshold,
        passes=settings.transfer_confirm_passes,
    )
    for state in states:
        if state.kc_id in confirmed:
            state.transfer_confirmed_at = now
```

`_record_achievements`: add `and not is_provisional(state)` to the candidate filter.

- [ ] **Step 5: Consumers of "mastered"**

`app/services/lesson_plan.py` `mastered_kc_ids`: the comprehension becomes

```python
        if standing.measured_at is not None
        and not standing.provisional
        and standing.current.conservative >= bar
```

and add a sentence to its docstring: "A provisional component (a head start carried over a concept link, not yet confirmed here — S24) is never mastered."

`goal_status`: in the loop, `if standing.measured_at is None or standing.provisional: continue`.

`app/services/analytics.py`: after `assessed`, read `provisional = await mastery.provisional_kc_ids(session, learner_id, kc_ids)`; KC-level `mastered=kc.id in assessed and kc.id not in provisional and _is_mastered(...)`; topic-level also requires `not any(kc.id in provisional for kc in topic_kcs)` (use whatever list the loop builds the topic from); subject-level requires `not provisional`.

`tests/eval/datasets/mine.py`: read `transfer_seed` like `placement_seed` — widen the `in_((...))` tuple to include `"transfer_seed"` and the `if event.event_type == "placement_seed":` test to `in ("placement_seed", "transfer_seed")`; update the docstring sentence to name both. (A `transfer_revoked` row resets to the prior; since a revoke only happens before any observation, the miner simply drops that seed: when a `transfer_revoked` row follows, `seeds.pop(key, None)`. Add `"transfer_revoked"` to the query and that branch.)

- [ ] **Step 6: Run** `uv run pytest tests/test_transfer.py -v` and the analytics test → PASS; `uv run poe check` → green.

- [ ] **Step 7: Commit**

```bash
git add app/core/config.py app/learning/mastery.py app/services/lesson_plan.py app/services/analytics.py tests/eval/reliability/knobs.py tests/eval/datasets/mine.py tests/test_transfer.py <analytics test file>
git status
git commit -m "feat(mastery): seed a provisional head start that only a run of passes confirms [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Learner decisions, suggestions, and the review endpoints [S24]

**Files:**
- Modify: `app/services/concept_links.py`, `app/api/v1/admin.py`, `app/api/v1/__init__.py`, `tests/test_visibility_sweep.py`, `frontend/src/api/schema.d.ts`
- Create: `app/schemas/concept_links.py`, `app/api/v1/concept_links.py`
- Test: `tests/test_concept_links.py`

**Interfaces:**
- Consumes: Task 5 (`ConceptLink`, `links_for_learner`, `set_admin_verdict`, `curated_queue`, exceptions), Task 7 (`seed_transfer`, `revoke_transfer`).
- Produces:
  - `concept_links.decide(session, learner_id, link_id, decision: Literal["accept", "decline", "revoke"]) -> ConceptLinkDecision`.
  - `concept_links.suggestions(session, learner_id) -> list[Suggestion]` where `Suggestion` is a frozen dataclass: `link_id`, `reason`, `endorsed_by`, `decision: str | None`, `a: LinkSide`, `b: LinkSide`; `LinkSide(kc_id, kc_name, subject_id, subject_name)`.
  - `GET /api/v1/concept-links/suggestions` → `ConceptLinkSuggestionRead[]`.
  - `POST /api/v1/concept-links/{link_id}/decision` body `{decision}` → `ConceptLinkSuggestionRead`.
  - `GET /api/v1/admin/concept-links` → `ConceptLinkReviewRead[]`; `POST /api/v1/admin/concept-links/{link_id}` body `{endorse: bool, reason: str}` → `ConceptLinkReviewRead`.

- [ ] **Step 1: Failing tests** — append to `tests/test_concept_links.py`:

```python
async def _endorsed_curated(db_session):
    concept = await _concept(db_session, "matrices")
    a = await _kc(db_session, await _subject(db_session, name="Linear Algebra"), "Matrices", concept)
    b = await _kc(db_session, await _subject(db_session, name="Graphics"), "Matrices", concept)
    await svc.sync_candidates(db_session, None)
    (link,) = await svc.curated_queue(db_session)
    admin = await _learner(db_session)
    await svc.set_admin_verdict(db_session, link.id, admin.id, endorse=True, reason="Same object.")
    return link, a, b


async def test_accepting_seeds_the_unmeasured_side(db_session):
    learner = await _learner(db_session)
    link, a, b = await _endorsed_curated(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=a.id, ability=2.0, uncertainty=0.3,
                                  last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    await svc.decide(db_session, learner.id, link.id, "accept")
    assert await mastery.provisional_kc_ids(db_session, learner.id, [a.id, b.id]) == {b.id}


async def test_decisions_follow_from_each_other(db_session):
    learner = await _learner(db_session)
    link, _a, _b = await _endorsed_curated(db_session)
    with pytest.raises(svc.LinkConflict):
        await svc.decide(db_session, learner.id, link.id, "revoke")  # nothing to revoke
    await svc.decide(db_session, learner.id, link.id, "decline")
    assert (await svc.decide(db_session, learner.id, link.id, "decline")).decision == "declined"  # idempotent
    with pytest.raises(svc.LinkConflict):
        await svc.decide(db_session, learner.id, link.id, "accept")  # a decline is final


async def test_revoking_withdraws_an_unanswered_head_start(db_session):
    learner = await _learner(db_session)
    link, a, b = await _endorsed_curated(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=a.id, ability=2.0, uncertainty=0.3,
                                  last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    await svc.decide(db_session, learner.id, link.id, "accept")
    await svc.decide(db_session, learner.id, link.id, "revoke")
    assert await mastery.provisional_kc_ids(db_session, learner.id, [b.id]) == set()
    assert await svc.links_in_effect(db_session, learner.id, [a.id]) == {}


async def test_an_unendorsed_or_invisible_link_cannot_be_decided(db_session):
    learner, stranger = await _learner(db_session), await _learner(db_session)
    concept = await _concept(db_session, "trees")
    await _kc(db_session, await _subject(db_session, name="Mine", owner=stranger.id), "Trees", concept)
    await _kc(db_session, await _subject(db_session, name="Lib"), "Trees", concept)
    await svc.sync_candidates(db_session, stranger.id)
    (theirs,) = await svc.links_for_learner(db_session, stranger.id)
    theirs.verdict = "endorsed"
    await db_session.flush()
    with pytest.raises(svc.LinkNotFound):
        await svc.decide(db_session, learner.id, theirs.id, "accept")
    link, _a, _b = await _endorsed_curated(db_session)
    link.verdict = None
    await db_session.flush()
    with pytest.raises(svc.LinkNotFound):
        await svc.decide(db_session, learner.id, link.id, "accept")


async def test_suggestions_list_endorsed_undecided_and_accepted_links_only(db_session):
    learner = await _learner(db_session)
    link, a, b = await _endorsed_curated(db_session)
    (s,) = await svc.suggestions(db_session, learner.id)
    assert (s.link_id, s.decision, s.reason) == (link.id, None, "Same object.")
    assert {s.a.subject_name, s.b.subject_name} == {"Linear Algebra", "Graphics"}
    await svc.decide(db_session, learner.id, link.id, "accept")
    assert [x.decision for x in await svc.suggestions(db_session, learner.id)] == ["accepted"]
    await svc.decide(db_session, learner.id, link.id, "revoke")
    assert [x.decision for x in await svc.suggestions(db_session, learner.id)] == [None]
```

Add imports: `from datetime import UTC, datetime`, `from app.learning import mastery`, `from app.models.learning import LearnerKCState`.

And an API test (same file or `tests/test_concept_links_api.py`) using `api_client` + `api_learner`: GET suggestions returns the endorsed link; POST decision `accept` returns `decision == "accepted"`; POST on a random uuid → 404; a non-admin POST to `/api/v1/admin/concept-links/{id}` → 403.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Service** — append to `app/services/concept_links.py`:

```python
@dataclass(frozen=True)
class LinkSide:
    kc_id: uuid.UUID
    kc_name: str
    subject_id: uuid.UUID
    subject_name: str


@dataclass(frozen=True)
class Suggestion:
    link_id: uuid.UUID
    reason: str | None
    endorsed_by: str | None
    decision: str | None  # None (undecided, or revoked) | "accepted"
    a: LinkSide
    b: LinkSide


async def _decidable(session: AsyncSession, learner_id: uuid.UUID, link_id: uuid.UUID) -> ConceptLink:
    """The link, if this learner may decide it: endorsed, and visible to them. One answer
    (``LinkNotFound``) for every other case, so a caller cannot probe for other learners' pairs."""
    link = await session.scalar(
        select(ConceptLink).where(ConceptLink.id == link_id, _visible_link(learner_id))
    )
    if link is None or link.verdict != ENDORSED:
        raise LinkNotFound(str(link_id))
    return link


async def decide(
    session: AsyncSession,
    learner_id: uuid.UUID,
    link_id: uuid.UUID,
    decision: Literal["accept", "decline", "revoke"],
) -> ConceptLinkDecision:
    """Record the learner's half of a link and apply it (S24).

    - ``accept`` (from undecided or revoked): seeds each side from the other where one has
      evidence and the other none (``mastery.seed_transfer``).
    - ``decline`` (from undecided): final — the pair is not offered again.
    - ``revoke`` (from accepted): withdraws any head start not yet answered on.
    A repeat of the current decision returns it unchanged. Anything else is ``LinkConflict``.
    """
    link = await _decidable(session, learner_id, link_id)
    row = await session.scalar(
        select(ConceptLinkDecision).where(
            ConceptLinkDecision.learner_id == learner_id, ConceptLinkDecision.link_id == link_id
        )
    )
    current = row.decision if row is not None else None
    target = {"accept": ACCEPTED, "decline": DECLINED, "revoke": REVOKED}[decision]
    if current == target:
        return row  # type: ignore[return-value]  # current == target implies a row
    allowed = {
        ACCEPTED: (None, REVOKED),
        DECLINED: (None,),
        REVOKED: (ACCEPTED,),
    }[target]
    if current not in allowed:
        raise LinkConflict(f"cannot {decision} a link that is {current or 'undecided'}")
    if row is None:
        row = ConceptLinkDecision(learner_id=learner_id, link_id=link_id, decision=target)
        session.add(row)
    else:
        row.decision, row.decided_at = target, datetime.now(UTC)
    pairs = ((link.kc_a_id, link.kc_b_id), (link.kc_b_id, link.kc_a_id))
    if target == ACCEPTED:
        for target_kc, source_kc in pairs:
            await mastery.seed_transfer(
                session, learner_id, target_kc_id=target_kc, source_kc_id=source_kc, link_id=link.id
            )
    elif target == REVOKED:
        for target_kc, source_kc in pairs:
            await mastery.revoke_transfer(
                session, learner_id, target_kc_id=target_kc, source_kc_id=source_kc, link_id=link.id
            )
    await session.flush()
    return row


async def suggestions(session: AsyncSession, learner_id: uuid.UUID) -> list[Suggestion]:
    """Endorsed links this learner can see and has not declined — undecided (or revoked) ones
    to accept, accepted ones to revoke. Refreshes candidates first, so a newly endorsed curated
    pair touching a subject the learner can see shows up without waiting for anything."""
    await sync_candidates(session, learner_id)
    decided = {
        d.link_id: d.decision
        for d in await session.scalars(
            select(ConceptLinkDecision).where(ConceptLinkDecision.learner_id == learner_id)
        )
    }
    links = [
        link
        for link in await session.scalars(
            select(ConceptLink)
            .where(_visible_link(learner_id), ConceptLink.verdict == ENDORSED)
            .order_by(ConceptLink.decided_at)
        )
        if decided.get(link.id) != DECLINED
    ]
    kc_ids = {kc for link in links for kc in (link.kc_a_id, link.kc_b_id)}
    sides = {
        kc.id: LinkSide(kc_id=kc.id, kc_name=kc.name, subject_id=subject.id, subject_name=subject.name)
        for kc, subject in (
            await session.execute(
                select(KC, Subject)
                .join(Topic, KC.topic_id == Topic.id)
                .join(Subject, Topic.subject_id == Subject.id)
                .where(KC.id.in_(kc_ids))
            )
        ).all()
    } if kc_ids else {}
    return [
        Suggestion(
            link_id=link.id,
            reason=link.reason,
            endorsed_by=link.endorsed_by,
            decision=ACCEPTED if decided.get(link.id) == ACCEPTED else None,
            a=sides[link.kc_a_id],
            b=sides[link.kc_b_id],
        )
        for link in links
        if link.kc_a_id in sides and link.kc_b_id in sides
    ]
```

Imports: `from dataclasses import dataclass`, `from typing import Literal`, `from app.learning import mastery`.

- [ ] **Step 4: Schemas** — `app/schemas/concept_links.py`:

```python
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LinkSideRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    kc_id: uuid.UUID
    kc_name: str
    subject_id: uuid.UUID
    subject_name: str


class ConceptLinkSuggestionRead(BaseModel):
    """An endorsed link the learner can accept (``decision`` null) or revoke (``"accepted"``)."""

    model_config = ConfigDict(from_attributes=True)

    link_id: uuid.UUID
    reason: str | None
    endorsed_by: Literal["admin", "judge"] | None
    decision: Literal["accepted"] | None
    a: LinkSideRead
    b: LinkSideRead


class ConceptLinkDecisionSubmit(BaseModel):
    decision: Literal["accept", "decline", "revoke"]


class ConceptLinkReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kc_a_id: uuid.UUID
    kc_b_id: uuid.UUID
    kc_a_name: str
    kc_b_name: str
    subject_a_name: str
    subject_b_name: str
    verdict: Literal["endorsed", "rejected"] | None
    reason: str | None
    decided_at: datetime | None


class ConceptLinkVerdictSubmit(BaseModel):
    endorse: bool
    reason: str = Field(min_length=1, max_length=500)
```

- [ ] **Step 5: Routes** — `app/api/v1/concept_links.py`:

```python
"""A learner's side of concept links (S24): what is suggested, and their decision on it."""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, SessionDep
from app.schemas.concept_links import ConceptLinkDecisionSubmit, ConceptLinkSuggestionRead
from app.services import concept_links as svc
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as lesson_plan_svc

router = APIRouter(tags=["concept-links"])


@router.get("/concept-links/suggestions", response_model=list[ConceptLinkSuggestionRead])
async def list_suggestions(session: SessionDep, learner: CurrentLearner):
    found = await svc.suggestions(session, learner.id)
    await session.commit()  # sync_candidates may have recorded new pairs
    return found


@router.post("/concept-links/{link_id}/decision", response_model=ConceptLinkSuggestionRead)
async def decide_link(
    link_id: uuid.UUID, data: ConceptLinkDecisionSubmit, session: SessionDep, learner: CurrentLearner
):
    """404 for a link that is not endorsed or not the caller's to see; 409 for a decision that
    does not follow from the last one (accepting a declined link, revoking an undecided one)."""
    # Read before deciding: a decline takes the link out of the suggestions for good, and the
    # response still has to describe it. `None` here is the same 404 as `LinkNotFound`.
    before = next((s for s in await svc.suggestions(session, learner.id) if s.link_id == link_id), None)
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link")
    try:
        row = await svc.decide(session, learner.id, link_id, data.decision)
    except svc.LinkNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link") from exc
    except svc.LinkConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    # A head start given or withdrawn changes what those plans should show (check-first,
    # external steps), so they are brought up to date now rather than on the next answer.
    for subject_id in {before.a.subject_id, before.b.subject_id}:
        await lesson_plan_svc.revise_plan(session, learner_id=learner.id, subject_id=subject_id)
    return dataclasses.replace(before, decision="accepted" if row.decision == "accepted" else None)
```

Import `dataclasses`; drop the unused `knowledge_svc` import.

Register in `app/api/v1/__init__.py`: `api_router.include_router(concept_links.router)`.

`app/api/v1/admin.py`:

```python
@router.get("/concept-links", response_model=list[ConceptLinkReviewRead])
async def concept_link_queue(_: CurrentAdmin, session: SessionDep):
    """Curated candidate links, undecided first (S24)."""
    await concept_links_svc.sync_candidates(session, None)
    await session.commit()
    return await concept_links_svc.review_rows(session)


@router.post("/concept-links/{link_id}", response_model=ConceptLinkReviewRead)
async def decide_concept_link(
    link_id: uuid.UUID, body: ConceptLinkVerdictSubmit, admin: CurrentAdmin, session: SessionDep
):
    """Endorse or reject one curated link, once, with a reason learners will read."""
    try:
        await concept_links_svc.set_admin_verdict(
            session, link_id, admin.id, endorse=body.endorse, reason=body.reason
        )
    except concept_links_svc.LinkNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such curated link") from exc
    except concept_links_svc.LinkAlreadyDecided as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "this link has already been decided") from exc
    await session.commit()
    return next(r for r in await concept_links_svc.review_rows(session) if r.id == link_id)
```

and in the service a `review_rows(session) -> list[ConceptLinkReviewRead]` that joins each curated link's two KCs and subjects (same KC→Topic→Subject join as `suggestions`) in `curated_queue` order. Put the schema import in the service or build plain dicts and let the route validate — follow whichever the admin module already does for publications.

- [ ] **Step 6: Visibility sweep** — `tests/test_visibility_sweep.py`:
  - `Ids` gains `link: uuid.UUID  # an endorsed private concept link between kc and kc2 (S24)`; `_random_ids` makes 9 uuids.
  - `_private_graph` adds, after the KCs exist:

```python
    a_kc, b_kc = sorted((kc.id, kc2.id))
    link = ConceptLink(kc_a_id=a_kc, kc_b_id=b_kc, scope="private", owner_learner_id=owner.id,
                       verdict="endorsed", endorsed_by="judge", reason="Same idea.")
    session.add(link)
```

    and passes `link=link.id` into `Ids(...)`.
  - New cases:

```python
    Case(
        "POST",
        "/api/v1/concept-links/{link_id}/decision",
        "link_id",
        ("link",),
        lambda c, t, o: c.post(f"{API}/concept-links/{t.link}/decision", json={"decision": "decline"}),
    ),
    Case(
        "POST",
        "/api/v1/admin/concept-links/{link_id}",
        "link_id",
        ("link",),
        lambda c, t, o: c.post(f"{API}/admin/concept-links/{t.link}", json={"endorse": True, "reason": "x"}),
        owner_exempt="every caller without the admin tier gets the same 403, whether the link exists or not — these are review routes, not learner routes",
        refusal=403,
    ),
```

- [ ] **Step 7: Regenerate types** — `cd frontend && npm run gen:api`.

- [ ] **Step 8: Run** `uv run pytest tests/test_concept_links.py tests/test_visibility_sweep.py -v`, `uv run poe check`, `uv run poe api-contract` → green.

- [ ] **Step 9: Commit**

```bash
git add app/services/concept_links.py app/schemas/concept_links.py app/api/v1/concept_links.py app/api/v1/__init__.py app/api/v1/admin.py tests/test_concept_links.py tests/test_visibility_sweep.py frontend/src/api/schema.d.ts <api test file if separate>
git status
git commit -m "feat(api): let a learner accept, decline or revoke an endorsed concept link [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Plan cross-subject prerequisites and check-first steps [S24]

**Files:**
- Modify: `app/learning/lesson_plan.py`, `app/services/lesson_plan.py`, `app/schemas/lesson_plan.py`, `frontend/src/api/schema.d.ts`
- Test: `tests/test_prerequisite_detour.py` (engine + service), `tests/test_lesson_plan.py`

**Interfaces:**
- Consumes: Task 5 `concept_links.links_in_effect`; Task 7 `mastery.provisional_kc_ids`, `mastered_kc_ids` (provisional-aware); `mastery.closed_detour_routes`; `knowledge.is_visible_to`.
- Produces:
  - `engine.DETOUR_EXTERNAL = "external"`.
  - `engine.Detour.source_subject_id: uuid.UUID | None = None`, `.source_subject_name: str | None = None`.
  - `StepDict.source_subject_id`, `.source_subject_name` (`NotRequired[str | None]`), `.check_first` (`NotRequired[bool]`).
  - `engine.revise_steps(..., external_detours: Sequence[Detour] = (), provisional_kc_ids: Iterable[uuid.UUID] = ())`.
  - `LessonStepRead.source_subject_id: uuid.UUID | None = None`, `.source_subject_name: str | None = None`, `.check_first: bool = False`.
  - `PlanGroundingContext.check_first: bool = False`.

- [ ] **Step 1: Failing engine tests** — `tests/test_prerequisite_detour.py` (pure `revise_steps`; reuse the file's `_step` / `_revise` / `_by_kc` helpers):

```python
def _external(prereq: uuid.UUID, blocked: uuid.UUID) -> engine.Detour:
    return engine.Detour(prereq_kc_id=prereq, blocked_kc_id=blocked, reason=engine.DETOUR_EXTERNAL,
                         source_subject_id=uuid.uuid4(), source_subject_name="Linear Algebra")


def test_an_external_prerequisite_sits_just_before_the_step_that_needs_it() -> None:
    first, blocked, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    steps = [_step(first, status="active", order=0), _step(blocked, status="pending", order=1)]
    revised = _revise(steps, external_detours=[_external(foreign, blocked)])
    assert [s["kc_id"] for s in revised] == [str(first), str(foreign), str(blocked)]
    ext = _by_kc(revised, foreign)
    assert (ext["step_type"], ext["status"], ext["source_subject_name"]) == ("detour", "pending", "Linear Algebra")
    assert _by_kc(revised, first)["status"] == "active"


def test_exploration_offers_an_external_prerequisite() -> None:
    blocked, foreign = uuid.uuid4(), uuid.uuid4()
    revised = _revise([_step(blocked, status="active")], external_detours=[_external(foreign, blocked)],
                      guidance="exploration")
    assert _by_kc(revised, foreign)["status"] == "proposed"


def test_an_external_prerequisite_is_never_disproved() -> None:
    blocked, foreign = uuid.uuid4(), uuid.uuid4()
    steps = _revise([_step(blocked, status="active")], external_detours=[_external(foreign, blocked)])
    steps = _revise(steps, disproved_kc_ids=[foreign])
    assert _by_kc(steps, foreign)["status"] != "done"


def test_an_external_step_is_dropped_once_its_step_is_done() -> None:
    blocked, foreign = uuid.uuid4(), uuid.uuid4()
    steps = _revise([_step(blocked, status="active")], external_detours=[_external(foreign, blocked)])
    steps = _revise(steps, mastered_kc_ids=[blocked])
    assert all(s["kc_id"] != str(foreign) for s in steps)


def test_a_provisional_component_is_checked_first() -> None:
    kc = uuid.uuid4()
    steps = _revise([_step(kc, status="active")], provisional_kc_ids=[kc])
    assert _by_kc(steps, kc)["check_first"] is True
    steps = _revise(steps)
    assert _by_kc(steps, kc)["check_first"] is False
```

(If `_revise` does not forward arbitrary keyword arguments to `engine.revise_steps`, extend it to.)

- [ ] **Step 2: Failing service tests** — `tests/test_prerequisite_detour.py`:

```python
async def _foreign_prereq(session: AsyncSession, *, owner: Learner | None = None):
    """Subject B's `blocked` requires `foreign` from subject A."""
    learner = owner or Learner(handle=f"x-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    subject_a = Subject(slug=f"a-{uuid.uuid4().hex[:8]}", name="Linear Algebra")
    subject_b = Subject(slug=f"b-{uuid.uuid4().hex[:8]}", name="Graphics")
    session.add_all([subject_a, subject_b])
    await session.flush()
    ta, tb = Topic(subject_id=subject_a.id, slug="t", name="T"), Topic(subject_id=subject_b.id, slug="t", name="T")
    session.add_all([ta, tb])
    await session.flush()
    foreign = KC(topic_id=ta.id, slug="vectors", name="Vectors")
    blocked = KC(topic_id=tb.id, slug="transforms", name="Transforms")
    session.add_all([foreign, blocked])
    await session.flush()
    session.add(KCEdge(prereq_kc_id=foreign.id, kc_id=blocked.id))
    await session.flush()
    return learner, subject_a, subject_b, foreign, blocked


async def test_an_unmastered_foreign_prerequisite_becomes_an_external_step(db_session):
    learner, subject_a, subject_b, foreign, blocked = await _foreign_prereq(db_session)
    plan = await _plan(db_session, learner, subject_b)
    ext = _detour_step(plan, foreign)
    assert (ext["detour_reason"], ext["source_subject_name"]) == ("external", "Linear Algebra")


async def test_a_mastered_foreign_prerequisite_is_satisfied(db_session):
    learner, _a, subject_b, foreign, _blocked = await _foreign_prereq(db_session)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=foreign.id, ability=3.0,
                                  uncertainty=0.2, last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    plan = await _plan(db_session, learner, subject_b)
    assert all(s["kc_id"] != str(foreign.id) for s in plan.steps)


async def test_a_foreign_prerequisite_in_a_subject_the_learner_cannot_see_is_dropped(db_session):
    """Review focus 4."""
    learner, subject_a, subject_b, foreign, _blocked = await _foreign_prereq(db_session)
    stranger = Learner(handle=f"s-{uuid.uuid4().hex[:8]}")
    db_session.add(stranger)
    await db_session.flush()
    subject_a.owner_learner_id = stranger.id
    await db_session.flush()
    plan = await _plan(db_session, learner, subject_b)
    assert all(s["kc_id"] != str(foreign.id) for s in plan.steps)


async def test_a_linked_local_equivalent_orders_the_plan_instead(db_session):
    learner, _a, subject_b, foreign, blocked = await _foreign_prereq(db_session)
    topic_b = await db_session.scalar(select(Topic).where(Topic.subject_id == subject_b.id))
    local = KC(topic_id=topic_b.id, slug="zz-vectors", name="Vectors")  # slug sorts last on purpose
    db_session.add(local)
    await db_session.flush()
    a, b = sorted((foreign.id, local.id))
    link = ConceptLink(kc_a_id=a, kc_b_id=b, scope="curated", verdict="endorsed", endorsed_by="admin")
    db_session.add(link)
    await db_session.flush()
    db_session.add(ConceptLinkDecision(learner_id=learner.id, link_id=link.id, decision="accepted"))
    await db_session.flush()
    plan = await _plan(db_session, learner, subject_b)
    order = [s["kc_id"] for s in plan.steps]
    assert str(foreign.id) not in order
    assert order.index(str(local.id)) < order.index(str(blocked.id))


async def test_a_skipped_external_step_is_not_offered_again(db_session):
    learner, _a, subject_b, foreign, _blocked = await _foreign_prereq(db_session)
    await _plan(db_session, learner, subject_b)
    await svc.decide_detour(db_session, learner_id=learner.id, subject_id=subject_b.id,
                            prereq_kc_id=foreign.id, decision="skip")
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject_b.id)
    open_ = [s for s in plan.steps if s["kc_id"] == str(foreign.id) and s["status"] not in ("done", "skipped")]
    assert open_ == []


async def test_an_external_step_closes_mastered_once_the_prerequisite_is(db_session):
    learner, _a, subject_b, foreign, blocked = await _foreign_prereq(db_session)
    await _plan(db_session, learner, subject_b)
    db_session.add(LearnerKCState(learner_id=learner.id, kc_id=foreign.id, ability=3.0,
                                  uncertainty=0.2, last_seen_at=datetime.now(UTC)))
    await db_session.flush()
    plan = await svc.revise_plan(db_session, learner_id=learner.id, subject_id=subject_b.id)
    step = _detour_step(plan, foreign)
    assert (step["status"], step["detour_outcome"]) == ("done", "mastered")
```

And in `tests/test_lesson_plan.py`: a goal whose objective is B's components reports `objective_kc_count` without the external step (external steps are never objective KCs).

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Engine** — `app/learning/lesson_plan.py`:

```python
DETOUR_EXTERNAL = "external"
"""A prerequisite written into the graph that lives in another subject (S24). Planned as a
detour so it shares the detour lifecycle — offered or taken per guidance, skippable, closed
with an outcome — but it is not a hypothesis about a struggle, so it is never disproved, sorts
just before the step that needs it rather than ahead of everything, and is practised in the
learner's preferred format rather than the diagnostic one."""
```

`Detour` gains the two optional source fields; `StepDict` gains:

```python
    # Set only on an external detour (S24): the subject the prerequisite lives in, so the
    # step can say where it is from. Absent everywhere else.
    source_subject_id: NotRequired[str | None]
    source_subject_name: NotRequired[str | None]
    # A provisional component (S24) — a head start carried over a concept link, unconfirmed
    # here. Practice asks before it explains. Recomputed on every revision.
    check_first: NotRequired[bool]
```

In `revise_steps`, add parameters `external_detours: Sequence[Detour] = ()` and `provisional_kc_ids: Iterable[uuid.UUID] = ()`, and document them in the docstring's numbered rules (0: externals inserted like detours; 2: externals never disproved; 3: an external of any open status whose blocked step is done/absent is dropped; 5: externals sort immediately before their blocked step; 7: `check_first` refreshed on open steps; externals use the scaffolding item type). Then:

```python
    def _is_external(step: StepDict) -> bool:
        return step["step_type"] == "detour" and step.get("detour_reason") == DETOUR_EXTERNAL
```

Disproval branch: add `and not _is_external(step)` to the `elif`.

Pruning comprehension: drop a step when

```python
            step["step_type"] == "detour"
            and (step["status"] == "proposed" or (_is_external(step) and step["status"] not in CLOSED))
            and blocked_status.get(step.get("detour_for") or "", "done") == "done"
```

(define `CLOSED = ("done", "skipped")` before this point and remove the later duplicate definition).

Insertion: move the existing `if detour is not None:` body into a nested helper and call it for every trigger:

```python
    def _insert(trigger: Detour) -> None:
        prereq_id = str(trigger.prereq_kc_id)
        already_open = any(
            step["step_type"] == "detour"
            and step["status"] in OPEN_DETOUR_STATUSES
            and step["kc_id"] == prereq_id
            for step in result
        )
        if already_open or prereq_id in mastered:
            return
        offered_at = (now or datetime.now(UTC)).isoformat()
        step = StepDict(
            kc_id=prereq_id,
            order=0,
            step_type="detour",
            status="proposed" if guidance == "exploration" else "pending",
            target_difficulty=None,
            hint_density=None,
            preferred_item_type=None,
            detour_for=str(trigger.blocked_kc_id),
            detour_reason=trigger.reason,
            offered_at=offered_at,
            opened_at=None if guidance == "exploration" else offered_at,
        )
        if trigger.source_subject_id is not None:
            step["source_subject_id"] = str(trigger.source_subject_id)
            step["source_subject_name"] = trigger.source_subject_name
        result.append(step)

    for trigger in (*([detour] if detour is not None else []), *external_detours):
        _insert(trigger)
```

Sorting:

```python
    def _bucket(step: StepDict) -> int:
        if step["status"] in CLOSED:
            return 3
        if step["step_type"] == "detour" and step["status"] != "proposed" and not _is_external(step):
            return 0
        return 1 if step["step_type"] == "review" else 2

    def _within_bucket(step: StepDict) -> Any:
        if step["status"] not in CLOSED and step["step_type"] == "review":
            return review_rank.get(step["kc_id"], len(due_order))
        if step["status"] == "proposed" or (_is_external(step) and step["status"] not in CLOSED):
            return (blocked_order.get(step.get("detour_for") or "", step["order"]), 0)
        if _bucket(step) == 2:
            return (step["order"], 1)
        return step["order"]
```

Hints loop:

```python
    provisional = {str(kc_id) for kc_id in provisional_kc_ids}
    for step in result:
        if step["status"] in CLOSED:
            continue
        step["target_difficulty"] = scaffolding.target_difficulty
        step["hint_density"] = scaffolding.hint_density
        step["preferred_item_type"] = (
            DETOUR_ITEM_TYPE
            if step["step_type"] == "detour" and not _is_external(step)
            else scaffolding.preferred_item_type
        )
        step["check_first"] = step["kc_id"] in provisional
```

- [ ] **Step 5: Service** — `app/services/lesson_plan.py`:

```python
async def _external_detours(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID,
    steps: Sequence[Mapping[str, Any]],
) -> list[engine.Detour]:
    """Prerequisites of this plan's open steps that live in another subject and still need
    teaching (S24): not linked to a component here, not mastered, visible to the learner, and
    not already skipped for that step. What is left is planned as an external detour."""
    open_new = {
        uuid.UUID(s["kc_id"])
        for s in steps
        if s["step_type"] == "new" and s["status"] not in ("done", "skipped")
    }
    if not open_new:
        return []
    local = {kc.id for kc in await knowledge_svc.list_kcs_for_subject(session, subject_id)}
    foreign = [
        (e.prereq_kc_id, e.kc_id)
        for e in await knowledge_svc.list_edges_for_subject(session, subject_id)
        if e.prereq_kc_id not in local and e.kc_id in open_new
    ]
    if not foreign:
        return []
    prereq_ids = {prereq for prereq, _ in foreign}
    linked = await concept_links_svc.links_in_effect(session, learner_id, prereq_ids)
    mastered = await mastered_kc_ids(session, learner_id, prereq_ids)
    subjects = {
        kc.id: subject
        for kc, subject in (
            await session.execute(
                select(KC, Subject)
                .join(Topic, KC.topic_id == Topic.id)
                .join(Subject, Topic.subject_id == Subject.id)
                .where(KC.id.in_(prereq_ids))
            )
        ).all()
    }
    out: list[engine.Detour] = []
    closed: dict[uuid.UUID, set[uuid.UUID]] = {}
    for prereq, blocked in foreign:
        if linked.get(prereq, set()) & local or prereq in mastered:
            continue
        subject = subjects.get(prereq)
        if subject is None or not knowledge_svc.is_visible_to(subject, learner_id):
            continue
        if blocked not in closed:
            closed[blocked] = await mastery.closed_detour_routes(session, learner_id, blocked)
        if prereq in closed[blocked]:
            continue
        out.append(
            engine.Detour(
                prereq_kc_id=prereq,
                blocked_kc_id=blocked,
                reason=engine.DETOUR_EXTERNAL,
                source_subject_id=subject.id,
                source_subject_name=subject.name,
            )
        )
    return out
```

Import `concept_links as concept_links_svc`, and `Topic` if absent.

`generate_lesson_plan`: after computing `foreign`, read links and fold linked equivalents into the local ordering (update the comment above `foreign` to describe all three outcomes):

```python
    linked = await concept_links_svc.links_in_effect(
        session, learner_id, {e.prereq_kc_id for e in foreign}
    )
    # A foreign prerequisite linked to a component of this subject orders the plan through that
    # component, exactly like a local edge (S24).
    local_pairs = [(e.prereq_kc_id, e.kc_id) for e in stored_edges if e.prereq_kc_id in all_kc_ids]
    local_pairs += [
        (equivalent, e.kc_id)
        for e in foreign
        for equivalent in sorted(linked.get(e.prereq_kc_id, set()) & all_kc_ids)
    ]
    kept, dropped = prerequisites.acyclic(local_pairs)
```

(replacing the existing `local_edges` / `acyclic` lines). Then pass externals and provisional into the initial `revise_steps`:

```python
    steps = engine.revise_steps(
        bare_steps,
        mastered_kc_ids=mastered,
        due_review_kc_ids=due_reviews,
        scaffolding=scaffolding,
        external_detours=await _external_detours(
            session, learner_id=learner_id, subject_id=subject_id, steps=bare_steps
        ),
        provisional_kc_ids=await mastery.provisional_kc_ids(session, learner_id, kc_order),
    )
```

Note `generate_lesson_plan` runs `revise_steps` with the default guidance (a regenerated plan keeps `plan.guidance`; pass `guidance=cast("engine.Guidance", plan.guidance)` if the plan exists — read `_get_plan` before this call and fall back to `"guided"`).

`_revision_inputs`: include open detour steps' KCs in the mastery read, and return provisional ids:

```python
    step_kc_ids = {
        uuid.UUID(step["kc_id"])
        for step in plan.steps
        if step["step_type"] in ("new", "detour") and step["status"] not in ("done", "skipped")
    }
    new_kc_ids = {uuid.UUID(step["kc_id"]) for step in plan.steps if step["step_type"] == "new"}
    mastered = await mastered_kc_ids(session, learner_id, new_kc_ids | step_kc_ids)
    ...
    provisional = await mastery.provisional_kc_ids(session, learner_id, step_kc_ids)
    return mastered, due_reviews, scaffolding, provisional
```

(Docstring: say detour KCs are included because an external detour's component is never a "new" step here, so without it an external detour could never close as mastered.) Update both callers to unpack four values and pass `provisional=provisional` into `_apply_revision`, which forwards `provisional_kc_ids=provisional` to **both** `revise_steps` calls and computes `external_detours=await _external_detours(session, learner_id=learner_id, subject_id=plan.subject_id, steps=plan.steps)` for the first one only. `_apply_revision` does **not** call `record_detour` for externals (the existing call is guarded by `detour is not None` and names only the struggle trigger — leave it so).

`get_active_step_context`: `PlanGroundingContext` gains `check_first: bool = False`, set from `bool(active.get("check_first"))`.

`app/schemas/lesson_plan.py` `LessonStepRead`: add

```python
    # Set only on an external detour (S24): the subject the prerequisite lives in.
    source_subject_id: uuid.UUID | None = None
    source_subject_name: str | None = None
    # A provisional component (S24): practice asks before it explains.
    check_first: bool = False
```

- [ ] **Step 6: Regenerate types** — `cd frontend && npm run gen:api`.

- [ ] **Step 7: Run** `uv run pytest tests/test_prerequisite_detour.py tests/test_lesson_plan.py -v`, `uv run poe check`, `uv run poe api-contract` → green.

- [ ] **Step 8: Commit**

```bash
git add app/learning/lesson_plan.py app/services/lesson_plan.py app/schemas/lesson_plan.py frontend/src/api/schema.d.ts tests/test_prerequisite_detour.py tests/test_lesson_plan.py
git status
git commit -m "feat(lesson-plan): plan cross-subject prerequisites and check provisional components first [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Guided practice asks before it explains on a check-first step [S24]

**Files:**
- Modify: `app/services/workflow.py` (`run_workflow_turn` fresh start, new prompt constant), `app/agent/state.py` (`WorkflowState.taught_first`), `app/agent/workflow.py` (`grade`)
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 9's `PlanGroundingContext.check_first`.
- Produces: `workflow.CHECK_FIRST_SYSTEM_PROMPT: str`; `WorkflowState.taught_first: NotRequired[bool]`.

- [ ] **Step 1: Failing tests** — in `tests/test_workflow.py`, copying the fresh-start setup of the file's existing start test:

```python
async def test_a_check_first_step_poses_the_problem_without_a_worked_example(db_session, monkeypatch):
    # Arrange a plan whose active step has check_first=True — set it directly on the stored
    # plan's active step after generating it, the same way other tests edit plan.steps.
    ...
    captured: list[str] = []
    real_build = workflow_mod.build_workflow_graph

    def capture(llm, session, **kw):
        graph = real_build(llm, session, **kw)
        return graph

    # Simplest observable: the system prompt the start composes. Patch learner_context.compose
    # to record its first argument, then start practice.
    monkeypatch.setattr(workflow_svc.learner_context, "compose",
                        lambda base, *a, **k: captured.append(base) or base)
    ...  # start practice exactly as the existing test does
    assert captured == [workflow_svc.CHECK_FIRST_SYSTEM_PROMPT]


async def test_an_answer_on_a_check_first_step_is_not_marked_taught(db_session, monkeypatch):
    seen: list[bool] = []
    real = assessment_svc.answer_item

    async def capture(session, learner_id, item, submission, **kwargs):
        seen.append(kwargs.get("taught_first", False))
        return await real(session, learner_id, item, submission, **kwargs)

    monkeypatch.setattr(assessment_svc, "answer_item", capture)
    ...  # check_first plan as above; start, then resume with an answer
    assert seen == [False]
```

Fill each `...` from the existing start/resume tests in the file; the only additions are setting `check_first` on the active step and the two captures. Remove the unused `capture`/`real_build` stub in the first test once the `compose` patch is in place.

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — `app/services/workflow.py`:

```python
CHECK_FIRST_SYSTEM_PROMPT = (
    "You are Guru, confirming something the learner has already shown in another subject. Do "
    "not give a worked example. Pose the practice problem below for the learner to attempt in "
    "their own words — do not invent a different problem, since their answer is graded against "
    "this one. If they struggle, you will teach it afterwards. Keep it brief and friendly."
)
```

At the fresh start, choose the base prompt and carry the flag:

```python
        # A provisional component (S24) is confirmed, not taught: asking first is the "short
        # confirmation" V04 calls for, and an answer given without a worked example is exactly
        # the unaided pass that confirms it.
        base_prompt = CHECK_FIRST_SYSTEM_PROMPT if step.check_first else WORKFLOW_SYSTEM_PROMPT
        system = learner_context.compose(base_prompt, context, ...)  # other args unchanged
        run_input = {..., "taught_first": not step.check_first}
```

`app/agent/state.py` `WorkflowState`:

```python
    # Whether this question opened with a worked example (S11/S24). False only on a
    # check-first step, which poses the problem cold. NotRequired: absent on checkpoints
    # written before this existed, which all opened with one.
    taught_first: NotRequired[bool]
```

`app/agent/workflow.py` `grade`: `taught_first=state.get("taught_first", True),` (update the comment above it to mention the check-first exception).

- [ ] **Step 4: Run** `uv run pytest tests/test_workflow.py -v`, then `uv run poe check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/workflow.py app/agent/state.py app/agent/workflow.py tests/test_workflow.py
git status
git commit -m "feat(workflow): ask before explaining on a component being confirmed [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Connections panel and the new step labels [S24]

**Files:**
- Modify: `frontend/src/api/hooks.ts`, `frontend/src/pages/Lessons.tsx`, `frontend/src/components/lessons/LessonStepRow.tsx`, `frontend/src/components/lessons/LessonStepRow.test.tsx`
- Create: `frontend/src/components/lessons/ConnectionsPanel.tsx`, `frontend/src/components/lessons/ConnectionsPanel.test.tsx`

**Interfaces:**
- Consumes: Task 8 endpoints; Task 9 `LessonStepRead` fields.
- Produces: `useConceptLinkSuggestions()`, `useDecideConceptLink()`, `<ConnectionsPanel subjectId />`.

- [ ] **Step 1: Failing tests**

`ConnectionsPanel.test.tsx` (same fetch-stub pattern as `CurriculumIssuesPanel.test.tsx`):

```tsx
const suggestion = {
  link_id: "l-1", reason: "Both are the rate of change.", endorsed_by: "judge", decision: null,
  a: { kc_id: "k-a", kc_name: "Derivatives", subject_id: "s-calc", subject_name: "Calculus" },
  b: { kc_id: "k-b", kc_name: "Derivatives", subject_id: "s-phys", subject_name: "Physics" },
};

it("offers a suggestion touching this subject, naming the other side and the reason", async () => {
  stub([suggestion]);
  renderPanel("s-phys");
  expect(await screen.findByText(/Derivatives in Calculus/)).toBeInTheDocument();
  expect(screen.getByText("Both are the rate of change.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Use it here" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Not the same" })).toBeInTheDocument();
});

it("ignores suggestions about other subjects", async () => {
  stub([suggestion]);
  const { container } = renderPanel("s-other");
  await waitFor(() => expect(container).toBeEmptyDOMElement());
});

it("accepts, and offers revoke on an accepted link", async () => {
  const calls = stub([{ ...suggestion, decision: "accepted" }]);
  renderPanel("s-phys");
  await userEvent.click(await screen.findByRole("button", { name: "Stop using it" }));
  await waitFor(() =>
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/concept-links/l-1/decision"))).toBe(true),
  );
});
```

`LessonStepRow.test.tsx` additions:

```tsx
it("labels an external prerequisite with the subject it comes from", async () => {
  stubKcLookup();
  renderRow(step({ kc_id: "kc-projection", step_type: "detour", status: "pending",
    detour_for: "kc-least-squares", detour_reason: "external",
    source_subject_name: "Linear Algebra" } as Partial<LessonStep>));
  expect(await screen.findByText("From Linear Algebra")).toBeInTheDocument();
  expect(await screen.findByText(/Needed for Least squares/)).toBeInTheDocument();
});

it("marks a component being confirmed", async () => {
  stubKcLookup();
  renderRow(step({ check_first: true } as Partial<LessonStep>));
  expect(await screen.findByText("Confirming what you already know")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run** vitest on the two files → FAIL.

- [ ] **Step 3: Hooks** — `hooks.ts`:

```ts
/** Endorsed concept links this learner can act on (S24): undecided ones to accept or decline,
 * accepted ones to revoke. Learner-wide; a subject page filters to the ones touching it. */
export function useConceptLinkSuggestions() {
  return useQuery({
    queryKey: ["concept-link-suggestions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/concept-links/suggestions");
      if (error) throw error;
      return data;
    },
  });
}

/** Accept, decline or revoke a link. Either side's plan may change (a head start given or
 * withdrawn), so every plan is refetched, as are the suggestions. */
export function useDecideConceptLink() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ linkId, decision }: { linkId: string; decision: "accept" | "decline" | "revoke" }) => {
      const { data, error } = await api.POST("/api/v1/concept-links/{link_id}/decision", {
        params: { path: { link_id: linkId } },
        body: { decision },
      });
      if (error) throw error;
      return data;
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["concept-link-suggestions"] });
      queryClient.invalidateQueries({ queryKey: ["lesson-plan"] });
    },
  });
}
```

- [ ] **Step 4: Panel** — `ConnectionsPanel.tsx`:

```tsx
import { Link2 } from "lucide-react";
import { useConceptLinkSuggestions, useDecideConceptLink } from "../../api/hooks";

/** Ideas this subject shares with another one, and whether to use what the learner showed
 * there (S24). Nothing carries over until they say so: a suggestion is the endorser's half of
 * the agreement, and these buttons are the learner's. Renders nothing when there is nothing to
 * decide. */
export function ConnectionsPanel({ subjectId }: { subjectId: string }) {
  const { data } = useConceptLinkSuggestions();
  const decide = useDecideConceptLink();
  const here = (data ?? []).filter((s) => s.a.subject_id === subjectId || s.b.subject_id === subjectId);
  if (here.length === 0) return null;
  return (
    <section className="flex max-w-2xl flex-col gap-2" aria-label="Connections">
      <h2 className="text-h3 flex items-center gap-2">
        <Link2 size={16} className="text-primary" /> Connections
      </h2>
      {here.map((s) => {
        const [mine, other] = s.a.subject_id === subjectId ? [s.a, s.b] : [s.b, s.a];
        const act = (decision: "accept" | "decline" | "revoke") =>
          decide.mutate({ linkId: s.link_id, decision });
        return (
          <div key={s.link_id} className="rounded-field bg-base-200 flex flex-col gap-1 px-3 py-2">
            <p className="text-body">
              {mine.kc_name} here looks like the same idea as {other.kc_name} in {other.subject_name}.
            </p>
            {s.reason && <p className="text-caption text-base-content/60">{s.reason}</p>}
            <div className="flex gap-1">
              {s.decision === "accepted" ? (
                <button type="button" className="btn btn-ghost btn-xs" disabled={decide.isPending} onClick={() => act("revoke")}>
                  Stop using it
                </button>
              ) : (
                <>
                  <button type="button" className="btn btn-primary btn-xs" disabled={decide.isPending} onClick={() => act("accept")}>
                    Use it here
                  </button>
                  <button type="button" className="btn btn-ghost btn-xs" disabled={decide.isPending} onClick={() => act("decline")}>
                    Not the same
                  </button>
                </>
              )}
            </div>
          </div>
        );
      })}
    </section>
  );
}
```

(The first test's `/Derivatives in Calculus/` matches "…same idea as Derivatives in Calculus.")

Mount in `Lessons.tsx` for every selected subject (not only owned — a curated subject can be linked too), after `LessonPlanPanel`: `{selectedId && <ConnectionsPanel key={`links-${selectedId}`} subjectId={selectedId} />}`.

- [ ] **Step 5: Step row** — `LessonStepRow.tsx`:
  - `const isExternal = isDetour && step.detour_reason === "external";`
  - Right-hand label: `isExternal ? \`From ${step.source_subject_name ?? "another subject"}\` : …existing…`.
  - Caption for an external detour (replace the struggle wording when `isExternal`): `blockedName ? \`Needed for ${blockedName}.\` : "Needed for a later step."`; suppress the `detour_reason` suffix for externals (it is the literal `"external"`).
  - Under any non-settled row with `step.check_first`, render `<p className="text-caption text-primary pl-9">Confirming what you already know</p>`.
  - Update the component docstring with one paragraph on each.

- [ ] **Step 6: Verify** — `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run build`, `npm run lint` → green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/hooks.ts frontend/src/pages/Lessons.tsx frontend/src/components/lessons/ConnectionsPanel.tsx frontend/src/components/lessons/ConnectionsPanel.test.tsx frontend/src/components/lessons/LessonStepRow.tsx frontend/src/components/lessons/LessonStepRow.test.tsx
git status
git commit -m "feat(frontend): let a learner use what they know from another subject [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: The administrator's concept-link queue [S24]

**Files:**
- Modify: `frontend/src/api/admin.ts`, `frontend/src/pages/Admin.tsx`
- Create: `frontend/src/components/ConceptLinkQueue.tsx`, `frontend/src/components/ConceptLinkQueue.test.tsx`

**Interfaces:**
- Consumes: Task 8 admin endpoints (`ConceptLinkReviewRead`).
- Produces: `useConceptLinkQueue()`, `useConceptLinkVerdict()`, `<ConceptLinkQueue />`.

- [ ] **Step 1: Failing test** — `ConceptLinkQueue.test.tsx` (fetch stub as in `PublicationQueue.test.tsx`):

```tsx
const row = {
  id: "l-1", kc_a_id: "a", kc_b_id: "b", kc_a_name: "Matrices", kc_b_name: "Matrices",
  subject_a_name: "Linear Algebra", subject_b_name: "Graphics",
  verdict: null, reason: null, decided_at: null,
};

it("needs a reason before a verdict can be sent", async () => {
  const calls = stub([row]);
  renderQueue();
  expect(await screen.findByText(/Matrices \(Linear Algebra\)/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Endorse" })).toBeDisabled();
  await userEvent.type(screen.getByLabelText("Reason"), "Same object in both.");
  await userEvent.click(screen.getByRole("button", { name: "Endorse" }));
  await waitFor(() =>
    expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/admin/concept-links/l-1"))).toBe(true),
  );
});

it("shows a decided link's verdict instead of controls", async () => {
  stub([{ ...row, verdict: "rejected", reason: "Different use.", decided_at: "2026-09-25T10:00:00Z" }]);
  renderQueue();
  expect(await screen.findByText("Rejected — Different use.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Endorse" })).toBeNull();
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Hooks** — `admin.ts`, following its existing query/mutation style:

```ts
/** Curated concept-link candidates, undecided first (S24). */
export function useConceptLinkQueue() {
  return useQuery({
    queryKey: ["admin", "concept-links"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/admin/concept-links");
      if (error) throw error;
      return data;
    },
  });
}

export function useConceptLinkVerdict() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ linkId, endorse, reason }: { linkId: string; endorse: boolean; reason: string }) => {
      const { data, error } = await api.POST("/api/v1/admin/concept-links/{link_id}", {
        params: { path: { link_id: linkId } },
        body: { endorse, reason },
      });
      if (error) throw error;
      return data;
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["admin", "concept-links"] }),
  });
}
```

- [ ] **Step 4: Component** — `ConceptLinkQueue.tsx`:

```tsx
import { useState } from "react";
import { useConceptLinkQueue, useConceptLinkVerdict } from "../api/admin";
import type { components } from "../api/schema";

type Row = components["schemas"]["ConceptLinkReviewRead"];

/** Pairs of curated components that share a concept name, for an administrator to rule on
 * (S24). An endorsement is half a link — learners still choose — so the reason is required:
 * it is what a learner reads when deciding. Decided rows stay listed with their verdict. */
export function ConceptLinkQueue() {
  const { data, isLoading } = useConceptLinkQueue();
  if (isLoading) return <p className="text-caption text-base-content/50">Loading concept links…</p>;
  if (!data || data.length === 0) {
    return <p className="text-caption text-base-content/50">No concept links to review.</p>;
  }
  return (
    <div className="flex flex-col gap-2">
      {data.map((row) => <LinkRow key={row.id} row={row} />)}
    </div>
  );
}

function LinkRow({ row }: { row: Row }) {
  const [reason, setReason] = useState("");
  const verdict = useConceptLinkVerdict();
  const send = (endorse: boolean) => verdict.mutate({ linkId: row.id, endorse, reason: reason.trim() });
  return (
    <div className="rounded-field bg-base-200 flex flex-col gap-2 px-3 py-2">
      <p className="text-body">
        {row.kc_a_name} ({row.subject_a_name}) ↔ {row.kc_b_name} ({row.subject_b_name})
      </p>
      {row.verdict ? (
        <p className="text-caption text-base-content/60">
          {row.verdict === "endorsed" ? "Endorsed" : "Rejected"} — {row.reason}
        </p>
      ) : (
        <div className="flex items-end gap-2">
          <label className="flex flex-1 flex-col gap-1">
            <span className="text-caption">Reason</span>
            <input className="input input-sm" value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button type="button" className="btn btn-primary btn-sm" disabled={!reason.trim() || verdict.isPending} onClick={() => send(true)}>
            Endorse
          </button>
          <button type="button" className="btn btn-ghost btn-sm" disabled={!reason.trim() || verdict.isPending} onClick={() => send(false)}>
            Reject
          </button>
        </div>
      )}
    </div>
  );
}
```

Mount in `Admin.tsx` beside `PublicationQueue`, under a heading "Concept links", using the same section markup the publication queue uses.

- [ ] **Step 5: Verify** — vitest, `npm run build`, `npm run lint` → green.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/api/admin.ts frontend/src/pages/Admin.tsx frontend/src/components/ConceptLinkQueue.tsx frontend/src/components/ConceptLinkQueue.test.tsx
git status
git commit -m "feat(frontend): give administrators a concept-link review queue [S24]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Spec coverage

| Spec | Task |
|---|---|
| §2.1 row lock, sorted order | 1 |
| §2.2 attempt id across retries | 2 |
| §3.1 serialized edge writes | 3 (global lock — Deviation 1) |
| §3.2 conflicts panel | 4 (Deviation 2) |
| §4.1 candidates | 5 |
| §4.2 admin endorsement | 5, 8, 12 |
| §4.2 LLM judge, background, failure handling, privacy | 6 |
| §4.2 learner accept / decline / revoke | 8, 11 |
| §4.3 data, `links_in_effect` | 5 (Deviation 3) |
| §4.4 endpoints, sweep | 8 |
| §5.1 seed | 7 |
| §5.2 provisional, confirmation, achievement | 7 |
| §5.3 check-first | 9, 10 |
| §5.4 revoke | 7, 8 |
| §6 planning cases 1–4, one level | 9 (Deviation 4) |
| §7 frontend | 4, 11, 12 (Deviation 5) |
| §8 settings + knobs | 7 |
| §9 errors | 3, 6, 8 |
| §10 tests 1–17 | 1–12 |
