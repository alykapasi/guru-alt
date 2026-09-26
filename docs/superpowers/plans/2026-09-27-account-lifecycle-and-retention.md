# Account Lifecycle and Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deleting an account takes effect at once but is recoverable for seven days (with erase-now); diagnostic data expires selectively after 30 days; failed erasures are retried until they succeed; a learner can download their uploaded files.

**Architecture:** Two columns on `learners` carry the pending state; `get_current_learner` refuses a pending account, and a second dependency (`AccountHolder`) admits it on a short allow-list. Erasure (`retention.erase_learner`) wraps today's `delete_learner` with the identity provider's `delete_user`, and any refusal becomes a `pending_erasures` row the worker retries. Three new worker loops: erase due accounts, retry erasures, expire diagnostics.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy async, Alembic, taskiq worker loops, pytest; React + TypeScript, TanStack Query, vitest.

**Spec:** `docs/superpowers/specs/2026-09-27-account-lifecycle-and-retention-design.md`

## Global Constraints

- Python 3.13; ruff line-length 100; match surrounding comment density and idiom.
- Every commit green on `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract` (after `uv run poe api-types`, stage `frontend/src/api/schema.d.ts`).
- Frontend changes also green on `cd frontend && npm run build`, `VITE_CLERK_PUBLISHABLE_KEY= npx vitest run`, `npx prettier --check`, `npx eslint` (no new warnings; component files export only components).
- New migration → `uv run python -m tests.testdb`.
- Async tests: no `session.expire_all()`; re-read with `populate_existing=True`; read ids into locals before code that may roll back or delete.
- Timestamp columns: `learners.deletion_*`, `pending_erasures.*` are `DateTime(timezone=True)`; compare with `datetime.now(UTC)`. (`llm_calls`/`turns`/`alert_transitions` `created_at` — check each model's column type and compare with `func.now()` in SQL to avoid naive/aware mixing.)
- One tracker id per commit subject: `[S61]`.
- Every commit message ends with exactly: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Stage only the task's files; `git status` after staging. Never reset, amend, rebase or force-push. Do not push.
- No paid model calls; no real Clerk calls (tests use `FakeIdentityProvider`).

## Review Focus

1. A pending learner's existing session (opened before the request) on another device — revoked, so the next request is 401, not 403 (Task 2 test).
2. An administrator visiting a pending account — gated like the learner; admin routes still work (Task 2 test).
3. The erase worker running while the learner restores in the same second — erase re-reads `deletion_due_at` under a row lock and skips a restored account (Task 2 test).
4. A blob key refused at erase, then re-uploaded by another learner before the retry — the retry leaves the bytes and drops the row (Task 3 test).
5. Diagnostic expiry never touching a row younger than the window, and never a pending turn however old (Task 4 test).

---

### Task 1: Schema, settings and the provider's `delete_user` [S61]

**Files:**
- Create: `db/migrations/versions/0067_account_deletion_and_erasures.py`, `app/models/erasure.py`
- Modify: `app/models/learner.py`, `app/models/__init__.py` (register the model if models are imported there), `app/core/config.py`, `app/core/identity.py` (`IdentityProvider`, `ClerkIdentityProvider`, `FakeIdentityProvider`), `app/schemas/auth.py` (`LearnerRead.deletion_due_at`)
- Test: `tests/test_migrations_with_data.py`, `tests/test_identity_provider.py`, `tests/test_account_deletion.py` (create)

**Interfaces:**
- Produces: `Learner.deletion_requested_at`, `Learner.deletion_due_at: datetime | None`; model `PendingErasure(kind, target, attempts, last_error, next_attempt_at, created_at)`; `Settings.account_recovery_days = 7`, `Settings.diagnostic_retention_days = 30`, `Settings.account_erase_interval_seconds = 300`, `Settings.erasure_retry_interval_seconds = 300`, `Settings.diagnostic_expiry_interval_seconds = 3600`, `Settings.alert_stuck_erasure_attempts = 10`; `IdentityProvider.delete_user(subject) -> None`; `FakeIdentityProvider.deleted: set[str]`; `LearnerRead.deletion_due_at: datetime | None = None`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_migrations_with_data.py`:

```python
async def test_learners_arrive_active_across_the_deletion_migration() -> None:
    """0067 (S61): nobody is pending deletion because a column appeared."""
    async with database_at("0066_archive_and_memory_origin") as connect:
        conn = await connect()
        try:
            learner_id = uuid.uuid4()
            await conn.execute(
                "INSERT INTO learners (id, handle) VALUES ($1, $2)", learner_id, "stayer"
            )
        finally:
            await conn.close()

        await upgrade(SCRATCH, "0067_account_deletion_and_erasures")

        conn = await connect()
        try:
            row = await conn.fetchrow(
                "SELECT deletion_requested_at, deletion_due_at FROM learners WHERE id = $1",
                learner_id,
            )
            assert row is not None
            assert row["deletion_requested_at"] is None and row["deletion_due_at"] is None
            assert await conn.fetchval("SELECT count(*) FROM pending_erasures") == 0
        finally:
            await conn.close()
```

Append to `tests/test_identity_provider.py`:

```python
async def test_the_fake_provider_deletes_users_and_can_refuse() -> None:
    provider = identity.FakeIdentityProvider()
    user = provider.add_user(emails=["a@example.com"])

    provider.fail_next = True
    with pytest.raises(identity.ProviderError):
        await provider.delete_user(user.subject)
    await provider.delete_user(user.subject)
    await provider.delete_user(user.subject)  # already gone is success

    assert user.subject in provider.deleted and user.subject not in provider.users
```

(Match that file's import names — it refers to the module as `identity`; adjust if it differs.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_identity_provider.py -q -k delete` → FAIL (`AttributeError: ... 'delete_user'`).

- [ ] **Step 3: Implement.**

Migration `0067_account_deletion_and_erasures.py`:

```python
"""Account deletion with a recovery window, and erasures that are retried (S61, V12).

``deletion_requested_at``/``deletion_due_at`` make deletion a state rather than an event: access
ends at once, the data stays for the recovery window, then a worker erases. ``pending_erasures``
holds what could not be erased when asked — object-store keys and identity-provider users —
naming no learner, so it survives the erase it belongs to and is retried until it succeeds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067_account_deletion_and_erasures"
down_revision: str | Sequence[str] | None = "0066_archive_and_memory_origin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learners", sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "learners", sa.Column("deletion_due_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_learners_deletion_due_at", "learners", ["deletion_due_at"])
    op.create_table(
        "pending_erasures",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("kind", "target", name="uq_pending_erasures_kind_target"),
    )
    op.create_index("ix_pending_erasures_next_attempt_at", "pending_erasures", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_erasures_next_attempt_at", table_name="pending_erasures")
    op.drop_table("pending_erasures")
    op.drop_index("ix_learners_deletion_due_at", table_name="learners")
    op.drop_column("learners", "deletion_due_at")
    op.drop_column("learners", "deletion_requested_at")
```

`app/models/erasure.py`:

```python
"""What could not be erased when asked, retried until it is (S61, V12).

Names no learner — a blob key or an identity-provider subject — so it survives the account
erase it belongs to, and is deleted once the erasure succeeds.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.base import UUIDPrimaryKeyMixin


class ErasureKind(StrEnum):
    BLOB = "blob"
    IDENTITY = "identity"


class PendingErasure(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pending_erasures"
    __table_args__ = (UniqueConstraint("kind", "target", name="uq_pending_erasures_kind_target"),)

    kind: Mapped[str] = mapped_column()
    target: Mapped[str] = mapped_column()
    attempts: Mapped[int] = mapped_column(server_default="0", default=0)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

(Import `UUIDPrimaryKeyMixin` from wherever the other models do — check `app/models/ops.py`'s imports — and register the module wherever `app/models/__init__.py` or Alembic's `env.py` collects models so metadata includes it.)

`Learner` (after `suspended_at`):

```python
    # Pending deletion (S61, V12): access ended at the request, the data stays until the due
    # time for recovery, then the erase worker removes the account. Both NULL means active.
    deletion_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    deletion_due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
```

`Settings` (beside `checkpoint_retention_days`):

```python
    # V12. How long a deleted account can still be restored by signing in, and how long
    # diagnostic rows (model-call accounting, finished turns, alert history) keep anything that
    # points at a learner. 0 disables a sweep's interval, as elsewhere.
    account_recovery_days: int = 7
    diagnostic_retention_days: int = 30
    account_erase_interval_seconds: int = 300
    erasure_retry_interval_seconds: int = 300
    diagnostic_expiry_interval_seconds: int = 3600
    alert_stuck_erasure_attempts: int = 10
```

`IdentityProvider` protocol: `async def delete_user(self, subject: str) -> None: ...`.

`ClerkIdentityProvider`:

```python
    async def delete_user(self, subject: str) -> None:
        """Remove the provider's copy of this person (S61). Already gone counts as done."""
        try:
            await self._call("users.delete_async", user_id=subject)
        except ProviderError as exc:
            if getattr(exc.__cause__, "status_code", None) == 404:
                return
            raise
```

`FakeIdentityProvider`: `self.deleted: set[str] = set()` in `__init__`, and

```python
    async def delete_user(self, subject: str) -> None:
        self._maybe_fail()
        self.users.pop(subject, None)
        self.deleted.add(subject)
```

`LearnerRead`: `deletion_due_at: datetime | None = None` (import datetime), with a comment: "Set while the account is pending deletion (S61): the app shows the recovery screen instead of the shell."

Create `tests/test_account_deletion.py` with the module docstring `"""Account deletion: pending, recoverable, then erased (S61, V12)."""` and nothing else yet (Task 2 fills it).

- [ ] **Step 4: Run tests and the gate** — `uv run python -m tests.testdb`; `uv run pytest tests/test_migrations_with_data.py tests/test_identity_provider.py -q` → PASS. `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/versions/0067_account_deletion_and_erasures.py app/models/erasure.py app/models/learner.py app/models/__init__.py app/core/config.py app/core/identity.py app/schemas/auth.py frontend/src/api/schema.d.ts tests/test_migrations_with_data.py tests/test_identity_provider.py tests/test_account_deletion.py
git status
git commit -m "feat(accounts): deletion state, pending erasures and provider user deletion [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

(Stage only the files you actually changed; `app/models/__init__.py` only if you edited it.)

---

### Task 2: Pending deletion, the gate, restore, erase-now and the erase worker [S61]

**Files:**
- Modify: `app/services/retention.py` (`request_deletion`, `restore_account`, `erase_learner`, `erase_due`), `app/api/deps.py` (`get_current_learner` gate; `get_account_holder` / `AccountHolder`), `app/api/v1/retention.py` (`DELETE /me`, `/me/deletion*`, exports switch to `AccountHolder`), `app/api/v1/auth.py` (`/me`, `/logout-all` switch to `AccountHolder`), `app/schemas/retention.py` (`DeletionStatusRead`, `DeletionRequestRead`), `app/workers/tasks.py` (erase loop)
- Test: `tests/test_account_deletion.py`

**Interfaces:**
- Consumes: Task 1 columns, settings, `delete_user`.
- Produces:
  ```python
  class NotPending(Exception)  # in retention
  async def request_deletion(session, learner_id, *, settings) -> Learner   # idempotent
  async def restore_account(session, learner_id) -> Learner                  # NotPending if active
  async def erase_learner(session, blobstore, provider, learner_id) -> DeletionReport
  async def erase_due(session, blobstore, provider, *, now: datetime) -> int
  AccountHolder = Annotated[Learner, Depends(get_account_holder)]
  ```

- [ ] **Step 1: Failing tests** — fill `tests/test_account_deletion.py`:

```python
"""Account deletion: pending, recoverable, then erased (S61, V12)."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.identity import FakeIdentityProvider
from app.models.auth import LearnerSession
from app.models.learner import Learner
from app.services import retention
from app.storage import InMemoryBlobStore

API = "/api/v1"


async def test_requesting_deletion_signs_out_everywhere_and_gates_the_account(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    learner_id = api_learner.id

    r = await api_client.delete(f"{API}/me")

    assert r.status_code == 202
    due = datetime.fromisoformat(r.json()["due_at"])
    assert timedelta(days=6, hours=23) < due - datetime.now(UTC) <= timedelta(days=7)
    live = await db_session.scalars(
        select(LearnerSession).where(
            LearnerSession.learner_id == learner_id, LearnerSession.revoked_at.is_(None)
        )
    )
    assert list(live) == []
    assert (await api_client.get(f"{API}/memory")).status_code == 401, "the session is gone"


async def test_a_new_session_on_a_pending_account_is_gated(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    from tests.conftest import sign_in

    await retention.request_deletion(db_session, api_learner.id, settings=get_settings())
    await sign_in(api_client, db_session, api_learner)

    blocked = await api_client.get(f"{API}/memory")
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "deletion_pending"
    me = await api_client.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["deletion_due_at"] is not None
    status = (await api_client.get(f"{API}/me/deletion")).json()
    assert status["pending"] is True
    assert (await api_client.get(f"{API}/me/export")).status_code == 200


async def test_restore_reopens_the_account(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    from tests.conftest import sign_in

    await retention.request_deletion(db_session, api_learner.id, settings=get_settings())
    await sign_in(api_client, db_session, api_learner)

    r = await api_client.post(f"{API}/me/deletion/restore")

    assert r.status_code == 200
    assert (await api_client.get(f"{API}/memory")).status_code == 200
    again = await api_client.post(f"{API}/me/deletion/restore")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "not_pending"


async def test_repeating_the_request_keeps_the_due_date(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    settings = get_settings()
    first = (await retention.request_deletion(db_session, api_learner.id, settings=settings)).deletion_due_at
    second = (await retention.request_deletion(db_session, api_learner.id, settings=settings)).deletion_due_at
    assert first == second


async def test_erase_now_removes_the_learner_and_the_provider_user(
    db_session: AsyncSession,
) -> None:
    provider = FakeIdentityProvider()
    user = provider.add_user(emails=["gone@example.com"])
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", auth_subject=user.subject)
    db_session.add(learner)
    await db_session.commit()
    learner_id = learner.id
    await retention.request_deletion(db_session, learner_id, settings=get_settings())

    await retention.erase_learner(db_session, InMemoryBlobStore(), provider, learner_id)

    assert await db_session.get(Learner, learner_id) is None
    assert user.subject in provider.deleted


async def test_a_refused_provider_delete_is_queued_not_fatal(db_session: AsyncSession) -> None:
    from app.models.erasure import PendingErasure

    provider = FakeIdentityProvider()
    user = provider.add_user(emails=["gone@example.com"])
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", auth_subject=user.subject)
    db_session.add(learner)
    await db_session.commit()
    learner_id = learner.id
    provider.fail_next = True

    await retention.erase_learner(db_session, InMemoryBlobStore(), provider, learner_id)

    assert await db_session.get(Learner, learner_id) is None
    queued = await db_session.scalar(
        select(PendingErasure).where(
            PendingErasure.kind == "identity", PendingErasure.target == user.subject
        )
    )
    assert queued is not None


async def test_the_worker_erases_only_past_due_accounts(db_session: AsyncSession) -> None:
    settings = get_settings()
    due, not_yet = Learner(handle=f"d-{uuid.uuid4().hex[:8]}"), Learner(handle=f"n-{uuid.uuid4().hex[:8]}")
    db_session.add_all([due, not_yet])
    await db_session.commit()
    due_id, not_yet_id = due.id, not_yet.id
    await retention.request_deletion(db_session, due_id, settings=settings)
    await retention.request_deletion(db_session, not_yet_id, settings=settings)

    erased = await retention.erase_due(
        db_session,
        InMemoryBlobStore(),
        FakeIdentityProvider(),
        now=datetime.now(UTC) + timedelta(days=7, minutes=1),
    )
    assert erased == 2
    # and nothing when nothing is due
    assert await retention.erase_due(
        db_session, InMemoryBlobStore(), FakeIdentityProvider(), now=datetime.now(UTC)
    ) == 0


async def test_a_restored_account_is_not_erased_by_a_stale_sweep(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"r-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.commit()
    learner_id = learner.id
    await retention.request_deletion(db_session, learner_id, settings=get_settings())
    await retention.restore_account(db_session, learner_id)

    erased = await retention.erase_due(
        db_session,
        InMemoryBlobStore(),
        FakeIdentityProvider(),
        now=datetime.now(UTC) + timedelta(days=30),
    )

    assert erased == 0 and await db_session.get(Learner, learner_id) is not None


async def test_an_administrator_visiting_a_pending_account_is_gated_too(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    """The gate reads the effective learner, so a visit sees what the learner would."""
    from fastapi import HTTPException

    from app.api.deps import get_current_learner
    from app.services.auth import Authenticated

    await retention.request_deletion(db_session, api_learner.id, settings=get_settings())
    pending = await db_session.get(Learner, api_learner.id, populate_existing=True)
    assert pending is not None
    try:
        await get_current_learner(
            None,  # type: ignore[arg-type]
            Authenticated(learner=pending, impersonated_by_id=uuid.uuid4(), session_id=uuid.uuid4()),
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a pending account was admitted")
```

(`Authenticated`'s constructor fields: read `app/services/auth.py`; if `get_current_learner` does not accept `None` for the request, build a minimal `Request({"type": "http"})` from starlette.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_account_deletion.py -q` → FAIL (`AttributeError: ... 'request_deletion'`, 405 on `DELETE /me` status expectation, etc.).

- [ ] **Step 3: Implement.**

`app/services/retention.py` additions (imports: `from datetime import UTC, datetime, timedelta`; `from app.core.config import Settings`; `from app.core.identity import IdentityProvider`; `from app.services import auth as auth_svc`; `from app.models.erasure import ErasureKind, PendingErasure`; `from sqlalchemy.dialects.postgresql import insert as pg_insert`):

```python
class NotPending(Exception):
    """Restore or erase-now asked of an account that is not pending deletion."""


async def request_deletion(
    session: AsyncSession, learner_id: uuid.UUID, *, settings: Settings
) -> Learner:
    """Start the recovery window (V12): access ends now, the data stays until the due time.

    Every session is revoked, so every device is signed out at once. Idempotent: asking again
    while pending neither moves the due date nor extends the window.
    """
    learner = await session.get(Learner, learner_id, populate_existing=True, with_for_update=True)
    if learner is None:
        raise LookupError(str(learner_id))
    if learner.deletion_due_at is None:
        now = datetime.now(UTC)
        learner.deletion_requested_at = now
        learner.deletion_due_at = now + timedelta(days=settings.account_recovery_days)
    await session.commit()  # before revoking: revoke_all commits its own transaction
    await auth_svc.revoke_all(session, learner_id)
    await session.refresh(learner)
    return learner


async def restore_account(session: AsyncSession, learner_id: uuid.UUID) -> Learner:
    learner = await session.get(Learner, learner_id, populate_existing=True, with_for_update=True)
    if learner is None:
        raise LookupError(str(learner_id))
    if learner.deletion_due_at is None:
        raise NotPending(str(learner_id))
    learner.deletion_requested_at = None
    learner.deletion_due_at = None
    await session.commit()
    await session.refresh(learner)
    return learner


async def queue_erasure(session: AsyncSession, kind: ErasureKind, target: str, error: str) -> None:
    """Record something that could not be erased, for the retry worker. Commits."""
    await session.execute(
        pg_insert(PendingErasure)
        .values(id=uuid.uuid4(), kind=kind, target=target, last_error=error[:500])
        .on_conflict_do_nothing(constraint="uq_pending_erasures_kind_target")
    )
    await session.commit()


async def erase_learner(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    learner_id: uuid.UUID,
) -> DeletionReport:
    """Erase the account now: every store per :data:`RETENTION`, then the provider's copy.

    A refusal by the object store or the provider never undoes the database erase; it becomes a
    pending erasure the worker retries until it succeeds.
    """
    learner = await session.get(Learner, learner_id)
    subject = learner.auth_subject if learner is not None else None
    report = await delete_learner(session, blobstore, learner_id)
    for key in report.blobs_failed:
        await queue_erasure(session, ErasureKind.BLOB, key, "refused at account erase")
    if subject:
        if provider is None:
            await queue_erasure(session, ErasureKind.IDENTITY, subject, "no provider configured")
        else:
            try:
                await provider.delete_user(subject)
            except Exception as exc:
                log.warning("retention.identity_not_deleted", learner_id=str(learner_id))
                await queue_erasure(session, ErasureKind.IDENTITY, subject, str(exc))
    return report


async def erase_due(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    *,
    now: datetime,
) -> int:
    """Erase every account whose recovery window has passed. Returns how many.

    Each candidate is re-read under a row lock: a learner who restored between the query and
    the erase is no longer due and is skipped.
    """
    ids = list(
        (
            await session.scalars(
                select(Learner.id).where(
                    Learner.deletion_due_at.is_not(None), Learner.deletion_due_at <= now
                )
            )
        ).all()
    )
    erased = 0
    for learner_id in ids:
        learner = await session.get(
            Learner, learner_id, populate_existing=True, with_for_update=True
        )
        if learner is None or learner.deletion_due_at is None or learner.deletion_due_at > now:
            await session.rollback()
            continue
        await erase_learner(session, blobstore, provider, learner_id)
        erased += 1
    return erased
```

(`delete_learner` already commits; the row lock taken just before is released by that commit.)

`app/api/deps.py`:

```python
async def get_account_holder(who: Authenticated) -> Learner:
    """The learner, admitted even while their account is pending deletion (S61).

    For the few routes a pending account must still reach: its status, restore, erase-now,
    export, who-am-I and sign-out-everywhere. Everything else goes through
    ``get_current_learner``, which refuses a pending account.
    """
    return who.learner


AccountHolder = Annotated[Learner, Depends(get_account_holder)]
```

and `get_current_learner` becomes:

```python
async def get_current_learner(request: Request, who: Authenticated) -> Learner:
    """The effective learner; authenticated sudo requests already have durable audit intent.

    A pending-deletion account is refused (S61): its sessions were revoked at the request, and
    a new one only reaches the recovery routes (``AccountHolder``).
    """
    if who.learner.deletion_due_at is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            {
                "code": "deletion_pending",
                "due_at": who.learner.deletion_due_at.isoformat(),
                "message": "This account is scheduled for deletion.",
            },
        )
    return who.learner
```

`app/schemas/retention.py`:

```python
class DeletionRequestRead(BaseModel):
    due_at: datetime


class DeletionStatusRead(BaseModel):
    pending: bool
    requested_at: datetime | None
    due_at: datetime | None
```

`app/api/v1/retention.py` (imports `AccountHolder`, `IdentityProviderDep`, `SettingsDep`, the schemas):

```python
@router.delete("/me", response_model=DeletionRequestRead, status_code=status.HTTP_202_ACCEPTED)
async def request_delete_me(session: SessionDep, learner: AccountHolder, settings: SettingsDep):
    """Delete this account (V12): access ends now; it can be restored for the recovery window
    by signing in again, then it is erased. ``POST /me/deletion/erase`` erases at once."""
    pending = await svc.request_deletion(session, learner.id, settings=settings)
    assert pending.deletion_due_at is not None
    return DeletionRequestRead(due_at=pending.deletion_due_at)


@router.get("/me/deletion", response_model=DeletionStatusRead)
async def deletion_status(learner: AccountHolder):
    return DeletionStatusRead(
        pending=learner.deletion_due_at is not None,
        requested_at=learner.deletion_requested_at,
        due_at=learner.deletion_due_at,
    )


def _not_pending() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {"code": "not_pending", "message": "This account is not scheduled for deletion."},
    )


@router.post("/me/deletion/restore", response_model=DeletionStatusRead)
async def restore_me(session: SessionDep, learner: AccountHolder):
    try:
        restored = await svc.restore_account(session, learner.id)
    except svc.NotPending as exc:
        raise _not_pending() from exc
    return DeletionStatusRead(pending=False, requested_at=None, due_at=None)


@router.post("/me/deletion/erase", response_model=DeletionReportRead)
async def erase_me(
    session: SessionDep,
    learner: AccountHolder,
    blobstore: BlobStoreDep,
    provider: IdentityProviderDep,
):
    """Erase now, without waiting out the recovery window. Only for a pending account: an
    active one asks for deletion first, so there is one way in to erasing."""
    if learner.deletion_due_at is None:
        raise _not_pending()
    report = await svc.erase_learner(session, blobstore, provider, learner.id)
    return DeletionReportRead(
        learner_id=report.learner_id,
        blobs_deleted=report.blobs_deleted,
        blobs_retained=report.blobs_retained,
        blobs_failed=len(report.blobs_failed),
        items_deleted=report.items_deleted,
        complete=report.complete,
    )
```

(drop the unused `restored` binding if ruff flags it.) `export_me` switches its dependency to `AccountHolder`.

`app/api/v1/auth.py`: `me` and `logout_all` take `learner: AccountHolder` instead of `CurrentLearner` (a pending account must still learn it is pending, and sign out everywhere).

`app/workers/tasks.py` (import `from datetime import UTC, datetime, timedelta`, `from app.core.identity import build_identity_provider`, `from app.services import retention as retention_svc`):

```python
async def _erase_due_once() -> None:
    """Erase every account whose recovery window has passed (S61, V12)."""
    settings = get_settings()
    async with SessionFactory() as session:
        erased = await retention_svc.erase_due(
            session,
            build_blob_store(settings),
            build_identity_provider(settings),
            now=datetime.now(UTC),
        )
    if erased:
        logger.info("erased %d account(s) past their recovery window", erased)
```

plus `_erase_due_loop(interval)`, `_start_account_erase`/`_stop_account_erase` and two `broker.add_event_handler` lines, mirroring `_purge_sessions_*` exactly and reading `account_erase_interval_seconds`.

- [ ] **Step 4: Run tests and the gate** — `uv run pytest tests/test_account_deletion.py tests/test_auth.py tests/test_retention.py tests/test_suspension.py tests/test_impersonation.py -q` → PASS. `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/retention.py app/api/deps.py app/api/v1/retention.py app/api/v1/auth.py app/schemas/retention.py app/workers/tasks.py frontend/src/api/schema.d.ts tests/test_account_deletion.py
git status
git commit -m "feat(accounts): deleting an account is recoverable for seven days, then erased [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Retryable erasure [S61]

**Files:**
- Modify: `app/services/retention.py` (`retry_erasures`), `app/services/removal.py` (`delete_source` queues a failed blob), `app/core/alerts.py` (`erasures_stuck`), `app/api/v1/ops.py` and `app/workers/tasks.py` (pass the count; retry loop)
- Test: `tests/test_erasure_retry.py` (create), `tests/test_ops_signals.py` (if it asserts the exact `checked` list)

**Interfaces:**
- Consumes: `queue_erasure`, `PendingErasure`, `ErasureKind` (Task 2/1).
- Produces: `async def retry_erasures(session, blobstore, provider, *, now: datetime) -> int` (rows resolved); `async def stuck_erasures(session, *, attempts: int) -> int`; `evaluate(..., stuck_erasures: int = 0)`.

- [ ] **Step 1: Failing tests** — `tests/test_erasure_retry.py`:

```python
"""What could not be erased is retried until it is (S61, V12)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.identity import FakeIdentityProvider
from app.models.erasure import ErasureKind, PendingErasure
from app.services import retention
from app.storage import InMemoryBlobStore


class _Refusing(InMemoryBlobStore):
    def __init__(self) -> None:
        super().__init__()
        self.refuse = True

    async def delete(self, key: str) -> None:
        if self.refuse:
            raise RuntimeError("store unavailable")
        await super().delete(key)


async def _row(session: AsyncSession, kind: str, target: str) -> PendingErasure | None:
    return await session.scalar(
        select(PendingErasure)
        .where(PendingErasure.kind == kind, PendingErasure.target == target)
        .execution_options(populate_existing=True)
    )


async def test_a_refused_blob_is_retried_with_backoff_then_deleted(db_session: AsyncSession) -> None:
    store = _Refusing()
    key = f"blobs/{uuid.uuid4().hex}"
    await store.put(key, b"bytes")
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    now = datetime.now(UTC) + timedelta(seconds=1)

    assert await retention.retry_erasures(db_session, store, None, now=now) == 0
    first = await _row(db_session, "blob", key)
    assert first is not None and first.attempts == 1 and first.next_attempt_at > now
    later = first.next_attempt_at + timedelta(seconds=1)
    store.refuse = False

    assert await retention.retry_erasures(db_session, store, None, now=later) == 1
    assert await _row(db_session, "blob", key) is None
    assert not await store.exists(key)


async def test_a_key_someone_uploads_again_is_left_alone(db_session: AsyncSession) -> None:
    from app.models.learner import Learner
    from app.models.source import Source, SourceKind, SourceStatus

    store = InMemoryBlobStore()
    key = f"blobs/{uuid.uuid4().hex}"
    await store.put(key, b"bytes")
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    other = Learner(handle=f"o-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Source(
            learner_id=other.id,
            kind=SourceKind.FILE,
            origin="x.txt",
            status=SourceStatus.DONE,
            blob_key=key,
            meta={},
            attempts=0,
        )
    )
    await db_session.commit()

    await retention.retry_erasures(
        db_session, store, None, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert await store.exists(key)
    assert await _row(db_session, "blob", key) is None


async def test_an_identity_is_retried_and_already_gone_counts_as_done(
    db_session: AsyncSession,
) -> None:
    provider = FakeIdentityProvider()
    user = provider.add_user(emails=["x@example.com"])
    await retention.queue_erasure(db_session, ErasureKind.IDENTITY, user.subject, "refused")

    resolved = await retention.retry_erasures(
        db_session, InMemoryBlobStore(), provider, now=datetime.now(UTC) + timedelta(seconds=1)
    )

    assert resolved == 1 and user.subject in provider.deleted


async def test_stuck_erasures_raise_an_alert(db_session: AsyncSession) -> None:
    from app.core.alerts import evaluate
    from tests.test_ops_signals import _backlog, _ready, _spend

    key = f"blobs/{uuid.uuid4().hex}"
    await retention.queue_erasure(db_session, ErasureKind.BLOB, key, "refused")
    row = await _row(db_session, "blob", key)
    assert row is not None
    row.attempts = 10
    await db_session.commit()

    stuck = await retention.stuck_erasures(db_session, attempts=10)
    report = evaluate(
        readiness=_ready(), backlog=_backlog(), spend=_spend(), settings=Settings(), stuck_erasures=stuck
    )

    assert stuck >= 1
    assert "erasures_stuck" in [a.name for a in report.firing]
```

(If `tests/test_ops_signals.py`'s helpers have different names or need arguments, read them and adapt; if importing across test modules is awkward, construct the three reports inline the same way that file does.)

Also add to `tests/test_removal.py` (slice A's file):

```python
async def test_a_file_the_store_refuses_on_delete_is_queued_for_retry(
    db_session: AsyncSession,
) -> None:
    from app.models.erasure import PendingErasure

    class _Refusing(InMemoryBlobStore):
        async def delete(self, key: str) -> None:
            raise RuntimeError("store unavailable")

    learner = await _learner(db_session)
    store = _Refusing()
    source_id = await _source(db_session, learner, store=store)
    key = (await _get(db_session, source_id)).blob_key
    await db_session.commit()

    await removal.delete_source(db_session, store, learner.id, source_id, forget=False)

    queued = await db_session.scalar(select(PendingErasure).where(PendingErasure.target == key))
    assert queued is not None and queued.kind == "blob"
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_erasure_retry.py tests/test_removal.py -q -k "erasure or refuses or retried or stuck or uploads_again or identity"` → FAIL.

- [ ] **Step 3: Implement** in `retention.py`:

```python
_MAX_BACKOFF = timedelta(days=1)


def _backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2**attempts), _MAX_BACKOFF)


async def retry_erasures(
    session: AsyncSession,
    blobstore: BlobStore,
    provider: IdentityProvider | None,
    *,
    now: datetime,
) -> int:
    """Try every due pending erasure once. Returns how many were resolved (row removed).

    A blob is deleted only while nothing references its key again — bytes are
    content-addressed, and a re-upload since the refusal makes them somebody's file; either way
    the erasure is no longer owed. An identity already gone at the provider counts as done.
    Failures back off exponentially to a day and keep retrying; ``stuck_erasures`` is what
    makes a persistent one visible.
    """
    rows = list(
        (
            await session.scalars(
                select(PendingErasure)
                .where(PendingErasure.next_attempt_at <= now)
                .order_by(PendingErasure.next_attempt_at)
            )
        ).all()
    )
    resolved = 0
    for row in rows:
        try:
            if row.kind == ErasureKind.BLOB:
                await ingestion.unreference_blob(session, blobstore, row.target)
            elif provider is None:
                raise RuntimeError("no identity provider configured")
            else:
                await provider.delete_user(row.target)
        except Exception as exc:
            row.attempts += 1
            row.last_error = str(exc)[:500]
            row.next_attempt_at = now + _backoff(row.attempts)
            await session.commit()
            continue
        await session.delete(row)
        await session.commit()
        resolved += 1
    return resolved


async def stuck_erasures(session: AsyncSession, *, attempts: int) -> int:
    return (
        await session.scalar(
            select(func.count()).select_from(PendingErasure).where(PendingErasure.attempts >= attempts)
        )
    ) or 0
```

(`unreference_blob` returns False for a referenced key without deleting — that path resolves the row too. Import `func`.)

In `delete_learner`, nothing changes (the caller `erase_learner` queues `report.blobs_failed`). In `removal.delete_source`, inside the `except` that logs `removal.blob_not_deleted`, add `await retention.queue_erasure(session, ErasureKind.BLOB, blob_key, "refused at source delete")` (import `retention` inside the function if a cycle appears: retention imports ingestion, removal imports ingestion — check), and change the note to "The stored file could not be removed yet; it will be retried."

`app/core/alerts.py`: `evaluate(..., stuck_erasures: int = 0)`, add `"erasures_stuck"` to `checked`, and:

```python
    if stuck_erasures:
        firing.append(
            Alert(
                name="erasures_stuck",
                severity="warning",
                detail=f"{stuck_erasures} erasure(s) refused {settings.alert_stuck_erasure_attempts}+ times",
                action="Object storage or the identity provider keeps refusing deletes. Read "
                "`pending_erasures.last_error`; the worker keeps retrying daily.",
            )
        )
```

`app/api/v1/ops.py` and `_alerts_once` pass `stuck_erasures=await retention.stuck_erasures(session, attempts=settings.alert_stuck_erasure_attempts)`.

Worker: `_retry_erasures_once` (builds the blob store and provider as `_erase_due_once` does; logs resolved count) with `_retry_erasures_loop`, start/stop handlers reading `erasure_retry_interval_seconds`, registered like the others.

- [ ] **Step 4: Run tests and the gate** — `uv run pytest tests/test_erasure_retry.py tests/test_removal.py tests/test_ops_signals.py tests/test_retention.py -q` → PASS (update any `checked`-list assertion in `test_ops_signals.py` to include `"erasures_stuck"`). `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/retention.py app/services/removal.py app/core/alerts.py app/api/v1/ops.py app/workers/tasks.py tests/test_erasure_retry.py tests/test_removal.py tests/test_ops_signals.py
git status
git commit -m "feat(retention): erasures the store or provider refused are retried until done [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Diagnostic expiry [S61]

**Files:**
- Modify: `app/services/retention.py` (`expire_diagnostics`, `RETENTION` window notes), `app/workers/tasks.py` (loop)
- Test: `tests/test_diagnostic_expiry.py` (create)

**Interfaces:**
- Produces: `@dataclass class ExpiryReport(calls_anonymised: int, decisions_anonymised: int, turns_deleted: int, alerts_deleted: int)`; `async def expire_diagnostics(session, *, older_than: timedelta) -> ExpiryReport`.

- [ ] **Step 1: Failing tests** — `tests/test_diagnostic_expiry.py`. Build rows directly and back-date `created_at` with an UPDATE (`update(Model).where(Model.id == x).values(created_at=func.now() - text("interval '40 days'"))`), then:

```python
"""Diagnostic data keeps nothing pointing at a learner past its window (S61, V12)."""

import uuid
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat import Conversation, LLMCall, Turn, TurnStatus
from app.models.learner import Learner
from app.models.ops import AlertTransition
from app.services import retention

WINDOW = timedelta(days=30)


async def _age(session: AsyncSession, model, row_id, days: int) -> None:
    await session.execute(
        update(model)
        .where(model.id == row_id)
        .values(created_at=func.now() - text(f"interval '{days} days'"))
    )


async def test_old_accounting_loses_its_learner_and_new_keeps_it(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    old = LLMCall(learner_id=learner.id, role="fast", provider="fake", model="fake-1")
    new = LLMCall(learner_id=learner.id, role="fast", provider="fake", model="fake-1")
    db_session.add_all([old, new])
    await db_session.flush()
    await _age(db_session, LLMCall, old.id, 40)
    await db_session.commit()
    old_id, new_id = old.id, new.id

    report = await retention.expire_diagnostics(db_session, older_than=WINDOW)

    assert report.calls_anonymised >= 1
    old_row = await db_session.get(LLMCall, old_id, populate_existing=True)
    new_row = await db_session.get(LLMCall, new_id, populate_existing=True)
    assert old_row is not None and old_row.learner_id is None
    assert new_row is not None and new_row.learner_id == learner.id


async def test_old_finished_turns_go_and_pending_ones_stay(db_session: AsyncSession) -> None:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}")
    db_session.add(learner)
    await db_session.flush()
    conversation = Conversation(learner_id=learner.id)
    db_session.add(conversation)
    await db_session.flush()
    done = Turn(conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.COMPLETED)
    pending = Turn(conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.PENDING)
    recent = Turn(conversation_id=conversation.id, flow="tutor", content="hi", status=TurnStatus.FAILED)
    db_session.add_all([done, pending, recent])
    await db_session.flush()
    for turn in (done, pending):
        await _age(db_session, Turn, turn.id, 40)
    await db_session.commit()
    done_id, pending_id, recent_id = done.id, pending.id, recent.id

    await retention.expire_diagnostics(db_session, older_than=WINDOW)

    assert await db_session.get(Turn, done_id, populate_existing=True) is None
    assert await db_session.get(Turn, pending_id, populate_existing=True) is not None
    assert await db_session.get(Turn, recent_id, populate_existing=True) is not None


async def test_the_newest_row_per_alert_survives(db_session: AsyncSession) -> None:
    name = f"a-{uuid.uuid4().hex[:6]}"
    rows = [
        AlertTransition(name=name, firing=f, severity="warning", detail="d", action="a")
        for f in (True, False)
    ]
    db_session.add_all(rows)
    await db_session.flush()
    for row in rows:
        await _age(db_session, AlertTransition, row.id, 40)
    await db_session.commit()

    await retention.expire_diagnostics(db_session, older_than=WINDOW)

    left = list(
        await db_session.scalars(select(AlertTransition).where(AlertTransition.name == name))
    )
    assert len(left) == 1 and left[0].firing is False
```

Add a `decision_calls` case mirroring the `llm_calls` one (construct `DecisionCall` with the model's required fields — read `app/models/decision.py`). Read `LLMCall`, `Turn`, `AlertTransition` for required columns and supply them.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_diagnostic_expiry.py -q` → FAIL (`AttributeError: ... 'expire_diagnostics'`).

- [ ] **Step 3: Implement** in `retention.py` (imports `DecisionCall`, `LLMCall`, `AlertTransition`, `TurnStatus`):

```python
@dataclass
class ExpiryReport:
    calls_anonymised: int = 0
    decisions_anonymised: int = 0
    turns_deleted: int = 0
    alerts_deleted: int = 0


async def expire_diagnostics(session: AsyncSession, *, older_than: timedelta) -> ExpiryReport:
    """Apply the diagnostic window (V12) — selectively.

    Accounting rows are anonymised, not deleted: spend totals and Jev's evidence must still
    add up, and without a learner or conversation they point at nobody. Finished turns go —
    their messages are the durable copy. Alert history goes except each condition's newest row,
    which is its current state. Learning history, notes, memories, sources and audit records
    are never touched here.
    """
    cutoff = func.now() - older_than
    report = ExpiryReport()
    report.calls_anonymised = (
        await session.execute(
            update(LLMCall)
            .where(
                LLMCall.created_at < cutoff,
                or_(LLMCall.learner_id.is_not(None), LLMCall.conversation_id.is_not(None)),
            )
            .values(learner_id=None, conversation_id=None)
        )
    ).rowcount
    report.decisions_anonymised = (
        await session.execute(
            update(DecisionCall)
            .where(
                DecisionCall.created_at < cutoff,
                or_(
                    DecisionCall.learner_id.is_not(None),
                    DecisionCall.conversation_id.is_not(None),
                ),
            )
            .values(learner_id=None, conversation_id=None)
        )
    ).rowcount
    report.turns_deleted = (
        await session.execute(
            delete(Turn).where(
                Turn.created_at < cutoff,
                Turn.status.in_([TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED]),
            )
        )
    ).rowcount
    # Alert history: everything past the window except each condition's newest row, which is
    # its current state.
    latest = aliased(AlertTransition)
    is_newest = ~(
        select(latest.id)
        .where(latest.name == AlertTransition.name, latest.seq > AlertTransition.seq)
        .exists()
    )
    report.alerts_deleted = (
        await session.execute(
            delete(AlertTransition).where(AlertTransition.created_at < cutoff, ~is_newest)
        )
    ).rowcount
    await session.commit()
    return report
```

(Import `aliased` from `sqlalchemy.orm`. If `DecisionCall` has no `conversation_id`, anonymise only `learner_id`. If a `created_at` column is timezone-naive, `func.now() - older_than` still compares correctly in SQL.)

`RETENTION`: append to the `llm_calls` and `decision_calls` reasons: " After the diagnostic window (30 days by default) the learner and conversation ids are dropped even for a live account." Add entries for `turns` (change "Cascades from the conversation." to "Cascades from the conversation. Finished turns are also deleted after the diagnostic window; the transcript is the durable copy.") and, if `alert_transitions` is not already in `RETENTION` (it has no learner_id, so it may not be), leave it out — it holds no learner data.

Worker: `_expire_diagnostics_once` calling it with `timedelta(days=settings.diagnostic_retention_days)` and logging the report when anything changed, plus loop/start/stop/registration reading `diagnostic_expiry_interval_seconds`.

- [ ] **Step 4: Run tests and the gate** — `uv run pytest tests/test_diagnostic_expiry.py tests/test_retention.py tests/test_chat_budget.py -q` → PASS. `uv run poe check && uv run poe format-check && uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/services/retention.py app/workers/tasks.py tests/test_diagnostic_expiry.py
git status
git commit -m "feat(retention): diagnostic data stops pointing at a learner after 30 days [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Export of uploaded files, and slice A's `kept` counts [S61]

**Files:**
- Modify: `app/api/v1/retention.py` (file route), `app/services/retention.py` (`export_learner` adds `file_path`), `app/services/removal.py` (forgotten keys → 0 under `kept`)
- Test: `tests/test_account_deletion.py`, `tests/test_removal.py`

**Interfaces:**
- Produces: `GET /me/export/sources/{source_id}/file` (StreamingResponse / Response with bytes); export source entries gain `file_path: str | None`.

- [ ] **Step 1: Failing tests.** Append to `tests/test_account_deletion.py`:

```python
async def test_a_learner_can_download_their_own_upload(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    from app.api.deps import get_blob_store
    from app.llm.registry import fake_llm_client
    from app.main import app
    from app.models.source import SourceKind
    from app.services import ingestion

    store = InMemoryBlobStore()
    app.dependency_overrides[get_blob_store] = lambda: store
    try:
        source = await ingestion.create_source(
            db_session,
            store,
            learner_id=api_learner.id,
            kind=SourceKind.FILE,
            origin="notes.txt",
            content_type="text/plain",
            data=b"Mitochondria make ATP.",
        )
        await ingestion.ingest_source(db_session, store, fake_llm_client(), source.id)
        other = Learner(handle=f"o-{uuid.uuid4().hex[:8]}")
        db_session.add(other)
        await db_session.flush()
        theirs = await ingestion.create_source(
            db_session,
            store,
            learner_id=other.id,
            kind=SourceKind.FILE,
            origin="theirs.txt",
            content_type="text/plain",
            data=b"Not yours.",
        )
        await db_session.commit()

        r = await api_client.get(f"{API}/me/export/sources/{source.id}/file")
        assert r.status_code == 200 and r.content == b"Mitochondria make ATP."
        assert r.headers["content-type"].startswith("text/plain")
        assert 'filename="notes.txt"' in r.headers["content-disposition"]
        exported = (await api_client.get(f"{API}/me/export")).json()
        [entry] = [s for s in exported["sources"] if s["id"] == str(source.id)]
        assert entry["file_path"] == f"/api/v1/me/export/sources/{source.id}/file"
        assert (
            await api_client.get(f"{API}/me/export/sources/{theirs.id}/file")
        ).status_code == 404
    finally:
        app.dependency_overrides.pop(get_blob_store, None)
```

Append to `tests/test_removal.py`:

```python
async def test_forget_reports_nothing_kept_that_it_removed(db_session: AsyncSession) -> None:
    learner = await _learner(db_session)
    store = InMemoryBlobStore()
    source_id = await _source(db_session, learner, store=store)
    await _lesson_citing(db_session, learner, source_id)
    await db_session.commit()

    done = await removal.delete_source(db_session, store, learner.id, source_id, forget=True)

    assert done is not None and done.kept["lessons"] == 0 and done.forgettable["lessons"] == 1
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_account_deletion.py tests/test_removal.py -q -k "download or nothing_kept"` → FAIL.

- [ ] **Step 3: Implement.**

`app/api/v1/retention.py`:

```python
@router.get("/me/export/sources/{source_id}/file")
async def export_source_file(
    source_id: uuid.UUID, session: SessionDep, learner: AccountHolder, blobstore: BlobStoreDep
) -> Response:
    """The file this learner uploaded, as they uploaded it (S61) — archived sources included."""
    source = await session.get(Source, source_id)
    if source is None or source.learner_id != learner.id or not source.blob_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    try:
        data = await blobstore.get(source.blob_key)
    except Exception as exc:  # the bytes are gone from the store
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from exc
    filename = source.origin.replace('"', "")
    return Response(
        content=data,
        media_type=source.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

(imports: `uuid`, `Response` from fastapi, `Source`.) `source_id` is on the visibility sweep's allowlist; no `CASES` entry needed.

`export_learner`: after building `sources` rows, set each entry's `file_path` — `f"/api/v1/me/export/sources/{row['id']}/file" if row.get("blob_key") else None`. Update its docstring: uploads are downloadable per file at `file_path`.

`removal.delete_source` / `delete_conversation`: when `forget` is set, build the returned `kept` with the forgotten keys zeroed — `{k: (0 if forget and k in impact.forgettable else v) for k, v in impact.kept.items()}`.

- [ ] **Step 4: Run tests and the gate** — `uv run pytest tests/test_account_deletion.py tests/test_removal.py tests/test_retention.py tests/test_authorization.py tests/test_impersonation.py -q` → PASS. `uv run poe api-types`; `uv run poe check && uv run poe format-check`; stage; `uv run poe api-contract` → green.

- [ ] **Step 5: Commit**

```bash
git add app/api/v1/retention.py app/services/retention.py app/services/removal.py frontend/src/api/schema.d.ts tests/test_account_deletion.py tests/test_removal.py
git status
git commit -m "feat(retention): a learner can download what they uploaded [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Frontend — Account page and the recovery screen [S61]

**Files:**
- Create: `frontend/src/pages/Account.tsx`, `frontend/src/pages/Recovery.tsx`, `frontend/src/pages/Recovery.test.tsx`
- Modify: `frontend/src/api/auth.ts` (`CurrentLearner.deletion_due_at`), `frontend/src/api/hooks.ts` (deletion hooks), `frontend/src/components/RequireLearner.tsx`, `frontend/src/App.tsx` (route `/app/account`), the app's navigation (add "Account" beside Memory — find where Memory's nav link lives)

**Interfaces:**
- Consumes: `DELETE /me`, `GET /me/deletion`, `POST /me/deletion/restore`, `POST /me/deletion/erase`, `/me/export`, `/me/export/sources/{id}/file`, `LearnerRead.deletion_due_at`.
- Produces: `Recovery({ dueAt })`; hooks `useRequestDeletion`, `useRestoreAccount`, `useEraseAccount`.

- [ ] **Step 1: Failing test** — `Recovery.test.tsx` (mock `../api/hooks` like `RemovalDialog.test.tsx` does):

```tsx
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

const restore = vi.fn();
const erase = vi.fn();
vi.mock("../api/hooks", () => ({
  useRestoreAccount: () => ({ mutate: restore, isPending: false, error: null }),
  useEraseAccount: () => ({ mutate: erase, isPending: false, error: null }),
}));

import { Recovery } from "./Recovery";

describe("Recovery", () => {
  it("says when the account goes, and restores it", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    expect(screen.getByText(/scheduled for deletion/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore my account" }));
    expect(restore).toHaveBeenCalled();
  });

  it("erases only after a second, explicit confirmation", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    fireEvent.click(screen.getByRole("button", { name: "Erase now" }));
    expect(erase).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Yes, erase everything now" }));
    expect(erase).toHaveBeenCalled();
  });

  it("offers the learner's data before they go", () => {
    render(<Recovery dueAt="2026-10-04T12:00:00Z" />);
    expect(screen.getByRole("link", { name: /Download your data/ })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify failure** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run src/pages/Recovery.test.tsx` → FAIL.

- [ ] **Step 3: Implement.**

`hooks.ts` additions:

```ts
/** Account deletion (S61, V12): request, restore within the window, or erase now. All three
 * change who the learner is, so they refresh the current-learner query. */
export function useRequestDeletion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.DELETE("/api/v1/me");
      if (error) throw error;
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries(),
  });
}

export function useRestoreAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/v1/me/deletion/restore");
      if (error) throw error;
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries(),
  });
}

export function useEraseAccount() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data, error } = await api.POST("/api/v1/me/deletion/erase");
      if (error) throw error;
      return data;
    },
    onSuccess: () => void queryClient.invalidateQueries(),
  });
}
```

`api/auth.ts`: `CurrentLearner` gains `deletion_due_at: string | null;`.

`Recovery.tsx`:

```tsx
import { useState } from "react";
import { API_BASE_URL } from "../api/client";
import { useEraseAccount, useRestoreAccount } from "../api/hooks";

/** A pending-deletion account (S61, V12): the one screen it reaches until restored or erased. */
export function Recovery({ dueAt }: { dueAt: string }) {
  const restore = useRestoreAccount();
  const erase = useEraseAccount();
  const [confirming, setConfirming] = useState(false);
  const when = new Date(dueAt).toLocaleDateString(undefined, { dateStyle: "long" });

  return (
    <div className="bg-base-100 flex min-h-svh items-center justify-center p-6">
      <div className="flex max-w-md flex-col gap-4">
        <h1 className="text-h1">Your account is scheduled for deletion</h1>
        <p className="text-body text-base-content/70">
          Everything will be erased on {when}. Until then you can restore it exactly as it was.
        </p>
        <button
          type="button"
          className="btn btn-primary"
          disabled={restore.isPending}
          onClick={() => restore.mutate()}
        >
          Restore my account
        </button>
        <a className="link text-body" href={`${API_BASE_URL}/api/v1/me/export`}>
          Download your data (JSON, with a link to each uploaded file)
        </a>
        {confirming ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="btn btn-error btn-sm"
              disabled={erase.isPending}
              onClick={() => erase.mutate()}
            >
              Yes, erase everything now
            </button>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConfirming(false)}>
              Keep it until {when}
            </button>
          </div>
        ) : (
          <button type="button" className="btn btn-outline btn-sm" onClick={() => setConfirming(true)}>
            Erase now
          </button>
        )}
      </div>
    </div>
  );
}
```

`RequireLearner.tsx`: after the `!learner` branch, `if (learner.deletion_due_at) return <Recovery dueAt={learner.deletion_due_at} />;` (import it). After `useEraseAccount` succeeds the current-learner query refetches, gets 401 (the account is gone) and the existing branch sends the browser to sign-in; also call the existing Clerk sign-out if `clerkEnabled` — check how `ClerkSessionWatcher`/`useLogout` in `api/auth.ts` sign out and reuse that in `Recovery`'s erase `onSuccess`.

`Account.tsx` (route `/app/account`, linked in the nav beside Memory): a "Your data" section with the export link, and a "Delete account" section: "Your account will be closed at once and erased after 7 days. Signing in again before then lets you restore it." with a Delete account button → inline confirm → `useRequestDeletion()`; on success sign out (as above). Also an "Erase now instead" button inside that confirm that calls `useRequestDeletion` then, on its success, `useEraseAccount`.

- [ ] **Step 4: Run tests and the build** — `cd frontend && VITE_CLERK_PUBLISHABLE_KEY= npx vitest run && npm run build`, prettier and eslint on changed files → green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Account.tsx frontend/src/pages/Recovery.tsx frontend/src/pages/Recovery.test.tsx frontend/src/api/auth.ts frontend/src/api/hooks.ts frontend/src/components/RequireLearner.tsx frontend/src/App.tsx <the nav file>
git status
git commit -m "feat(web): delete an account with a seven-day way back, and download your data [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Tracker, RUNBOOK and CLAUDE.md [S61]

- [ ] **Step 1:** Tracker S61 → **Done** (software): "V11 (slice A) and V12 (slice B) delivered: archive/delete/forget; account deletion disables access at once with a seven-day self-service recovery window and erase-now, then a worker erases, including the identity provider's copy; failed file and provider erasures are retried until done with an alert on persistent refusal; diagnostic rows are anonymised or deleted after 30 days (learning history, notes and audit kept); uploads downloadable from the export. Operating remainder (backup creation and its 30-day window) belongs to workstream 7." Move the row to completed work per the tracker's convention, with evidence links to `app/services/retention.py`, `app/services/removal.py`, `tests/test_account_deletion.py`, `tests/test_erasure_retry.py`, `tests/test_diagnostic_expiry.py`.
- [ ] **Step 2:** RUNBOOK: a new section "16. Account deletion and retention (S61)" — the lifecycle, the three worker loops and their settings (`account_recovery_days`, `diagnostic_retention_days`, the three intervals, `alert_stuck_erasure_attempts`), what `erasures_stuck` means and how to read `pending_erasures`, and that `diagnostic_retention_days` is the target window for backups once they exist.
- [ ] **Step 3:** CLAUDE.md Key Technical Decisions, after the archive/delete/forget bullet:

```markdown
- **Deleting an account is a state, then an erase** (S61; V12) — access ends at the request
  and every session is revoked; signing in again within seven days reaches only the recovery
  routes (`AccountHolder`); then a worker erases every store and the identity provider's copy.
  What the object store or provider refuses becomes a `pending_erasures` row retried until done.
  Diagnostic rows keep nothing pointing at a learner past 30 days. See RUNBOOK §16.
```

- [ ] **Step 4:** `uv run poe check` → green, then:

```bash
git add docs/guru-suggestions-tracker.md docs/RUNBOOK.md CLAUDE.md
git status
git commit -m "docs: record account lifecycle and retention [S61]

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
