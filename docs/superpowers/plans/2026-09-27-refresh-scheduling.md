# Refresh Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Memory write-back and profile refresh run on their own when a conversation or learner goes quiet, a backlog catches up by itself, the profile reads a bounded window and pays for a model call only when that estimator's input changed, admin-visit messages stop counting as evidence, and a learner can pause memory.

**Architecture:** A new service `app/services/refresh_schedule.py` answers "what is due" from stored data and claims items with conditional updates; a worker loop calls it every few minutes and queues the existing write-back task and a new profile-refresh task. `refresh_profile` loads recency windows and skips model-backed estimators whose input fingerprint is unchanged. `learners.remember_conversations` gates every write-back path.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, taskiq, pytest; React + TypeScript, TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-27-refresh-scheduling-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`; API changes also `uv run poe api-types` then stage `frontend/src/api/schema.d.ts`, then `uv run poe api-contract`.
- Frontend changes also green on `cd frontend && npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npm run lint`.
- New migration → `uv run python -m tests.testdb`. Alembic revision ids ≤ 32 characters.
- Times in these tables are naive UTC (`datetime.now(UTC).replace(tzinfo=None)`), like `memory_watermark` and `evidence_watermark`.
- Settings defaults (verbatim from the spec): `refresh_poll_interval_seconds = 300` (0 disables), `memory_quiet_minutes = 20`, `refresh_retry_minutes = 60`, `refresh_batch_size = 50`, `refresh_stuck_hours = 6`, `profile_event_window = 2000`, `profile_message_window = 500`.
- Admin-visit messages are those with `admin_actor_id` or `admin_action_id` set; they never count as evidence or make anything due.
- No paid model calls: tests use `fake_llm_client`.
- Async tests: re-read with `populate_existing=True`; read ids into locals before code that commits or rolls back.
- One tracker id per commit subject: `[S43]`. Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.

## Review Focus

1. A conversation whose unprocessed backlog is larger than the extraction window: after a successful run it is still due and is claimed again on the next pass, not after the 60-minute retry window (Task 3 test: success clears the claim).
2. Memory paused (or the account closing) *after* a claim but before the job runs: the job makes no model call and learns nothing (Task 3 test).
3. A conversation where only an administrator wrote since the watermark: not due, and the admin message does not move `latest_evidence_at` (Tasks 1 and 4 tests).
4. A learner with evidence but no `learner_profiles` row, or a row with no watermark: due, and claiming creates the row (Task 4 test).
5. Two workers claiming the same item in the same instant: the second claims nothing (Task 4 test).

---

### Task 1: Schema, settings, and admin messages stop counting as evidence [S43]

**Files:**
- Create: `db/migrations/versions/0070_refresh_scheduling.py`
- Modify: `app/models/learner.py`, `app/models/chat.py` (`Conversation`), `app/models/profile.py` (`LearnerProfile`, `ProfileDimension`), `app/core/config.py`, `app/services/profile.py` (`latest_evidence_at`)
- Test: `tests/test_migrations_with_data.py` (append), `tests/test_refresh_cursors.py` (append)

**Interfaces:**
- Produces: `Learner.remember_conversations: bool` (default True); `Conversation.memory_attempted_at: datetime | None`; `LearnerProfile.refresh_attempted_at: datetime | None`; `ProfileDimension.input_fingerprint: str | None`; the seven settings in Global Constraints.

- [ ] **Step 1: Failing tests.** Append to `tests/test_refresh_cursors.py`:

```python
async def test_an_admin_visit_message_is_not_new_evidence(db_session: AsyncSession) -> None:
    """S43: an admin visit made the profile look stale and paid for a recompute that could
    not change anything, since the estimators already ignore those messages."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "mine", at=_t(0))
    db_session.add(
        Message(
            conversation_id=conv.id,
            role="user",
            content="typed by an administrator",
            created_at=_t(10),
            admin_actor_id=uuid.uuid4(),
        )
    )
    await db_session.commit()

    assert await profile_svc.latest_evidence_at(db_session, learner.id) == _t(0)
```

Append to `tests/test_migrations_with_data.py`:

```python
async def test_refresh_scheduling_columns_default_sensibly() -> None:
    """0070 (S43): existing learners keep memory on; nothing starts claimed."""
    async with database_at("0069_memory_forgotten_scope") as connect:
        conn = await connect()
        try:
            learner_id, conversation_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "sched"
            )
            await conn.execute(
                "INSERT INTO conversations (id, learner_id, kind, phase) "
                "VALUES ($1, $2, 'chat', 'chatting')",
                conversation_id,
                learner_id,
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0070_refresh_scheduling")

        conn = await connect()
        try:
            remember = await conn.fetchval(
                "SELECT remember_conversations FROM learners WHERE id = $1", learner_id
            )
            attempted = await conn.fetchval(
                "SELECT memory_attempted_at FROM conversations WHERE id = $1", conversation_id
            )
            assert remember is True and attempted is None
        finally:
            await conn.close()
```

(`SCRATCH` and `upgrade` are already imported/defined in that file — check the slice D test above it for the exact names and match them.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_refresh_cursors.py tests/test_migrations_with_data.py -q -k "admin_visit or refresh_scheduling"` → both FAIL (evidence is `_t(10)`; no revision 0070).

- [ ] **Step 3: Implement.**

Migration `db/migrations/versions/0070_refresh_scheduling.py`:

```python
"""Refresh scheduling (S43): memory consent, claim stamps, and estimator input fingerprints.

