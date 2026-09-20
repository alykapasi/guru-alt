"""The rows that make enrollment invite-controlled and administrative acts reviewable (S21)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AccountAction, AccountActionKind, Invitation
from app.models.learner import Learner
from app.services import retention as retention_svc
from app.storage.memory import InMemoryBlobStore


async def _learner(session: AsyncSession, **kwargs: object) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", **kwargs)
    session.add(learner)
    await session.flush()
    return learner


async def test_one_provider_identity_belongs_to_one_learner(db_session: AsyncSession) -> None:
    """Two learners sharing a subject would mean one sign-in resolving to either of them."""
    await _learner(db_session, auth_subject="user_1")
    # Added but not flushed through the `_learner` helper: the helper's own flush would raise
    # here, outside the block below that is meant to catch it (the same reason
    # `test_one_note_per_learner_topic` in tests/test_notes_models.py adds its second row bare).
    db_session.add(Learner(handle=f"l-{uuid.uuid4().hex[:8]}", auth_subject="user_1"))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_address_can_have_only_one_open_invitation(db_session: AsyncSession) -> None:
    admin = await _learner(db_session)
    for _ in range(2):
        db_session.add(
            Invitation(
                email="invitee@example.com",
                invited_by_learner_id=admin.id,
                invited_by_handle=admin.handle,
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_spent_invitation_does_not_block_a_new_one(db_session: AsyncSession) -> None:
    """Re-inviting somebody whose invitation was accepted or withdrawn is ordinary."""
    admin = await _learner(db_session)
    learner = await _learner(db_session)
    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            accepted_at=datetime.now(UTC),
            accepted_learner_id=learner.id,
        )
    )
    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            revoked_at=datetime.now(UTC),
            revoked_by_learner_id=admin.id,
        )
    )
    await db_session.flush()

    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
        )
    )
    await db_session.flush()  # no error


async def test_the_audit_outlives_both_accounts(db_session: AsyncSession) -> None:
    """An audit a subject can erase by closing their account is not an audit of anything."""
    admin = await _learner(db_session)
    learner = await _learner(db_session)
    action = AccountAction(
        actor_learner_id=admin.id,
        actor_handle=admin.handle,
        learner_id=learner.id,
        learner_handle=learner.handle,
        action=AccountActionKind.SUSPEND,
        reason="abusing the tutor",
    )
    db_session.add(action)
    await db_session.flush()

    await db_session.delete(learner)
    await db_session.delete(admin)
    await db_session.flush()
    await db_session.refresh(action)

    assert action.learner_id is None and action.actor_learner_id is None
    assert action.actor_handle == admin.handle
    assert action.action == AccountActionKind.SUSPEND
    assert action.reason == "abusing the tutor"


async def test_closing_an_account_takes_its_invitation_and_unnames_it_in_the_audit(
    db_session: AsyncSession,
) -> None:
    """Their address is theirs: it goes with the account. Who acted, and that they acted, stays."""
    admin = await _learner(db_session)
    learner = await _learner(db_session, email="gone@example.com")
    db_session.add(
        Invitation(
            email="gone@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            accepted_at=datetime.now(UTC),
            accepted_learner_id=learner.id,
        )
    )
    action = AccountAction(
        actor_learner_id=admin.id,
        actor_handle=admin.handle,
        learner_id=learner.id,
        learner_handle=learner.handle,
        action=AccountActionKind.INVITE,
        email="gone@example.com",
    )
    db_session.add(action)
    await db_session.commit()

    await retention_svc.delete_learner(db_session, InMemoryBlobStore(), learner.id)

    await db_session.refresh(action)
    assert action.learner_handle is None and action.email is None
    assert action.actor_handle == admin.handle
    remaining = await db_session.scalar(
        Invitation.__table__.select().where(Invitation.email == "gone@example.com")
    )
    assert remaining is None


def test_the_new_stores_have_a_stated_disposition() -> None:
    named = set(retention_svc.retention_tables())

    assert {"invitations", "account_actions"} <= named
