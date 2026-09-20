"""Suspending and reinstating an account: what it stops, what it leaves alone (S21).

Clerk's free tier has no account ban, so Guru is the only place that can stop somebody, and
the stop has to bite a session that is already live — not only the next sign-in attempt. These
tests drive the real admin routes end to end (never ``app.services.accounts`` directly), because
"the session stops working" is a property of ``resolve_session`` and the routes wired to it, not
of the service function in isolation.
"""

import uuid
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_identity_provider
from app.core.config import get_settings
from app.core.identity import FakeIdentityProvider
from app.main import app
from app.models.auth import AccountAction, AccountActionKind
from app.models.chat import Conversation
from app.models.knowledge import KC, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState
from app.models.note import Note
from tests.conftest import sign_in
from tests.test_impersonation import REASON, _learner, _visit

API = "/api/v1"


@pytest.fixture
def provider() -> Iterator[FakeIdentityProvider]:
    fake = FakeIdentityProvider()
    app.dependency_overrides[get_identity_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_identity_provider, None)


async def _suspend(client: AsyncClient, learner_id: uuid.UUID | str, *, reason: str = REASON):
    return await client.post(f"{API}/admin/learners/{learner_id}/suspend", json={"reason": reason})


async def _reinstate(
    client: AsyncClient, learner_id: uuid.UUID | str, *, reason: str | None = None
):
    body = {} if reason is None else {"reason": reason}
    return await client.post(f"{API}/admin/learners/{learner_id}/reinstate", json=body)


# --- the bite -----------------------------------------------------------------------------


async def test_suspending_ends_the_learners_sessions_immediately(
    admin_client: AsyncClient, api_client: AsyncClient, api_learner: Learner
) -> None:
    """A session issued before the suspension stops working after it, with the holder doing
    nothing — no new request from them, no fresh sign-in, nothing but the admin's act."""
    assert (await api_client.get(f"{API}/auth/me")).status_code == 200

    r = await _suspend(admin_client, api_learner.id)
    assert r.status_code == 200, r.text

    assert (await api_client.get(f"{API}/auth/me")).status_code == 401


