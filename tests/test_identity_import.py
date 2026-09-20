"""Moving existing accounts into the provider, passwords intact (S21).

The property under test is the one an operator is trusting: run this once, and every learner
who had a password can still sign in with it, nobody acquires somebody else's account, and a
provider that falls over halfway does not have to be untangled by hand.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import FakeIdentityProvider, ProviderError
from app.models.auth import LegacyPasswordDigest
from app.models.learner import Learner
from app.workers import identity_import

DIGEST = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$0123456789abcdefghijklmnopqrstuv"


@pytest.fixture
def provider() -> FakeIdentityProvider:
    return FakeIdentityProvider()


async def _learner(
    session: AsyncSession, *, email: str | None = None, digest: str | None = None
) -> Learner:
    tag = uuid.uuid4().hex[:8]
    learner = Learner(handle=f"imp-{tag}", email=email)
    session.add(learner)
    await session.flush()
    if digest is not None:
        session.add(LegacyPasswordDigest(learner_id=learner.id, digest=digest))
        await session.flush()
    return learner


async def _run(session: AsyncSession, provider: FakeIdentityProvider) -> list:
    steps = await identity_import.plan(session, provider)
    await identity_import.apply(session, provider, steps)
    return steps


async def test_a_learner_with_a_password_keeps_it(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The whole point: nobody is told to reset a password they never chose to lose."""
    learner = await _learner(db_session, email="keeps@example.com", digest=DIGEST)

    await _run(db_session, provider)

    assert learner.auth_subject is not None
    created = provider.users[learner.auth_subject]
    assert created.external_id == str(learner.id)
    # The digest reached the provider as-is, which is what lets the old password still work.
    assert provider.imported[created.subject] == DIGEST
    # And is gone from Guru: a credential for an account whose credential now lives elsewhere
    # is a copy nobody is maintaining.
    assert (
        await db_session.scalar(
            select(LegacyPasswordDigest).where(LegacyPasswordDigest.learner_id == learner.id)
        )
        is None
    )


async def test_a_learner_with_no_password_is_created_without_one(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    learner = await _learner(db_session, email="nopass@example.com")

    await _run(db_session, provider)

    assert learner.auth_subject is not None
    assert provider.imported[learner.auth_subject] is None


async def test_an_address_the_provider_already_has_is_linked_not_duplicated(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Creating a second provider user for one address is how somebody ends up with two
    accounts and no way to tell which holds their work."""
    existing = provider.add_user(emails=["already@example.com"])
    learner = await _learner(db_session, email="already@example.com")

    steps = await _run(db_session, provider)

    assert [s.action for s in steps if s.learner_id == learner.id] == ["link"]
    assert learner.auth_subject == existing.subject
    assert len(provider.users) == 1


async def test_an_address_matching_two_provider_users_is_refused(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Picking one would hand somebody another person's account. A human decides."""
    provider.add_user(emails=["ambiguous@example.com"])
    provider.add_user(emails=["ambiguous@example.com"])
    learner = await _learner(db_session, email="ambiguous@example.com")

    steps = await _run(db_session, provider)

    step = next(s for s in steps if s.learner_id == learner.id)
    assert step.action == "skip"
    assert "a human must decide" in step.why
    assert learner.auth_subject is None


async def test_a_learner_with_no_address_is_skipped_and_said_so(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The dev learner, and anyone else who predates addresses entirely."""
    learner = await _learner(db_session)

    steps = await _run(db_session, provider)

    step = next(s for s in steps if s.learner_id == learner.id)
    assert step.action == "skip"
    assert "no address" in step.why
    assert learner.auth_subject is None


async def test_a_dry_run_performs_nothing(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Performs nothing — it does *read*, which is a deliberate departure from the plan's
    "no provider calls".

    A dry run has to ask the provider which addresses it already holds, or it cannot tell an
    operator whether a learner would be created or linked, which is the only question the dry
    run exists to answer. Reads change nothing; what must not happen is a write, to Guru or to
    the provider, and that is what this pins.
    """
    learner = await _learner(db_session, email="dry@example.com", digest=DIGEST)

    steps = await identity_import.plan(db_session, provider)

    assert [s.action for s in steps if s.learner_id == learner.id] == ["create"]
    assert provider.users == {}
    assert learner.auth_subject is None
    assert (
        await db_session.scalar(
            select(LegacyPasswordDigest).where(LegacyPasswordDigest.learner_id == learner.id)
        )
        is not None
    )


async def test_running_twice_changes_nothing_the_second_time(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """An import that is not safe to re-run is an import nobody dares re-run after a failure."""
    learner = await _learner(db_session, email="twice@example.com", digest=DIGEST)

    await _run(db_session, provider)
    subject_after_first = learner.auth_subject
    users_after_first = dict(provider.users)

    steps = await _run(db_session, provider)

    assert learner.auth_subject == subject_after_first
    assert provider.users == users_after_first
    assert [s.action for s in steps if s.learner_id == learner.id] == ["skip"]


async def test_a_provider_failure_leaves_the_already_imported_linked(
    db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Each learner is its own transaction, so a re-run resumes rather than restarting against
    a provider that already holds half the accounts."""
    first = await _learner(db_session, email="first@example.com")
    second = await _learner(db_session, email="second@example.com")

    steps = await identity_import.plan(db_session, provider)
    ordered = [s for s in steps if s.learner_id in {first.id, second.id}]
    provider.fail_next = False
    await identity_import.apply(db_session, provider, ordered[:1])
    provider.fail_next = True
    with pytest.raises(ProviderError):
        await identity_import.apply(db_session, provider, ordered[1:])

    # Whichever went first is linked and stays linked; the other is untouched and re-runnable.
    done, pending = (first, second) if ordered[0].learner_id == first.id else (second, first)
    await db_session.refresh(done)
    await db_session.refresh(pending)
    assert done.auth_subject is not None
    assert pending.auth_subject is None
