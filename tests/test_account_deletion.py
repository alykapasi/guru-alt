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
    first = (
        await retention.request_deletion(db_session, api_learner.id, settings=settings)
    ).deletion_due_at
    second = (
        await retention.request_deletion(db_session, api_learner.id, settings=settings)
    ).deletion_due_at
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
    due, not_yet = (
        Learner(handle=f"d-{uuid.uuid4().hex[:8]}"),
        Learner(handle=f"n-{uuid.uuid4().hex[:8]}"),
    )
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
    assert (
        await retention.erase_due(
            db_session, InMemoryBlobStore(), FakeIdentityProvider(), now=datetime.now(UTC)
        )
        == 0
    )


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
    from starlette.requests import Request

    from app.api.deps import get_current_learner
    from app.services.auth import Authenticated

    await retention.request_deletion(db_session, api_learner.id, settings=get_settings())
    pending = await db_session.get(Learner, api_learner.id, populate_existing=True)
    assert pending is not None
    try:
        await get_current_learner(
            Request({"type": "http"}),
            Authenticated(
                learner=pending, impersonated_by_id=uuid.uuid4(), session_id=uuid.uuid4()
            ),
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a pending account was admitted")


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


async def test_a_download_survives_a_filename_outside_latin_1(
    api_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    from app.api.deps import get_blob_store
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
            origin='細胞 "notes"\r\n.txt',
            content_type="text/plain",
            data=b"Cells.",
        )
        await db_session.commit()

        r = await api_client.get(f"{API}/me/export/sources/{source.id}/file")

        assert r.status_code == 200 and r.content == b"Cells."
        disposition = r.headers["content-disposition"]
        assert "\r" not in disposition and "\n" not in disposition
        assert "filename*=UTF-8''" in disposition
    finally:
        app.dependency_overrides.pop(get_blob_store, None)
