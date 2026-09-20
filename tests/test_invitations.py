"""Who may invite somebody into Guru, and what an invitation does at both ends (S21).

Issuing and revoking are the two acts an administrator can take before the person they concern
has an account at all, and each one is required to leave the audit trail this slice exists to
create — see `app.services.accounts`. These tests drive the real admin routes with a fake
identity provider (the same `provider` fixture Task 3's `test_identity_exchange.py` uses), never
the service function directly, so "the provider was asked" and "the row was written" mean
exactly what `/auth/exchange` checks on the other end.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings, get_identity_provider
from app.core.config import get_settings
from app.core.identity import FakeIdentityProvider
from app.main import app
from app.models.auth import AccountAction, AccountActionKind, Invitation
from app.models.learner import Learner
from tests.test_impersonation import REASON, _learner, _visit

API = "/api/v1"


@pytest.fixture
def provider() -> Iterator[FakeIdentityProvider]:
    fake = FakeIdentityProvider()
    app.dependency_overrides[get_identity_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_identity_provider, None)


async def _create(client: AsyncClient, email: str):
    return await client.post(f"{API}/admin/invitations", json={"email": email})


async def _revoke(client: AsyncClient, invitation_id: str):
    return await client.post(f"{API}/admin/invitations/{invitation_id}/revoke")


async def test_inviting_records_the_invitation_and_asks_the_provider_to_send_it(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    admin_id = (await admin_client.get(f"{API}/auth/me")).json()["id"]

    r = await _create(admin_client, "New@Example.com")

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new@example.com"
    assert body["status"] == "open"

    row = await db_session.scalar(select(Invitation).where(Invitation.id == uuid.UUID(body["id"])))
    assert row is not None
    assert row.email == "new@example.com"
    assert str(row.invited_by_learner_id) == admin_id
    assert row.provider_invitation_id is not None
    # The provider was asked for exactly that address, and nothing else.
    assert provider.invitations == {row.provider_invitation_id: "new@example.com"}

    action = await db_session.scalar(
        select(AccountAction).where(AccountAction.action == AccountActionKind.INVITE)
    )
    assert action is not None
    assert str(action.actor_learner_id) == admin_id
    assert action.email == "new@example.com"


async def test_the_stored_address_is_normalised_even_when_the_caller_was_not(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Pins the task-4 ruling: the write side normalises, not just `/auth/exchange`'s read of it.

    Postgres's bare `TRIM()` strips only spaces, where Python's `.strip()` (what
    `normalise_email` uses) strips a wider whitespace class — so a dirty stored address could
    still slip past the exchange's own SQL-side normalisation. This asserts what actually landed
    in the column, not just what the API echoed back.
    """
    r = await _create(admin_client, "  Mixed.Case@Example.com  ")

    assert r.status_code == 201, r.text
    row = await db_session.scalar(
        select(Invitation).where(Invitation.id == uuid.UUID(r.json()["id"]))
    )
    assert row is not None
    assert row.email == "mixed.case@example.com"


