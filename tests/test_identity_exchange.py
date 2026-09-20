"""Signing in with a hosted identity, and who that lets in (S21).

Clerk says *who* somebody is. Every question about whether they may use Guru at all — invited,
suspended, already known — is answered here, which is why these tests drive the real route with
a fake provider rather than testing a helper.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_identity_provider
from app.core.identity import FakeIdentityProvider
from app.main import app
from app.models.auth import Invitation
from app.models.learner import Learner

API = "/api/v1"


@pytest.fixture
def provider() -> Iterator[FakeIdentityProvider]:
    fake = FakeIdentityProvider()
    app.dependency_overrides[get_identity_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_identity_provider, None)


async def _invitation(session: AsyncSession, email: str, **kwargs: object) -> Invitation:
    invitation = Invitation(
        email=email, invited_by_learner_id=None, invited_by_handle="operator", **kwargs
    )
    session.add(invitation)
    await session.flush()
    return invitation


async def _exchange(client: AsyncClient, token: str):
    return await client.post(f"{API}/auth/exchange", headers={"Authorization": f"Bearer {token}"})


async def test_an_invited_address_enrolls_and_is_signed_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    user = provider.add_user(emails=["Invitee@example.com"], display_name="Ada Lovelace")

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "invitee@example.com"
    assert body["display_name"] == "Ada Lovelace"
    learner = await db_session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
    assert learner is not None
    invitation = await db_session.scalar(select(Invitation))
    assert invitation is not None
    assert invitation.accepted_at is not None and invitation.accepted_learner_id == learner.id
    # Signed in: the session cookie is set, and it authenticates the same learner.
    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["id"] == str(learner.id)


async def test_an_uninvited_identity_is_refused_and_creates_nothing(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    user = provider.add_user(emails=["stranger@example.com"])
    before = await db_session.scalar(select(Learner.id).limit(1))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403
    assert "invite" in r.json()["detail"].lower()
    after = await db_session.scalars(select(Learner.id))
    assert (before is None) == (len(list(after)) == 0)


async def test_an_unverified_address_does_not_claim_an_invitation(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The provider only hands us verified addresses; an identity with none is nobody here."""
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    user = provider.add_user(emails=[])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403
    invitation = await db_session.scalar(select(Invitation))
    assert invitation is not None
    assert invitation.accepted_at is None


async def test_a_revoked_invitation_does_not_let_anybody_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(db_session, "invitee@example.com", revoked_at=datetime.now(UTC))
    await db_session.commit()
    user = provider.add_user(emails=["invitee@example.com"])

    assert (await _exchange(anon_client, provider.token_for(user.subject))).status_code == 403


async def test_an_invitation_is_spent_once(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    first = provider.add_user(emails=["invitee@example.com"])
    second = provider.add_user(emails=["invitee@example.com"])

    assert (await _exchange(anon_client, provider.token_for(first.subject))).status_code == 200
    # A second identity with the same address is not the invited person arriving twice.
    assert (await _exchange(anon_client, provider.token_for(second.subject))).status_code == 403


async def test_an_existing_account_is_linked_by_its_verified_address(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The re-invitation path: the learner's work is already here and must stay theirs."""
    learner = Learner(handle="ada", email="ada@example.com", display_name="Ada")
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["ADA@example.com"])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200 and r.json()["id"] == str(learner.id)
    await db_session.refresh(learner)
    assert learner.auth_subject == user.subject


async def test_an_imported_account_is_linked_by_its_external_id(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """What `poe identity-import` sets up: the provider carries the learner's own id."""
    learner = Learner(handle="ada2", email="ada2@example.com")
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["somethingelse@example.com"], external_id=str(learner.id))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200 and r.json()["id"] == str(learner.id)


async def test_an_identity_matching_two_accounts_is_refused_rather_than_guessed(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Picking one would hand somebody another learner's work."""
    db_session.add(Learner(handle="one", email="one@example.com"))
    db_session.add(Learner(handle="two", email="two@example.com"))
    await db_session.commit()
    user = provider.add_user(emails=["one@example.com", "two@example.com"])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 409
    assert (
        await db_session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
    ) is None


async def test_a_suspended_account_cannot_sign_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    learner = Learner(handle="susp", email="susp@example.com", suspended_at=datetime.now(UTC))
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["susp@example.com"], external_id=str(learner.id))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403 and "suspended" in r.json()["detail"].lower()
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_an_address_change_at_the_provider_follows_the_learner(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    learner = Learner(handle="moved", email="old@example.com", auth_subject="user_moved")
    db_session.add(learner)
    await db_session.commit()
    provider.add_user(emails=["new@example.com"], subject="user_moved")

    assert (await _exchange(anon_client, provider.token_for("user_moved"))).status_code == 200

    await db_session.refresh(learner)
    assert learner.email == "new@example.com"


async def test_an_address_another_learner_holds_is_not_taken(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Syncing must never make two accounts collide on one address."""
    db_session.add(Learner(handle="holder", email="taken@example.com"))
    learner = Learner(handle="mover", email="mine@example.com", auth_subject="user_x")
    db_session.add(learner)
    await db_session.commit()
    provider.add_user(emails=["taken@example.com"], subject="user_x")

    assert (await _exchange(anon_client, provider.token_for("user_x"))).status_code == 200

    await db_session.refresh(learner)
    assert learner.email == "mine@example.com"


async def test_a_bad_token_is_one_answer(
    anon_client: AsyncClient, provider: FakeIdentityProvider
) -> None:
    assert (await _exchange(anon_client, "fake:nobody")).status_code == 401
    assert (await anon_client.post(f"{API}/auth/exchange")).status_code == 401


async def test_an_unreachable_provider_is_not_a_refusal(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """502, not 403: "we could not ask" and "you may not" are different things to a learner."""
    user = provider.add_user(emails=["invitee@example.com"])
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    provider.fail_next = True

    assert (await _exchange(anon_client, provider.token_for(user.subject))).status_code == 502


async def test_without_a_provider_the_route_says_so(anon_client: AsyncClient) -> None:
    """No Clerk keys configured: a checkout that still runs, and does not quietly admit anybody."""
    app.dependency_overrides[get_identity_provider] = lambda: None
    try:
        r = await anon_client.post(
            f"{API}/auth/exchange", headers={"Authorization": "Bearer whatever"}
        )
    finally:
        app.dependency_overrides.pop(get_identity_provider, None)

    assert r.status_code == 503