``learners.remember_conversations`` is the learner's switch for memory; existing learners keep
it on, which is today's behaviour. ``memory_attempted_at`` / ``refresh_attempted_at`` are the
scheduler's claims: set when a pass queues the work, cleared when it succeeds, so a failure is
retried after a delay rather than every pass. ``input_fingerprint`` records what a model-backed
estimator last judged, so an unchanged sample costs no model call.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0070_refresh_scheduling"
down_revision: str | Sequence[str] | None = "0069_memory_forgotten_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learners",
        sa.Column(
            "remember_conversations", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column("conversations", sa.Column("memory_attempted_at", sa.DateTime(), nullable=True))
    op.add_column(
        "learner_profiles", sa.Column("refresh_attempted_at", sa.DateTime(), nullable=True)
    )
    op.add_column("profile_dimensions", sa.Column("input_fingerprint", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("profile_dimensions", "input_fingerprint")
    op.drop_column("learner_profiles", "refresh_attempted_at")
    op.drop_column("conversations", "memory_attempted_at")
    op.drop_column("learners", "remember_conversations")
```

Models:
- `Learner` (after `deletion_due_at`):
  ```python
      # The learner's switch for memory (S43). Off: nothing new is learned from their
      # conversations, by the scheduler or on request; what is already remembered stays usable.
      remember_conversations: Mapped[bool] = mapped_column(server_default=true(), default=True)
  ```
  (import `true` from `sqlalchemy` beside `false`).
- `Conversation` (after `memory_watermark`):
  ```python
      # The scheduler's claim on this conversation's write-back (S43): set when a pass queues
      # it, cleared when it succeeds. A failed run is retried after a delay, not every pass.
      memory_attempted_at: Mapped[datetime | None] = mapped_column(default=None)
  ```
- `LearnerProfile` (after `last_error`): `refresh_attempted_at: Mapped[datetime | None] = mapped_column(default=None)` with the same comment for the profile.
- `ProfileDimension`: `input_fingerprint: Mapped[str | None] = mapped_column(Text, default=None)` with a comment: "What a model-backed estimator last judged (S43): the same fingerprint means the same answer, so no model call."

Settings in `app/core/config.py`, beside `memory_extraction_window`:

```python
    # Refresh scheduling (S43). The worker looks for conversations and learners that have gone
    # quiet with unprocessed evidence every `refresh_poll_interval_seconds` (0 disables it),
    # claims at most `refresh_batch_size` of each per pass, and retries a failed item after
    # `refresh_retry_minutes`. `refresh_stuck_hours` is when a due item raises an alert. The
    # profile reads only the most recent events/messages. All uncalibrated.
    refresh_poll_interval_seconds: int = 300
    memory_quiet_minutes: int = 20
    refresh_retry_minutes: int = 60
    refresh_batch_size: int = 50
    refresh_stuck_hours: int = 6
    profile_event_window: int = 2000
    profile_message_window: int = 500
```

`latest_evidence_at`: add `Message.admin_actor_id.is_(None), Message.admin_action_id.is_(None)` to the message query's `where`, and one sentence to the docstring: "Admin-visit messages are excluded like everywhere else the learner's evidence is read (S43)."

- [ ] **Step 4: Run** — `uv run python -m tests.testdb`; the two tests → PASS; `uv run pytest tests/test_refresh_cursors.py tests/test_migrations_with_data.py tests/test_profile.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0070_refresh_scheduling.py app/models/learner.py app/models/chat.py app/models/profile.py app/core/config.py app/services/profile.py tests/test_refresh_cursors.py tests/test_migrations_with_data.py
git status
git commit -m "feat(profile): scheduling columns, and admin-visit messages are not evidence [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The profile reads a window and pays only for changed input [S43]

**Files:**
- Modify: `app/learning/profile_estimators.py` (`FingerprintFn`, `DimensionSpec.fingerprint`, three fingerprints, `_recent_goals`), `app/services/profile.py` (`_load_events`, `_load_own_messages`, `refresh_profile`, `_upsert_dimension`)
- Test: `tests/test_refresh_cursors.py` (append), `tests/test_query_budgets.py` (append)

**Interfaces:**
- Consumes: `ProfileDimension.input_fingerprint`, `LearnerProfile.refresh_attempted_at`, `profile_event_window`, `profile_message_window` (Task 1).
- Produces: `FingerprintFn = Callable[[EstimatorContext], Awaitable[str | None]]`; `DimensionSpec.fingerprint: FingerprintFn | None = None`; `refresh_profile` clears `refresh_attempted_at` on success.

- [ ] **Step 1: Failing tests.** Append to `tests/test_refresh_cursors.py` (add `from app.models.learning import LearningEvent` and `from app.core.config import get_settings` if not present):

```python
# --- the profile reads a window and pays only for changed input (S43) --------------------


def _answer(learner_id: uuid.UUID, *, at: datetime, score: float = 1.0) -> LearningEvent:
    return LearningEvent(
        learner_id=learner_id,
        event_type="observation",
        payload={"score": score, "difficulty": 0.5, "latency_ms": 5000, "hints_used": 0},
        created_at=at,
    )


async def test_new_evidence_the_model_would_not_see_costs_no_model_call(
    db_session: AsyncSession,
) -> None:
    """Interests reads the learner's messages; a new correct answer changes none of them."""
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I love astronomy and chess.", at=_t(0))
    llm = fake_llm_client('["astronomy"]')
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    calls = await _llm_calls(db_session, learner.id)
    assert calls >= 1  # interests ran

    db_session.add(_answer(learner.id, at=_t(5)))
    await db_session.commit()
    await profile_svc.refresh_profile(db_session, learner.id, llm)

    assert await _llm_calls(db_session, learner.id) == calls


async def test_a_changed_sample_is_judged_again(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I love astronomy.", at=_t(0))
    llm = fake_llm_client('["astronomy"]')
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    calls = await _llm_calls(db_session, learner.id)

    await _say(db_session, conv, "Also chess, lately.", at=_t(5))
    await profile_svc.refresh_profile(db_session, learner.id, llm)

    assert await _llm_calls(db_session, learner.id) > calls


async def test_force_judges_again_even_when_the_sample_is_unchanged(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "I love astronomy.", at=_t(0))
    llm = fake_llm_client('["astronomy"]')
    await profile_svc.refresh_profile(db_session, learner.id, llm)
    calls = await _llm_calls(db_session, learner.id)

    await profile_svc.refresh_profile(db_session, learner.id, llm, force=True)

    assert await _llm_calls(db_session, learner.id) > calls


async def test_only_the_most_recent_evidence_is_read(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    for minute in range(5):
        await _say(db_session, conv, f"message {minute}", at=_t(minute))
        db_session.add(_answer(learner.id, at=_t(minute)))
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.profile.get_settings",
        lambda: Settings(profile_event_window=2, profile_message_window=3),
    )

    events = await profile_svc._load_events(db_session, learner.id)
    messages = await profile_svc._load_own_messages(db_session, learner.id)

    assert [e.created_at for e in events] == [_t(3), _t(4)]
    assert [m.content for m in messages] == ["message 2", "message 3", "message 4"]


async def test_a_successful_refresh_releases_the_schedulers_claim(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    conv = await _conversation(db_session, learner)
    await _say(db_session, conv, "hello", at=_t(0))
    profile = LearnerProfile(learner_id=learner.id, refresh_attempted_at=_t(1))
    db_session.add(profile)
    await db_session.commit()

    await profile_svc.refresh_profile(db_session, learner.id, fake_llm_client("{}"))

    await db_session.refresh(profile)
    assert profile.refresh_attempted_at is None
```

Append to `tests/test_query_budgets.py` (reuse its imports; add what is missing):

```python
async def test_profile_refresh_does_not_query_per_piece_of_evidence(
    db_session: AsyncSession,
) -> None:
    """S43: the window bounds what is read; this bounds how many round trips reading it takes."""
    from app.llm.registry import fake_llm_client
    from app.models.chat import Conversation, Message
    from app.models.learner import Learner
    from app.models.learning import LearningEvent
    from app.services import profile as profile_svc

    async def learner_with(n: int) -> uuid.UUID:
        learner = Learner(handle=f"q-{uuid.uuid4().hex[:8]}")
        db_session.add(learner)
        await db_session.flush()
        conv = Conversation(learner_id=learner.id)
        db_session.add(conv)
        await db_session.flush()
        for i in range(n):
            db_session.add(Message(conversation_id=conv.id, role="user", content=f"m{i}"))
            db_session.add(
                LearningEvent(
                    learner_id=learner.id,
                    event_type="observation",
                    payload={"score": 1.0, "difficulty": 0.5, "latency_ms": 4000, "hints_used": 0},
                )
            )
        await db_session.commit()
        return learner.id

    llm = fake_llm_client("{}")
    small, large = await learner_with(3), await learner_with(40)
    with count_queries(db_session) as small_count:
        await profile_svc.refresh_profile(db_session, small, llm)
    with count_queries(db_session) as large_count:
        await profile_svc.refresh_profile(db_session, large, llm)

    assert len(large_count) == len(small_count), (
        f"query count grew with the history\nsmall: {small_count}\nlarge: {large_count}"
    )
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_refresh_cursors.py tests/test_query_budgets.py -q` → the unchanged-sample, window and claim-release tests FAIL (the force and changed-sample tests may already pass — they pin behaviour that must survive; the budget test may already pass — it pins it).

- [ ] **Step 3: Implement.**

`app/learning/profile_estimators.py`:

```python
FingerprintFn = Callable[[EstimatorContext], Awaitable[str | None]]
"""What a model-backed estimator would send, reduced to a digest (S43). The same digest means
the same input and therefore the same answer, so the refresh keeps the stored value and makes
no call. ``None`` means "no fingerprint": always estimate."""


def _digest(parts: Iterable[str]) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
```

Add to `DimensionSpec`, last field: `fingerprint: FingerprintFn | None = None` with the comment "Only model-backed estimators set it (S43)." Imports: `hashlib`, `Iterable` from `collections.abc`.

The three fingerprints, each beside its estimator, sharing the estimator's own sample selection so the two can never drift apart:

```python
def _incorrect_sample(ctx: EstimatorContext) -> list[LearningEvent]:
    incorrect = [
        e
        for e in _observations(ctx.events)
        if e.payload.get("score", 1.0) < 0.5 and e.payload.get("item_id")
    ]
    return incorrect[-ERROR_TYPE_MAX_SAMPLE:]


async def _fingerprint_error_type(ctx: EstimatorContext) -> str | None:
    return _digest(str(e.id) for e in _incorrect_sample(ctx))
```

and change `_estimate_error_type` to start from `sample = _incorrect_sample(ctx)` (keep its `ERROR_TYPE_MIN_INCORRECT` checks: the minimum applies to the sample, which is the same list as before whenever the full list had at least the minimum — if the full incorrect list is shorter than `ERROR_TYPE_MAX_SAMPLE` the sample *is* the full list).

```python
async def _recent_goals(ctx: EstimatorContext) -> list[str]:
    return [
        g
        for g in (
            await ctx.session.scalars(
                select(Conversation.goal)
                .where(Conversation.learner_id == ctx.learner_id, Conversation.goal.is_not(None))
                .order_by(Conversation.created_at.desc())
                .limit(5)
            )
        ).all()
        if g
    ]


async def _fingerprint_goal_orientation(ctx: EstimatorContext) -> str | None:
    return _digest(await _recent_goals(ctx))
```

and `_estimate_goal_orientation` starts with `goals = await _recent_goals(ctx)`.

```python
def _interest_sample(ctx: EstimatorContext) -> list[Message]:
    return [m for m in ctx.messages if m.content.strip()][-INTERESTS_MAX_MESSAGES:]


async def _fingerprint_interests(ctx: EstimatorContext) -> str | None:
    return _digest(str(m.id) for m in _interest_sample(ctx))
```

and `_estimate_interests` builds `own_messages` from `_interest_sample(ctx)` (content as before).

Set `fingerprint=_fingerprint_error_type`, `fingerprint=_fingerprint_goal_orientation`, `fingerprint=_fingerprint_interests` on the three `DimensionSpec` entries. Update the `EstimatorContext` docstring: "``events``/``messages`` are the learner's most recent history (the refresh's window, S43)…".

`app/services/profile.py`:

```python
async def _load_events(session: AsyncSession, learner_id: uuid.UUID) -> list[LearningEvent]:
    """The learner's most recent events, oldest first (S43: a window, not the whole history)."""
    rows = (
        await session.scalars(
            select(LearningEvent)
            .where(LearningEvent.learner_id == learner_id)
            .order_by(LearningEvent.created_at.desc(), LearningEvent.id.desc())
            .limit(get_settings().profile_event_window)
        )
    ).all()
    return list(reversed(rows))
```

`_load_own_messages` the same way: `.order_by(Message.created_at.desc(), Message.id.desc()).limit(get_settings().profile_message_window)`, then reversed. Import `get_settings`.

`_upsert_dimension` gains `fingerprint: str | None` and sets `input_fingerprint=fingerprint` on insert and `dim.input_fingerprint = fingerprint` on update.

In `refresh_profile`, before the loop:

```python
        stored = {
            key: fingerprint
            for key, fingerprint in (
                await session.execute(
                    select(ProfileDimension.key, ProfileDimension.input_fingerprint).where(
                        ProfileDimension.learner_id == learner_id
                    )
                )
            ).all()
        }
```

and the loop becomes:

```python
        for spec in DIMENSION_SPECS:
            fingerprint = await spec.fingerprint(context) if spec.fingerprint else None
            if not force and fingerprint is not None and stored.get(spec.key) == fingerprint:
                continue  # the same input as last time: the stored answer stands, unpaid
            result, usage = await spec.estimate(context)
            ...  # logging unchanged
            if result is not None:
                await _upsert_dimension(session, learner_id, spec, result, fingerprint)
```

On success, beside `profile.last_error = None`: `profile.refresh_attempted_at = None`. Update the module and `refresh_profile` docstrings: the recompute reads a recency window, and a model-backed dimension whose input is unchanged keeps its value without a call (S43); delete the sentence saying making the estimators incremental is a different, larger change.

- [ ] **Step 4: Run** — `uv run pytest tests/test_refresh_cursors.py tests/test_query_budgets.py tests/test_profile.py tests/test_profile_estimators.py tests/test_profile_proxies.py -q` → PASS; `uv run poe check && uv run poe format-check` → green. If an existing estimator test builds a `DimensionSpec` positionally, the new field has a default and nothing changes.

- [ ] **Step 5: Commit**

```bash
git add app/learning/profile_estimators.py app/services/profile.py tests/test_refresh_cursors.py tests/test_query_budgets.py
git status
git commit -m "feat(profile): read a recent window and skip unchanged model input [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The memory setting, and write-back respects it [S43]

**Files:**
- Modify: `app/services/memory.py` (`write_back`), `app/api/v1/memory.py` (setting routes; 409 on write-back), `app/schemas/memory.py` (`MemorySetting`), `frontend/src/api/schema.d.ts`
- Test: `tests/test_memory.py` (append), `tests/test_admin_sudo.py` (append)

**Interfaces:**
- Consumes: `Learner.remember_conversations`, `Conversation.memory_attempted_at` (Task 1).
- Produces: `class MemorySetting(BaseModel): remember: bool`; `GET /api/v1/me/memory-setting` and `PUT /api/v1/me/memory-setting` → `MemorySetting`; write-back endpoint 409 `{"code": "memory_paused", "message": ...}`; `write_back` returns `[]` with no model call when the learner is paused, pending deletion or suspended, and clears `memory_attempted_at` whenever it completes.

- [ ] **Step 1: Failing tests.** Append to `tests/test_memory.py` (add imports as needed: `Learner` is already imported; `datetime`/`UTC` from `datetime`):

```python
# --- the memory setting (S43) ----------------------------------------------------------------


async def test_a_paused_learner_learns_nothing_and_pays_nothing(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"p-{uuid.uuid4().hex[:8]}", remember_conversations=False)
    db_session.add(learner)
    await db_session.flush()
    conv = Conversation(learner_id=learner.id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role="user", content="I study at night."))
    await db_session.commit()
    llm = fake_llm_client('{"memories": [{"kind": "fact", "content": "Studies at night."}]}')

    assert await svc.write_back(db_session, llm, conversation_id=conv.id) == []
    assert llm._providers["fake"].prompts_sent == []  # type: ignore[attr-defined]


async def test_a_closing_account_learns_nothing(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"c-{uuid.uuid4().hex[:8]}", deletion_requested_at=datetime.now(UTC))
    db_session.add(learner)
    await db_session.flush()
    conv = Conversation(learner_id=learner.id)
    db_session.add(conv)
    await db_session.flush()
    db_session.add(Message(conversation_id=conv.id, role="user", content="I study at night."))
    await db_session.commit()

    llm = fake_llm_client('{"memories": [{"kind": "fact", "content": "Studies at night."}]}')
    assert await svc.write_back(db_session, llm, conversation_id=conv.id) == []


async def test_a_finished_write_back_releases_the_claim(db_session: AsyncSession) -> None:
    """Review focus 1: a backlog bigger than the window stays due and must be claimable on the
    next pass, not after the retry delay meant for failures."""
    learner = Learner(handle=f"w-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    conv = Conversation(learner_id=learner.id, memory_attempted_at=datetime(2026, 1, 1))
    db_session.add(conv)
    await db_session.flush()
    conv_id = conv.id
    db_session.add(Message(conversation_id=conv_id, role="user", content="hello"))
    await db_session.commit()

    await svc.write_back(db_session, fake_llm_client('{"memories": []}'), conversation_id=conv_id)

    again = await db_session.get(Conversation, conv_id, populate_existing=True)
    assert again is not None and again.memory_attempted_at is None


async def test_the_setting_defaults_on_and_pausing_refuses_write_back(
    api_client: AsyncClient,
) -> None:
    assert (await api_client.get(f"{API}/me/memory-setting")).json() == {"remember": True}

    r = await api_client.put(f"{API}/me/memory-setting", json={"remember": False})
    assert r.status_code == 200 and r.json() == {"remember": False}
    assert (await api_client.get(f"{API}/me/memory-setting")).json() == {"remember": False}

    conv = (await api_client.post(f"{API}/conversations", json={})).json()
    refused = await api_client.post(f"{API}/conversations/{conv['id']}/memory/write-back")
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "memory_paused"


async def test_what_is_remembered_is_still_used_while_paused(
    db_session: AsyncSession,
) -> None:
    """Pausing stops learning; it does not hide what is already known."""
    learner = Learner(handle=f"k-{uuid.uuid4().hex[:8]}", remember_conversations=False)
    db_session.add(learner)
    await db_session.flush()
    db_session.add(
        Memory(
            embedding_space=FAKE_SPACE,
            learner_id=learner.id,
            kind=MemoryKind.FACT,
            content="Studies at night.",
            embedding=[0.1] * get_settings().embed_dim,
        )
    )
    await db_session.commit()

    assert [m.content for m in await svc.list_memories(db_session, learner.id)] == [
        "Studies at night."
    ]


async def test_the_setting_is_exported(api_client: AsyncClient) -> None:
    await api_client.put(f"{API}/me/memory-setting", json={"remember": False})
    exported = (await api_client.get(f"{API}/me/export")).json()
    assert exported["learner"]["remember_conversations"] is False
```

(Use `cast(FakeProvider, llm._providers["fake"]).prompts_sent` instead of the `type: ignore` if `ty` rejects it, as the slice D tests do. If `MemoryKind.FACT` is not the enum member's name, read `app/models/memory.py`.)

Append to `tests/test_admin_sudo.py`:

```python
async def test_pausing_memory_during_a_visit_is_recorded(
    admin_client: AsyncClient, anon_client: AsyncClient, api_learner: Learner
) -> None:
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    r = await anon_client.put("/api/v1/me/memory-setting", json={"remember": False})
    assert r.status_code == 200, r.text
    log = await admin_client.get(
        f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
    )
    assert any(a["route"] == "/api/v1/me/memory-setting" for a in log.json())
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_memory.py tests/test_admin_sudo.py -q` → the new tests FAIL (routes 404; write-back runs for a paused learner; claim not cleared). `test_what_is_remembered_is_still_used_while_paused` passes already — it pins that pausing must not hide memories.

- [ ] **Step 3: Implement.**

`app/services/memory.py` `write_back`, right after `if conversation is None: return []`:

```python
    learner = await session.get(Learner, conversation.learner_id)
    if (
        learner is None
        or not learner.remember_conversations
        or learner.deletion_requested_at is not None
        or learner.suspended_at is not None
    ):
        # Paused (S43), or the account closed or was suspended after this was queued: no model
        # call and nothing learned. What is already remembered is untouched.
        return []
```

Every successful exit clears the scheduler's claim — the `not window` early return becomes:

```python
    if not window:
        # Nothing new since the last run. Returning here is the difference between a repeated
        # write-back being free and it costing a FAST call to rediscover it had nothing to do.
        conversation.memory_attempted_at = None
        await session.commit()
        return []
```

and set `conversation.memory_attempted_at = None` immediately after `conversation.memory_watermark = window[-1].created_at` (both later commits then carry it). Import `Learner` from `app.models.learner`. Docstring: add "Nothing is learned while the learner has memory paused (S43)."

`app/schemas/memory.py`:

```python
class MemorySetting(BaseModel):
    """Whether Guru learns new things from this learner's conversations (S43)."""

    remember: bool
```

`app/api/v1/memory.py`:

```python
@router.get("/me/memory-setting", response_model=MemorySetting)
async def memory_setting(learner: CurrentLearner):
    return MemorySetting(remember=learner.remember_conversations)


@router.put("/me/memory-setting", response_model=MemorySetting)
async def set_memory_setting(body: MemorySetting, session: SessionDep, learner: CurrentLearner):
    """Pause or resume memory (S43). Pausing stops learning; what is remembered stays."""
    row = await session.get(Learner, learner.id)
    assert row is not None
    row.remember_conversations = body.remember
    await session.commit()
    return MemorySetting(remember=row.remember_conversations)
```

and in the write-back endpoint, after the 404 check:

```python
    if not learner.remember_conversations:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "memory_paused", "message": "Memory is paused for this account."},
        )
```

Import `Learner` and `MemorySetting`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_memory.py tests/test_admin_sudo.py tests/test_memory_lifecycle.py tests/test_refresh_cursors.py -q` → PASS; `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage the schema; `uv run poe api-contract` → green. If a route-inventory test (e.g. in `tests/test_authorization.py` or `tests/test_visibility_sweep.py`) lists every authenticated route, add the two new routes there as learner-only routes with no graph ids, and note it in the ledger.

- [ ] **Step 5: Commit**

```bash
git add app/services/memory.py app/api/v1/memory.py app/schemas/memory.py frontend/src/api/schema.d.ts tests/test_memory.py tests/test_admin_sudo.py
git status
git commit -m "feat(memory): a learner can pause memory, and write-back respects it [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: What is due, claimed once [S43]

**Files:**
- Create: `app/services/refresh_schedule.py`, `tests/test_refresh_schedule.py`

**Interfaces:**
- Consumes: Task 1's columns and settings.
- Produces:
  ```python
  @dataclass(frozen=True) class Claimed: conversations: list[uuid.UUID]; learners: list[uuid.UUID]
  async def due_conversations(session, *, quiet_before: datetime, retry_before: datetime | None, limit: int | None) -> list[uuid.UUID]
  async def due_learners(session, *, quiet_before: datetime, retry_before: datetime | None, limit: int | None) -> list[uuid.UUID]
  async def claim_due(session, *, now: datetime, settings: Settings) -> Claimed
  async def stuck(session, *, now: datetime, settings: Settings) -> int
  ```

- [ ] **Step 1: Failing tests** — `tests/test_refresh_schedule.py`:

```python
"""Write-back and profile refresh run when a conversation or learner goes quiet (S43).

"Due" is read from the data on every pass, so a restart or a long absence is caught up by the
next pass without a catch-up job. A claim is a conditional update, so two workers never queue
the same item, and a failed item waits `refresh_retry_minutes` before it is tried again.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.profile import LearnerProfile
from app.services import refresh_schedule as sched

NOW = datetime(2026, 3, 1, 12, 0)
SETTINGS = Settings(memory_quiet_minutes=20, refresh_retry_minutes=60, refresh_batch_size=50)


def _ago(minutes: int) -> datetime:
    return NOW - timedelta(minutes=minutes)


async def _learner(session: AsyncSession, **kwargs) -> Learner:
    learner = Learner(handle=f"s-{uuid.uuid4().hex[:8]}", **kwargs)
    session.add(learner)
    await session.commit()
    return learner


async def _chat(
    session: AsyncSession, learner: Learner, *, said: datetime, **kwargs
) -> Conversation:
    conversation = Conversation(learner_id=learner.id, **kwargs)
    session.add(conversation)
    await session.flush()
    session.add(
        Message(conversation_id=conversation.id, role="user", content="hi", created_at=said)
    )
    await session.commit()
    return conversation


async def _claim(session: AsyncSession, now: datetime = NOW) -> sched.Claimed:
    return await sched.claim_due(session, now=now, settings=SETTINGS)


# --- conversations -------------------------------------------------------------------------


async def test_a_quiet_conversation_is_claimed_and_a_live_one_is_not(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    quiet = await _chat(db_session, learner, said=_ago(30))
    live = await _chat(db_session, learner, said=_ago(5))

    claimed = await _claim(db_session)

    assert quiet.id in claimed.conversations
    assert live.id not in claimed.conversations


async def test_what_was_already_read_is_not_due(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    done = await _chat(db_session, learner, said=_ago(30), memory_watermark=_ago(30))

    assert done.id not in (await _claim(db_session)).conversations


async def test_an_administrators_message_makes_nothing_due(db_session: AsyncSession) -> None:
    """Review focus 3."""
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(90), memory_watermark=_ago(90))
    db_session.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="from a visit",
            created_at=_ago(30),
            admin_actor_id=uuid.uuid4(),
        )
    )
    await db_session.commit()

    claimed = await _claim(db_session)

    assert conversation.id not in claimed.conversations
    assert learner.id not in claimed.learners  # its profile evidence did not move either


async def test_paused_closing_suspended_and_archived_are_left_alone(
    db_session: AsyncSession,
) -> None:
    paused = await _chat(
        db_session, await _learner(db_session, remember_conversations=False), said=_ago(30)
    )
    closing = await _chat(
        db_session,
        await _learner(db_session, deletion_requested_at=datetime.now(UTC)),
        said=_ago(30),
    )
    suspended = await _chat(
        db_session, await _learner(db_session, suspended_at=datetime.now(UTC)), said=_ago(30)
    )
    archived = await _chat(
        db_session, await _learner(db_session), said=_ago(30), archived_at=datetime.now(UTC)
    )

    claimed = (await _claim(db_session)).conversations

    assert not {paused.id, closing.id, suspended.id, archived.id} & set(claimed)


async def test_a_claim_waits_out_the_retry_window(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(30))
    assert conversation.id in (await _claim(db_session)).conversations

    # Still due (nothing ran), but claimed a minute ago: not again yet.
    assert conversation.id not in (await _claim(db_session, NOW + timedelta(minutes=1))).conversations
    # After the retry window it is claimed again.
    later = NOW + timedelta(minutes=61)
    assert conversation.id in (await _claim(db_session, later)).conversations


async def test_two_claims_in_the_same_instant_claim_once(db_session: AsyncSession) -> None:
    """Review focus 5: two workers polling together."""
    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=_ago(30))

    first = await _claim(db_session)
    second = await _claim(db_session)

    assert conversation.id in first.conversations
    assert conversation.id not in second.conversations


async def test_the_batch_takes_the_oldest_first(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    oldest = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 0))
    older = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 1))
    newest = await _chat(db_session, learner, said=datetime(2000, 1, 1, 0, 2))

    claimed = await sched.claim_due(
        db_session, now=NOW, settings=SETTINGS.model_copy(update={"refresh_batch_size": 2})
    )

    assert claimed.conversations == [oldest.id, older.id]
    assert newest.id not in claimed.conversations


# --- learners --------------------------------------------------------------------------------


async def test_a_learner_with_new_evidence_and_no_profile_is_claimed(
    db_session: AsyncSession,
) -> None:
    """Review focus 4: claiming creates the profile row it stamps."""
    learner = await _learner(db_session)
    db_session.add(
        LearningEvent(
            learner_id=learner.id,
            event_type="observation",
            payload={"score": 1.0},
            created_at=_ago(30),
        )
    )
    await db_session.commit()

    assert learner.id in (await _claim(db_session)).learners
    row = await db_session.scalar(
        select(LearnerProfile).where(LearnerProfile.learner_id == learner.id)
    )
    assert row is not None and row.refresh_attempted_at == NOW


async def test_a_learner_whose_profile_has_read_everything_is_not_due(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=_ago(30))
    db_session.add(LearnerProfile(learner_id=learner.id, evidence_watermark=_ago(30)))
    await db_session.commit()

    assert learner.id not in (await _claim(db_session)).learners


async def test_a_learner_still_answering_is_not_due(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=_ago(5))

    assert learner.id not in (await _claim(db_session)).learners


# --- stuck -----------------------------------------------------------------------------------


async def test_work_due_for_hours_counts_as_stuck(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    await _chat(db_session, learner, said=NOW - timedelta(hours=7))
    fresh = await _learner(db_session)
    await _chat(db_session, fresh, said=_ago(30))
    settings = SETTINGS.model_copy(update={"refresh_stuck_hours": 6})

    # One conversation and one learner have been due for over six hours; the fresh ones not.
    assert await sched.stuck(db_session, now=NOW, settings=settings) == 2
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_refresh_schedule.py -q` → FAIL (`ImportError: refresh_schedule`).

- [ ] **Step 3: Implement** `app/services/refresh_schedule.py`:

```python
"""What is due for memory write-back and profile refresh, and claiming it once (S43).

Both used to run only when something asked: write-back when a client called an endpoint no
screen calls, the profile when the learner pressed Refresh. This decides, from the data alone,
what has gone quiet with evidence nothing has read yet — so a restart or a learner back after
weeks is caught up by the next pass, and there is no separate catch-up job to forget.

"Quiet" is measured from the newest message of any kind, so a conversation mid-reply is never
picked up. Administrator messages never make anything due: they are not the learner's evidence.
A claim is a conditional update of an attempt stamp, so two workers never queue the same item;
the job clears the stamp when it succeeds, and a failure waits ``refresh_retry_minutes``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, or_, select, union_all, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.chat import Conversation, Message
from app.models.learner import Learner
from app.models.learning import LearningEvent
from app.models.profile import LearnerProfile


@dataclass(frozen=True)
class Claimed:
    conversations: list[uuid.UUID]
    learners: list[uuid.UUID]


def _learners_own_message():
    return (
        Message.role == "user",
        Message.admin_actor_id.is_(None),
        Message.admin_action_id.is_(None),
    )


_ACTIVE = (Learner.deletion_requested_at.is_(None), Learner.suspended_at.is_(None))


async def due_conversations(
    session: AsyncSession,
    *,
    quiet_before: datetime,
    retry_before: datetime | None,
    limit: int | None,
) -> list[uuid.UUID]:
    """Conversations with learner messages newer than their watermark, quiet since
    ``quiet_before``, oldest unprocessed first. ``retry_before=None`` ignores claims."""
    newest_own = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .where(*_learners_own_message())
        .group_by(Message.conversation_id)
        .subquery()
    )
    newest_any = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    stmt = (
        select(Conversation.id)
        .join(Learner, Learner.id == Conversation.learner_id)
        .join(newest_own, newest_own.c.conversation_id == Conversation.id)
        .join(newest_any, newest_any.c.conversation_id == Conversation.id)
        .where(
            or_(
                Conversation.memory_watermark.is_(None),
                newest_own.c.at > Conversation.memory_watermark,
            ),
            newest_any.c.at <= quiet_before,
            Conversation.archived_at.is_(None),
            Learner.remember_conversations.is_(True),
            *_ACTIVE,
        )
        .order_by(newest_own.c.at, Conversation.id)
    )
    if retry_before is not None:
        stmt = stmt.where(
            or_(
                Conversation.memory_attempted_at.is_(None),
                Conversation.memory_attempted_at < retry_before,
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.scalars(stmt)).all())


async def due_learners(
    session: AsyncSession,
    *,
    quiet_before: datetime,
    retry_before: datetime | None,
    limit: int | None,
) -> list[uuid.UUID]:
    """Learners whose newest evidence — a graded answer or a message they wrote — is newer
    than their profile's watermark and older than ``quiet_before``, oldest first."""
    evidence = union_all(
        select(LearningEvent.learner_id.label("learner_id"), LearningEvent.created_at.label("at"))
        .where(LearningEvent.event_type == "observation"),
        select(Conversation.learner_id.label("learner_id"), Message.created_at.label("at"))
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(*_learners_own_message()),
    ).subquery()
    newest = (
        select(evidence.c.learner_id, func.max(evidence.c.at).label("at"))
        .group_by(evidence.c.learner_id)
        .subquery()
    )
    stmt = (
        select(Learner.id)
        .join(newest, newest.c.learner_id == Learner.id)
        .outerjoin(LearnerProfile, LearnerProfile.learner_id == Learner.id)
        .where(
            newest.c.at <= quiet_before,
            or_(
                LearnerProfile.evidence_watermark.is_(None),
                newest.c.at > LearnerProfile.evidence_watermark,
            ),
            *_ACTIVE,
        )
        .order_by(newest.c.at, Learner.id)
    )
    if retry_before is not None:
        stmt = stmt.where(
            or_(
                LearnerProfile.refresh_attempted_at.is_(None),
                LearnerProfile.refresh_attempted_at < retry_before,
            )
        )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await session.scalars(stmt)).all())


async def _claim_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, *, now: datetime, retry_before: datetime
) -> bool:
    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.id == conversation_id,
            or_(
                Conversation.memory_attempted_at.is_(None),
                Conversation.memory_attempted_at < retry_before,
            ),
        )
        .values(memory_attempted_at=now)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def _claim_learner(
    session: AsyncSession, learner_id: uuid.UUID, *, now: datetime, retry_before: datetime
) -> bool:
    # A learner with evidence and no profile yet is due; the claim needs a row to stamp.
    await session.execute(
        insert(LearnerProfile)
        .values(id=uuid.uuid4(), learner_id=learner_id)
        .on_conflict_do_nothing(index_elements=["learner_id"])
    )
    result = await session.execute(
        update(LearnerProfile)
        .where(
            LearnerProfile.learner_id == learner_id,
            or_(
                LearnerProfile.refresh_attempted_at.is_(None),
                LearnerProfile.refresh_attempted_at < retry_before,
            ),
        )
        .values(refresh_attempted_at=now)
        .execution_options(synchronize_session=False)
    )
    return cast("CursorResult[Any]", result).rowcount == 1


async def claim_due(session: AsyncSession, *, now: datetime, settings: Settings) -> Claimed:
    """Claim up to ``refresh_batch_size`` conversations and learners that are due, and commit."""
    quiet_before = now - timedelta(minutes=settings.memory_quiet_minutes)
    retry_before = now - timedelta(minutes=settings.refresh_retry_minutes)
    conversations = [
        conversation_id
        for conversation_id in await due_conversations(
            session,
            quiet_before=quiet_before,
            retry_before=retry_before,
            limit=settings.refresh_batch_size,
        )
        if await _claim_conversation(
            session, conversation_id, now=now, retry_before=retry_before
        )
    ]
    learners = [
        learner_id
        for learner_id in await due_learners(
            session,
            quiet_before=quiet_before,
            retry_before=retry_before,
            limit=settings.refresh_batch_size,
        )
        if await _claim_learner(session, learner_id, now=now, retry_before=retry_before)
    ]
    await session.commit()
    return Claimed(conversations=conversations, learners=learners)


async def stuck(session: AsyncSession, *, now: datetime, settings: Settings) -> int:
    """How many conversations and learners have been due for over ``refresh_stuck_hours``."""
    before = now - timedelta(hours=settings.refresh_stuck_hours)
    conversations = await due_conversations(
        session, quiet_before=before, retry_before=None, limit=None
    )
    learners = await due_learners(session, quiet_before=before, retry_before=None, limit=None)
    return len(conversations) + len(learners)
```

(`LearnerProfile`'s id default is Python-side; the explicit `id=uuid.uuid4()` keeps the core insert independent of that. If `ty` rejects `Learner.remember_conversations.is_(True)`, use `Learner.remember_conversations == True  # noqa: E712`.)

- [ ] **Step 4: Run** — `uv run pytest tests/test_refresh_schedule.py -q` → PASS; `uv run poe check && uv run poe format-check` → green. The tests use times in 2000/2026 far from each other; if another suite's committed rows ever leak into this database, `test_the_batch_takes_the_oldest_first` is the one that notices (it asserts exact order) — that is intended.

- [ ] **Step 5: Commit**

```bash
git add app/services/refresh_schedule.py tests/test_refresh_schedule.py
git status
git commit -m "feat(worker): find quiet conversations and learners and claim them once [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The worker loop, the profile task, and the stuck alert [S43]

**Files:**
- Modify: `app/workers/tasks.py`, `app/core/alerts.py`, `app/api/v1/ops.py`, `app/services/refresh_schedule.py` (adds `utcnow()`)
- Test: `tests/test_refresh_schedule.py` (append), `tests/test_ops_signals.py` or wherever `evaluate` is unit-tested (append; `grep -rn "def test_.*erasures_stuck\|stuck_erasures=" tests`)

**Interfaces:**
- Consumes: `refresh_schedule.claim_due`, `refresh_schedule.stuck`, `Claimed` (Task 4); `refresh_poll_interval_seconds` (Task 1).
- Produces: `profile_refresh_task` (taskiq, arg `learner_id: str`); `_refresh_due_once()`; `evaluate(..., refresh_stuck: int = 0)` with alert name `refresh_stuck`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_refresh_schedule.py`:

```python
# --- the worker pass -------------------------------------------------------------------------


async def test_a_pass_queues_what_it_claimed(db_session: AsyncSession, monkeypatch) -> None:
    import contextlib

    from app.workers import tasks

    learner = await _learner(db_session)
    conversation = await _chat(db_session, learner, said=datetime(2000, 1, 1))
    queued: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        async def kiq(self, arg: str) -> None:
            queued.append((self.name, arg))

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(tasks, "memory_write_back_task", _Recorder("memory"))
    monkeypatch.setattr(tasks, "profile_refresh_task", _Recorder("profile"))

    await tasks._refresh_due_once()

    assert ("memory", str(conversation.id)) in queued
    assert ("profile", str(learner.id)) in queued

    queued.clear()
    await tasks._refresh_due_once()  # claimed a moment ago: nothing queued twice
    assert ("memory", str(conversation.id)) not in queued


async def test_the_profile_task_refreshes_an_active_learner_only(
    db_session: AsyncSession, monkeypatch
) -> None:
    import contextlib

    from app.llm.registry import fake_llm_client
    from app.workers import tasks

    active = await _learner(db_session)
    await _chat(db_session, active, said=datetime(2000, 1, 1))
    closing = await _learner(db_session, deletion_requested_at=datetime.now(UTC))
    await _chat(db_session, closing, said=datetime(2000, 1, 1))

    @contextlib.asynccontextmanager
    async def factory():
        yield db_session

    monkeypatch.setattr(tasks, "SessionFactory", factory)
    monkeypatch.setattr(tasks, "build_llm_client", lambda _settings: fake_llm_client("{}"))

    await tasks._profile_refresh_task(str(active.id))
    await tasks._profile_refresh_task(str(closing.id))

    refreshed = await db_session.scalar(
        select(LearnerProfile.refreshed_at).where(LearnerProfile.learner_id == active.id)
    )
    untouched = await db_session.scalar(
        select(LearnerProfile.refreshed_at).where(LearnerProfile.learner_id == closing.id)
    )
    assert refreshed is not None and untouched is None
```

Append beside the existing `erasures_stuck` alert test (same helpers `_ready`, `_backlog`, `_spend` from `tests.test_ops_signals`):

```python
def test_refresh_stuck_fires_only_when_something_is_stuck() -> None:
    from app.core.alerts import evaluate
    from tests.test_ops_signals import _backlog, _ready, _spend

    def names(stuck: int) -> list[str]:
        report = evaluate(
            readiness=_ready(),
            backlog=_backlog(),
            spend=_spend(),
            settings=Settings(),
            refresh_stuck=stuck,
        )
        assert "refresh_stuck" in report.checked
        return [a.name for a in report.firing]

    assert "refresh_stuck" in names(3)
    assert "refresh_stuck" not in names(0)
```

(Put it in `tests/test_refresh_schedule.py` if `test_ops_signals.py`'s helpers are importable from there — they are module-level functions — so the slice's tests stay together.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_refresh_schedule.py -q` → the three new tests FAIL (`_refresh_due_once` / `profile_refresh_task` / `refresh_stuck` missing).

- [ ] **Step 3: Implement.**

`app/core/alerts.py`: `evaluate` gains `refresh_stuck: int = 0`; add `"refresh_stuck"` to `checked`; after the `erasures_stuck` block:

```python
    if refresh_stuck > 0:
        firing.append(
            Alert(
                name="refresh_stuck",
                severity="warning",
                detail=f"{refresh_stuck} conversation(s) or learner(s) due for over "
                f"{settings.refresh_stuck_hours}h",
                action="Memory write-back or profile refresh is not keeping up. Check the "
                "worker is running and GURU_REFRESH_POLL_INTERVAL_SECONDS is not 0, then read "
                "`learner_profiles.last_error` and the `memory.write_back_failed` log lines.",
            )
        )
```

`app/api/v1/ops.py` and `_alerts_once` in `app/workers/tasks.py`: pass
`refresh_stuck=await refresh_schedule.stuck(session, now=_utcnow(), settings=settings)` where `_utcnow()` is `datetime.now(UTC).replace(tzinfo=None)` (define it once in `app/services/refresh_schedule.py` as `def utcnow() -> datetime` and import it in both places).

`app/workers/tasks.py`:

```python
async def _profile_refresh_task(learner_id: str) -> None:
    """Refresh one learner's profile (queued by the refresh sweep, S43)."""
    settings = get_settings()
    llm = build_llm_client(settings)
    async with SessionFactory() as session:
        learner = await session.get(Learner, uuid.UUID(learner_id))
        if learner is None or learner.deletion_requested_at or learner.suspended_at:
            return  # closed or suspended since it was queued: no model call
        await profile_svc.refresh_profile(session, learner.id, llm)


async def _refresh_due_once() -> None:
    """Queue write-back and profile refresh for whatever has gone quiet (S43)."""
    async with SessionFactory() as session:
        claimed = await refresh_schedule.claim_due(
            session, now=refresh_schedule.utcnow(), settings=get_settings()
        )
    for conversation_id in claimed.conversations:
        await memory_write_back_task.kiq(str(conversation_id))
    for learner_id in claimed.learners:
        await profile_refresh_task.kiq(str(learner_id))
    if claimed.conversations or claimed.learners:
        logger.info(
            "queued %d write-back(s) and %d profile refresh(es)",
            len(claimed.conversations),
            len(claimed.learners),
        )


async def _refresh_due_loop(interval: int) -> None:
    """Sweep for quiet work forever, surviving its own failures like the reconciler above."""
    while True:
        await asyncio.sleep(interval)
        try:
            await _refresh_due_once()
        except Exception:
            logger.exception("refresh sweep failed; will retry")


async def _start_refresh_due(state: TaskiqState) -> None:
    interval = get_settings().refresh_poll_interval_seconds
    if interval <= 0:
        return
    state.refresh_due = asyncio.create_task(_refresh_due_loop(interval))


async def _stop_refresh_due(state: TaskiqState) -> None:
    await _cancel(getattr(state, "refresh_due", None))
```

`_memory_write_back_task` wraps its call so a failure is findable without content:

```python
    async with SessionFactory() as session:
        try:
            await memory_svc.write_back(session, llm, conversation_id=uuid.UUID(conversation_id))
        except Exception:
            logger.exception("memory.write_back_failed conversation=%s", conversation_id)
            raise
```

Register: `profile_refresh_task = broker.task(_profile_refresh_task)` beside the others, and the two event handlers beside the rest. Imports: `Learner` from `app.models.learner`, `from app.services import profile as profile_svc`, `from app.services import refresh_schedule`. Keep the module's order: task functions, once-functions, loops, start/stop, registration.

- [ ] **Step 4: Run** — `uv run pytest tests/test_refresh_schedule.py tests/test_alert_history.py tests/test_ops_signals.py tests/test_erasure_retry.py -q` → PASS; `uv run poe check && uv run poe format-check` → green.

- [ ] **Step 5: Commit**

```bash
git add app/workers/tasks.py app/core/alerts.py app/api/v1/ops.py app/services/refresh_schedule.py tests/test_refresh_schedule.py
git status
git commit -m "feat(worker): write back and refresh whatever went quiet, and alert if stuck [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — the memory switch, the paused notice, the profile caption [S43]

**Files:**
- Modify: `frontend/src/api/hooks.ts`, `frontend/src/pages/Account.tsx`, `frontend/src/pages/Account.test.tsx`, `frontend/src/pages/Memory.tsx`, `frontend/src/pages/Memory.test.tsx`, `frontend/src/components/dashboard/ProfileSection.tsx`

**Interfaces:**
- Consumes: `GET/PUT /api/v1/me/memory-setting` (Task 3).
- Produces: `useMemorySetting()`, `useSetMemorySetting()` (mutation taking `boolean`; invalidates `["memory-setting"]`).

- [ ] **Step 1: Failing tests.** In `Account.test.tsx`, extend the hooks mock and add a test:

```tsx
const setMemory = vi.fn();
// inside vi.mock("../api/hooks", () => ({ ... })):
//   useMemorySetting: () => ({ data: { remember: true } }),
//   useSetMemorySetting: () => ({ mutate: setMemory, isPending: false }),

  it("lets the learner pause memory", () => {
    render(<Account />);
    fireEvent.click(screen.getByRole("checkbox", { name: /Remember things from my conversations/ }));
    expect(setMemory).toHaveBeenCalledWith(false);
  });
```

(`vi.mock` factories are hoisted: declare `setMemory` with `vi.hoisted(() => vi.fn())` if referencing it inside the factory fails.)

In `Memory.test.tsx`, wrap `renderPage` in a `MemoryRouter` (import from `react-router-dom`, or whichever router package `App.tsx` imports) and add:

```tsx
describe("while memory is paused", () => {
  it("says nothing new is being learned and links to the switch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((input: Request | string) => {
        const url = typeof input === "string" ? input : input.url;
        if (url.includes("/me/memory-setting")) {
          return Promise.resolve(jsonResponse({ remember: false }));
        }
        return Promise.resolve(jsonResponse([MEMORY]));
      }),
    );
    renderPage();

    expect(await screen.findByText(/Memory is paused/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Account/ })).toHaveAttribute("href", "/account");
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/pages/Account.test.tsx src/pages/Memory.test.tsx` → the two new tests FAIL.

- [ ] **Step 3: Implement.**

`hooks.ts`, beside `useUndoReplacement`:

```ts
/** Whether Guru learns new things from this learner's conversations (S43). */
export function useMemorySetting() {
  return useQuery({
    queryKey: ["memory-setting"],
    queryFn: async () => {
      const { data, error } = await api.GET("/api/v1/me/memory-setting");
      if (error) throw error;
      return data;
    },
  });
}

export function useSetMemorySetting() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (remember: boolean) => {
      const { data, error } = await api.PUT("/api/v1/me/memory-setting", {
        body: { remember },
      });
      if (error) throw error;
      return data;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory-setting"] });
    },
  });
}
```

`Account.tsx`, inside the Preferences section after `<PreferenceControls />`:

```tsx
        <MemorySwitch />
```

with, in the same file:

```tsx
/** Memory is not a teaching setting and has no per-subject override (S43). Pausing stops
 * learning; what is already remembered stays until it is forgotten. */
function MemorySwitch() {
  const { data } = useMemorySetting();
  const set = useSetMemorySetting();
  const remember = data?.remember ?? true;
  return (
    <label className="flex items-start gap-3 pt-2">
      <input
        type="checkbox"
        className="toggle toggle-sm mt-1"
        checked={remember}
        disabled={set.isPending}
        onChange={(e) => set.mutate(e.target.checked)}
      />
      <span className="flex flex-col">
        <span className="text-body">Remember things from my conversations</span>
        <span className="text-caption text-base-content/60">
          When this is off, Guru stops learning new things about you. What it already remembers
          stays until you forget it on the Memory page.
        </span>
      </span>
    </label>
  );
}
```

`Memory.tsx`, in `Memory()` after the `<header>`:

```tsx
      {setting?.remember === false && (
        <p className="alert alert-info text-body">
          Memory is paused — Guru isn&apos;t learning anything new from your conversations. Turn
          it back on in <Link to="/account" className="link">Account</Link>.
        </p>
      )}
```

with `const { data: setting } = useMemorySetting();` and `Link` imported from the router package `App.tsx` uses.

`ProfileSection.tsx`, under the heading row: `<p className="text-caption text-base-content/60">Read from your most recent answers and messages.</p>`.

- [ ] **Step 4: Run** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build && npm run lint` → green (run `npx prettier --write` on the changed files first); `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/hooks.ts frontend/src/pages/Account.tsx frontend/src/pages/Account.test.tsx frontend/src/pages/Memory.tsx frontend/src/pages/Memory.test.tsx frontend/src/components/dashboard/ProfileSection.tsx
git status
git commit -m "feat(web): pause memory, and say so on the Memory page [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Docs [S43]

- [ ] **Step 1: RUNBOOK** — add `## 17. Refresh scheduling (S43)`: what the sweep does (quiet conversations → write-back; quiet learners → profile refresh), the settings with defaults, that "due" is derived from the data so backlogs catch up by themselves, how to force one learner (`POST /api/v1/profile/refresh?force=true` as that learner, or queue `profile_refresh_task`), how to stop it (`GURU_REFRESH_POLL_INTERVAL_SECONDS=0`), what `refresh_stuck` means and the checks in its action, and that a learner can pause memory (`learners.remember_conversations`).
- [ ] **Step 2: OPERATIONS.md** — add the refresh sweep to the "Besides ingestion, the worker runs periodic sweeps" paragraph with a link to RUNBOOK §17; add a `refresh_stuck` row to the alert table in the `erasures_stuck` row's format; replace the admin-visit table's "Read-only" row with the current behaviour: "Audited sudo | A visit can act as the learner: every write is recorded in `admin_actions` with route, status and the visit's reason, is exported to the learner, and requires the originating administrator to still be authorized. Evidence created during a visit is attributed to it and never counts as the learner's own." (check `tests/test_admin_sudo.py` for any detail this sentence gets wrong before writing it).
- [ ] **Step 3: Tracker** — move S43 to "Completed and consolidated work" (sorted by id) as **Implemented**: "Write-back and profile refresh run when a conversation or learner goes quiet (20 minutes), from a state-driven worker sweep that also catches up backlogs; claims stop double work and delay retries after failures; `refresh_stuck` alerts when work is due for hours. The profile reads a recency window (2,000 events, 500 messages) and model-backed estimators run only when their sample changed. Admin-visit messages no longer count as evidence. Learners can pause memory. Remaining for the testing phase: calibrate the quiet period, windows and batch size." Evidence: `app/services/refresh_schedule.py`, `app/services/profile.py`, `app/workers/tasks.py`, `tests/test_refresh_schedule.py`, `tests/test_refresh_cursors.py`. Update S62's note "Incremental profile refresh is owned by S43" to "…was S43 (a recency window; true running aggregates only if S62's measurements call for them)". Add any deferred minors from the final review.
- [ ] **Step 4: CLAUDE.md** — add a bullet after the preferences one: "**Background work runs when things go quiet** (S43) — memory write-back and profile refresh are queued by a worker sweep (`app/services/refresh_schedule.py`) for conversations and learners with unread evidence and no activity for 20 minutes; due-ness is derived from the data, so backlogs catch up by themselves. The profile reads a recency window, and a model-backed estimator pays only when its input changed. Learners can pause memory."
- [ ] **Step 5:** `uv run poe check` → green; commit:

```bash
git add docs/RUNBOOK.md docs/OPERATIONS.md docs/guru-suggestions-tracker.md CLAUDE.md
git status
git commit -m "docs: record refresh scheduling [S43]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