async def test_an_address_that_already_has_an_account_is_refused(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    db_session.add(Learner(handle="taken", email="taken@example.com"))
    await db_session.commit()

    r = await _create(admin_client, "taken@example.com")

    assert r.status_code == 409, r.text
    assert await db_session.scalar(select(Invitation)) is None
    assert await db_session.scalar(select(AccountAction)) is None
    # The account check runs before the provider is ever asked.
    assert provider.invitations == {}


async def test_a_second_open_invitation_for_one_address_is_refused(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    first = await _create(admin_client, "dup@example.com")
    assert first.status_code == 201, first.text

    second = await _create(admin_client, "dup@example.com")

    assert second.status_code == 409, second.text
    rows = list(await db_session.scalars(select(Invitation)))
    assert len(rows) == 1


async def test_re_inviting_an_address_whose_invitation_was_revoked_is_allowed(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    first = await _create(admin_client, "again@example.com")
    assert first.status_code == 201, first.text
    revoked = await _revoke(admin_client, first.json()["id"])
    assert revoked.status_code == 200, revoked.text

    second = await _create(admin_client, "again@example.com")

    assert second.status_code == 201, second.text
    rows = list(
        await db_session.scalars(
            select(Invitation)
            .where(Invitation.email == "again@example.com")
            .order_by(Invitation.created_at)
        )
    )
    assert len(rows) == 2
    assert rows[0].revoked_at is not None
    assert rows[1].revoked_at is None and rows[1].accepted_at is None


async def test_nothing_is_recorded_when_the_provider_cannot_be_reached(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    provider.fail_next = True

    r = await _create(admin_client, "unlucky@example.com")

    assert r.status_code == 502, r.text
    assert await db_session.scalar(select(Invitation)) is None
    assert await db_session.scalar(select(AccountAction)) is None


async def test_revoking_closes_the_invitation_at_both_ends(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    admin_id = (await admin_client.get(f"{API}/auth/me")).json()["id"]
    created = await _create(admin_client, "close@example.com")
    assert created.status_code == 201, created.text
    invitation_id = created.json()["id"]
    provider_invitation_id = await db_session.scalar(
        select(Invitation.provider_invitation_id).where(Invitation.id == uuid.UUID(invitation_id))
    )

    r = await _revoke(admin_client, invitation_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "revoked"

    row = await db_session.scalar(
        select(Invitation).where(Invitation.id == uuid.UUID(invitation_id))
    )
    assert row is not None
    assert row.revoked_at is not None
    assert str(row.revoked_by_learner_id) == admin_id
    assert provider_invitation_id in provider.revoked

    action = await db_session.scalar(
        select(AccountAction).where(AccountAction.action == AccountActionKind.REVOKE_INVITATION)
    )
    assert action is not None
    assert str(action.actor_learner_id) == admin_id
    assert action.email == "close@example.com"


async def test_revoking_an_invitation_that_is_not_open_is_refused(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    admin_id = uuid.UUID((await admin_client.get(f"{API}/auth/me")).json()["id"])
    learner = Learner(handle="accepted-learner", email="accepted@example.com")
    db_session.add(learner)
    await db_session.flush()
    invitation = Invitation(
        email="accepted@example.com",
        invited_by_learner_id=admin_id,
        invited_by_handle="admin",
        accepted_at=datetime.now(UTC),
        accepted_learner_id=learner.id,
    )
    db_session.add(invitation)
    await db_session.commit()

    r = await _revoke(admin_client, str(invitation.id))

    assert r.status_code == 409, r.text


async def test_revoking_survives_a_provider_that_will_not_answer(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    created = await _create(admin_client, "stubborn@example.com")
    assert created.status_code == 201, created.text
    invitation_id = created.json()["id"]
    provider.fail_next = True

    r = await _revoke(admin_client, invitation_id)

    # The local revocation still succeeds: refusing to record it would leave an invitation
    # Guru still believes is open, which is the worse of the two errors.
    assert r.status_code == 200, r.text
    row = await db_session.scalar(
        select(Invitation).where(Invitation.id == uuid.UUID(invitation_id))
    )
    assert row is not None
    assert row.revoked_at is not None


async def test_listing_shows_open_accepted_and_revoked(
    admin_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    admin_id = uuid.UUID((await admin_client.get(f"{API}/auth/me")).json()["id"])

    opened = await _create(admin_client, "open@example.com")
    assert opened.status_code == 201, opened.text

    revoked_created = await _create(admin_client, "revoked@example.com")
    assert revoked_created.status_code == 201, revoked_created.text
    assert (await _revoke(admin_client, revoked_created.json()["id"])).status_code == 200

    learner = Learner(handle="accepted2", email="accepted2@example.com")
    db_session.add(learner)
    await db_session.flush()
    db_session.add(
        Invitation(
            email="accepted2@example.com",
            invited_by_learner_id=admin_id,
            invited_by_handle="admin",
            accepted_at=datetime.now(UTC),
            accepted_learner_id=learner.id,
        )
    )
    await db_session.commit()

    r = await admin_client.get(f"{API}/admin/invitations")

    assert r.status_code == 200, r.text
    statuses = {row["email"]: row["status"] for row in r.json()}
    assert statuses == {
        "open@example.com": "open",
        "revoked@example.com": "revoked",
        "accepted2@example.com": "accepted",
    }


async def test_an_ordinary_learner_cannot_invite_or_list(
    api_client: AsyncClient, admin_client: AsyncClient, provider: FakeIdentityProvider
) -> None:
    created = await _create(admin_client, "target@example.com")
    assert created.status_code == 201, created.text
    invitation_id = created.json()["id"]

    assert (await _create(api_client, "nope@example.com")).status_code == 403
    assert (await api_client.get(f"{API}/admin/invitations")).status_code == 403
    assert (await _revoke(api_client, invitation_id)).status_code == 403


async def test_an_impersonated_administrator_cannot_invite(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    db_session: AsyncSession,
    provider: FakeIdentityProvider,
) -> None:
    """Borrowed identity is not an administrator, even onto an administrator's own account.

    Mirrors `test_a_visit_is_never_an_administrator` in tests/test_impersonation.py.
    """
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    try:
        other_admin = await _learner(db_session, "other-admin", admin=True)
        _, visit = await _visit(admin_client, other_admin, reason=REASON)
        anon_client.headers["authorization"] = f"Bearer {visit['token']}"

        r = await _create(anon_client, "borrowed@example.com")
    finally:
        app.dependency_overrides.pop(get_app_settings, None)

    assert r.status_code == 403, r.text
    assert await db_session.scalar(select(Invitation)) is None


async def test_without_a_provider_inviting_says_so(admin_client: AsyncClient) -> None:
    app.dependency_overrides[get_identity_provider] = lambda: None
    try:
        r = await _create(admin_client, "someone@example.com")
    finally:
        app.dependency_overrides.pop(get_identity_provider, None)

    assert r.status_code == 503, r.text