async def test_a_session_issued_after_suspension_is_still_refused(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    """``suspend``'s own revocation ``UPDATE`` only reaches sessions that already exist at that
    instant — it cannot touch one minted afterwards. This isolates the *other* half of the bite:
    ``resolve_session``'s own ``suspended_at`` check, which is what stops a session like this one,
    issued straight through ``tests.conftest.sign_in`` (i.e. ``auth.issue``, which never consults
    suspension) with no revocation involved at all. Fix round 1, Important 2: deleting the
    ``resolve_session`` branch fails this test; deleting only the revocation ``UPDATE`` in
    ``accounts.suspend`` does not, because there is nothing here for that ``UPDATE`` to reach."""
    assert (await _suspend(admin_client, api_learner.id)).status_code == 200

    await sign_in(anon_client, db_session, api_learner)

    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_an_administrators_visit_to_a_suspended_account_still_works(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    """Support has to be able to look at exactly the account that is in trouble (P10/V13). A
    visit is the administrator's own credential, not the learner's, so it must not be caught by
    the same gate that ends the learner's own sessions."""
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        assert (await _suspend(admin_client, api_learner.id)).status_code == 200

        _, visit = await _visit(admin_client, api_learner)
        anon_client.headers["authorization"] = f"Bearer {visit['token']}"
        r = await anon_client.get(f"{API}/auth/me")
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(api_learner.id)


async def test_suspending_the_visited_learner_after_the_visit_started_leaves_the_visit_open(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    """Unlike the test above (which suspends *before* the visit starts, so the target has no
    live session for the revocation ``UPDATE`` to reach in the first place), this opens the
    visit first. At the moment of suspension the target now has two live sessions — their own
    and the visit's — and only the ``impersonated_by_id IS NULL`` filter on that ``UPDATE`` keeps
    the visit's alive. Fix round 1, Important 3(b): this makes that filter load-bearing in a
    test rather than incidental."""
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        _, visit = await _visit(admin_client, api_learner)
        anon_client.headers["authorization"] = f"Bearer {visit['token']}"
        assert (await anon_client.get(f"{API}/auth/me")).status_code == 200

        assert (await _suspend(admin_client, api_learner.id)).status_code == 200

        r = await anon_client.get(f"{API}/auth/me")
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 200, r.text
    assert r.json()["id"] == str(api_learner.id)


async def test_suspending_the_visiting_administrator_ends_the_visit(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    """The clause a suspended admin's mid-flight visit relies on
    (``app/services/auth.py``'s ``admin.suspended_at is not None`` check) had no test at all —
    without it, a suspended administrator would go on acting *as the learner* through a visit
    already open, until the visit token's own ``expires_at``: precisely the laundering
    ``get_current_admin`` exists to prevent (fix round 1, Important 3(a)). An administrator
    cannot suspend themselves, so a second admin does it."""
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        visited_admin_id = (await admin_client.get(f"{API}/auth/me")).json()["id"]
        _, visit = await _visit(admin_client, api_learner)
        anon_client.headers["authorization"] = f"Bearer {visit['token']}"
        assert (await anon_client.get(f"{API}/auth/me")).status_code == 200

        second_admin = await _learner(db_session, "second-admin", admin=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as second_admin_client:
            await sign_in(second_admin_client, db_session, second_admin)
            suspended = await _suspend(second_admin_client, visited_admin_id)
            assert suspended.status_code == 200, suspended.text

        r = await anon_client.get(f"{API}/auth/me")
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 401, r.text


async def test_a_suspended_learner_cannot_exchange_a_new_identity_for_a_session(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    provider: FakeIdentityProvider,
) -> None:
    learner = Learner(
        handle=f"exch-{uuid.uuid4().hex[:8]}", email="exch@example.com", auth_subject="user_exch"
    )
    db_session.add(learner)
    await db_session.flush()
    user = provider.add_user(emails=["exch@example.com"], subject="user_exch")

    assert (await _suspend(admin_client, learner.id)).status_code == 200

    r = await anon_client.post(
        f"{API}/auth/exchange",
        headers={"Authorization": f"Bearer {provider.token_for(user.subject)}"},
    )

    assert r.status_code == 403, r.text
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_a_suspended_administrator_loses_the_portal_and_the_operational_reads(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    other_admin = await _learner(db_session, "other-admin", admin=True)
    await sign_in(anon_client, db_session, other_admin)
    assert (await anon_client.get(f"{API}/admin/learners")).status_code == 200
    assert (await anon_client.get(f"{API}/ops/spend")).status_code == 200

    assert (await _suspend(admin_client, other_admin.id)).status_code == 200

    assert (await anon_client.get(f"{API}/admin/learners")).status_code == 401
    assert (await anon_client.get(f"{API}/ops/spend")).status_code == 401


async def test_reinstating_lets_them_sign_in_again(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    api_learner: Learner,
) -> None:
    assert (await _suspend(admin_client, api_learner.id)).status_code == 200

    r = await _reinstate(admin_client, api_learner.id)
    assert r.status_code == 200, r.text
    assert r.json()["suspended_at"] is None

    await sign_in(anon_client, db_session, api_learner)
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 200


# --- the floor ----------------------------------------------------------------------------


async def test_suspension_requires_a_reason(
    admin_client: AsyncClient, api_learner: Learner
) -> None:
    r = await _suspend(admin_client, api_learner.id, reason="short")
    assert r.status_code == 422, r.text


async def test_an_administrator_cannot_suspend_themselves(admin_client: AsyncClient) -> None:
    me = (await admin_client.get(f"{API}/auth/me")).json()
    r = await _suspend(admin_client, me["id"])
    assert r.status_code == 409, r.text


async def test_an_ordinary_learner_cannot_suspend_anybody(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    target = await _learner(db_session, "target")
    r = await _suspend(api_client, target.id)
    assert r.status_code == 403, r.text


async def test_suspending_an_already_suspended_account_is_refused(
    admin_client: AsyncClient, api_learner: Learner
) -> None:
    assert (await _suspend(admin_client, api_learner.id)).status_code == 200
    r = await _suspend(admin_client, api_learner.id)
    assert r.status_code == 409, r.text


async def test_reinstating_an_account_that_is_not_suspended_is_refused(
    admin_client: AsyncClient, api_learner: Learner
) -> None:
    r = await _reinstate(admin_client, api_learner.id)
    assert r.status_code == 409, r.text


# --- the record -----------------------------------------------------------------------------


async def test_every_suspension_and_reinstatement_is_recorded(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    me = (await admin_client.get(f"{API}/auth/me")).json()

    assert (await _suspend(admin_client, api_learner.id, reason=REASON)).status_code == 200
    assert (
        await _reinstate(admin_client, api_learner.id, reason="turned out to be a mistake")
    ).status_code == 200

    suspend_action = await db_session.scalar(
        select(AccountAction).where(AccountAction.action == AccountActionKind.SUSPEND)
    )
    assert suspend_action is not None
    assert suspend_action.actor_handle == me["handle"]
    assert str(suspend_action.actor_learner_id) == me["id"]
    assert suspend_action.learner_handle == api_learner.handle
    assert suspend_action.learner_id == api_learner.id
    assert suspend_action.reason == REASON

    reinstate_action = await db_session.scalar(
        select(AccountAction).where(AccountAction.action == AccountActionKind.REINSTATE)
    )
    assert reinstate_action is not None
    assert reinstate_action.actor_handle == me["handle"]
    assert reinstate_action.learner_handle == api_learner.handle
    assert reinstate_action.reason == "turned out to be a mistake"


async def test_the_roster_shows_who_is_suspended(
    admin_client: AsyncClient, api_learner: Learner
) -> None:
    assert (await _suspend(admin_client, api_learner.id)).status_code == 200

    r = await admin_client.get(f"{API}/admin/learners")
    assert r.status_code == 200
    rows = {row["id"]: row for row in r.json()["learners"]}
    assert rows[str(api_learner.id)]["suspended_at"] is not None


async def test_suspension_touches_no_learner_work(
    admin_client: AsyncClient, db_session: AsyncSession, api_learner: Learner
) -> None:
    """Their conversations, notes and mastery rows are all still there afterwards — suspension
    is not deletion."""
    tag = uuid.uuid4().hex[:8]
    subject = Subject(slug=f"s-{tag}", name=f"Subject {tag}", owner_learner_id=api_learner.id)
    db_session.add(subject)
    await db_session.flush()
    topic = Topic(subject_id=subject.id, slug=f"t-{tag}", name="Topic")
    db_session.add(topic)
    await db_session.flush()
    kc = KC(topic_id=topic.id, slug=f"k-{tag}", name="Component")
    db_session.add(kc)
    await db_session.flush()

    conversation = Conversation(learner_id=api_learner.id)
    note = Note(learner_id=api_learner.id, topic_id=topic.id, learner_authored_md="mine")
    state = LearnerKCState(learner_id=api_learner.id, kc_id=kc.id, ability=1.0, uncertainty=0.5)
    db_session.add_all([conversation, note, state])
    await db_session.commit()

    assert (await _suspend(admin_client, api_learner.id)).status_code == 200

    assert await db_session.get(Conversation, conversation.id) is not None
    assert await db_session.get(Note, note.id) is not None
    assert await db_session.get(LearnerKCState, state.id) is not None
