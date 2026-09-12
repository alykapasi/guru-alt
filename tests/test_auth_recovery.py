"""A way back into an account, and a limit on guessing at one (S21).

Sign-in worked and nothing around it did. A forgotten password was an operator's problem, by
hand, in the database; a learner could not change their own credentials; and the endpoint that
spends an Argon2 hash per attempt would do so as many times as anybody asked — which makes the
defence against a stolen table into a way to spend our CPU at the attacker's convenience.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings
from app.core import security
from app.core.config import Settings, get_settings
from app.core.release import production_problems
from app.main import app
from app.models.auth import LearnerSession, PasswordResetToken, SignInAttempt
from app.models.learner import Learner
from app.services import auth as svc

API = "/api/v1"
PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "staple-correct-horse-9"


async def _registered(session: AsyncSession, email: str | None = None) -> Learner:
    return await svc.register(
        session, email=email or f"{uuid.uuid4().hex[:8]}@example.com", password=PASSWORD
    )


@pytest.fixture
def reset_enabled():
    """Password reset is off by default — the release gate refuses production without a real
    mail transport, so the flow has to be switched on deliberately to be exercised."""
    base = get_settings()
    enabled = base.model_copy(update={"password_reset_enabled": True})
    # The routes depend on ``get_app_settings``, not on ``get_settings`` directly — overriding
    # the wrong one of those is silent, and the request simply keeps the real settings.
    app.dependency_overrides[get_app_settings] = lambda: enabled
    yield enabled
    app.dependency_overrides.pop(get_app_settings, None)


# --- throttling -----------------------------------------------------------------------------


async def test_repeated_failures_against_one_address_are_refused(
    db_session: AsyncSession,
) -> None:
    learner = await _registered(db_session)
    assert learner.email is not None
    for _ in range(get_settings().sign_in_max_failures_per_email):
        await svc.record_failed_sign_in(db_session, email=learner.email, client="10.0.0.1")

    with pytest.raises(svc.TooManyAttempts):
        await svc.check_sign_in_allowed(
            db_session,
            email=learner.email,
            client="10.0.0.1",
            window=timedelta(minutes=15),
            max_per_email=get_settings().sign_in_max_failures_per_email,
            max_per_client=get_settings().sign_in_max_failures_per_client,
        )


async def test_stuffing_from_one_client_across_addresses_is_refused(
    db_session: AsyncSession,
) -> None:
    """The attack the per-address counter never sees: each address fails once or twice, so no
    address is ever near its own limit while the client works through a leaked list."""
    for _ in range(5):
        await svc.record_failed_sign_in(
            db_session, email=f"{uuid.uuid4().hex[:8]}@example.com", client="10.0.0.9"
        )

    with pytest.raises(svc.TooManyAttempts):
        await svc.check_sign_in_allowed(
            db_session,
            email="someone-new@example.com",
            client="10.0.0.9",
            window=timedelta(minutes=15),
            max_per_email=100,
            max_per_client=5,
        )


async def test_an_old_failure_stops_counting(db_session: AsyncSession) -> None:
    """The window is a window. A learner who mistyped last month is not under suspicion."""
    db_session.add(
        SignInAttempt(
            email="old@example.com",
            client="10.0.0.2",
            created_at=datetime.now(UTC) - timedelta(hours=2),
        )
    )
    await db_session.flush()

    await svc.check_sign_in_allowed(
        db_session,
        email="old@example.com",
        client="10.0.0.2",
        window=timedelta(minutes=15),
        max_per_email=1,
        max_per_client=1,
    )


async def test_a_successful_sign_in_records_nothing(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Only failures are written, so the common path costs no extra write."""
    learner = await _registered(db_session)
    await db_session.commit()

    r = await anon_client.post(
        f"{API}/auth/login", json={"email": learner.email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    assert (await db_session.scalars(select(SignInAttempt))).all() == []


async def test_the_endpoint_answers_429_once_the_limit_is_reached(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """429 rather than 401, because this one is worth distinguishing: it is the only way a
    person locked out by somebody else's guessing can tell what is happening to them."""
    learner = await _registered(db_session)
    assert learner.email is not None
    for _ in range(get_settings().sign_in_max_failures_per_email):
        await svc.record_failed_sign_in(db_session, email=learner.email, client="testclient")
    await db_session.commit()

    r = await anon_client.post(
        f"{API}/auth/login", json={"email": learner.email, "password": PASSWORD}
    )
    assert r.status_code == 429


async def test_purging_drops_attempts_past_the_window(db_session: AsyncSession) -> None:
    db_session.add(
        SignInAttempt(
            email="old@example.com",
            client="c",
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
    )
    db_session.add(SignInAttempt(email="fresh@example.com", client="c"))
    await db_session.commit()

    assert await svc.purge_sign_in_attempts(db_session, older_than=timedelta(hours=24)) == 1
    remaining = (await db_session.scalars(select(SignInAttempt))).all()
    assert [a.email for a in remaining] == ["fresh@example.com"]


# --- password reset -------------------------------------------------------------------------


async def test_a_reset_sets_the_password_and_ends_every_session(
    db_session: AsyncSession,
) -> None:
    """A reset is what somebody does when they think the account may not be theirs alone, so
    leaving the intruder's session working would make it a gesture."""
    learner = await _registered(db_session)
    assert learner.email is not None
    await svc.issue(db_session, learner, ttl=timedelta(hours=1))

    issued = await svc.begin_password_reset(
        db_session, email=learner.email, ttl=timedelta(minutes=30)
    )
    assert issued is not None
    assert await svc.complete_password_reset(db_session, token=issued.token, password=NEW_PASSWORD)

    await db_session.refresh(learner)
    assert security.verify_password(NEW_PASSWORD, learner.password_hash)
    live = (
        await db_session.scalars(
            select(LearnerSession).where(
                LearnerSession.learner_id == learner.id, LearnerSession.revoked_at.is_(None)
            )
        )
    ).all()
    assert live == []


async def test_a_reset_token_cannot_be_spent_twice(db_session: AsyncSession) -> None:
    learner = await _registered(db_session)
    assert learner.email is not None
    issued = await svc.begin_password_reset(
        db_session, email=learner.email, ttl=timedelta(minutes=30)
    )
    assert issued is not None
    assert await svc.complete_password_reset(db_session, token=issued.token, password=NEW_PASSWORD)

    assert (
        await svc.complete_password_reset(db_session, token=issued.token, password="another-one-99")
        is None
    )


async def test_an_expired_reset_token_does_nothing(db_session: AsyncSession) -> None:
    learner = await _registered(db_session)
    assert learner.email is not None
    issued = await svc.begin_password_reset(
        db_session, email=learner.email, ttl=timedelta(minutes=-1)
    )
    assert issued is not None

    assert (
        await svc.complete_password_reset(db_session, token=issued.token, password=NEW_PASSWORD)
        is None
    )


async def test_the_token_is_never_stored_in_full(db_session: AsyncSession) -> None:
    """A dump, a backup or a log line must not be a way in — the same rule as a session."""
    learner = await _registered(db_session)
    assert learner.email is not None
    issued = await svc.begin_password_reset(
        db_session, email=learner.email, ttl=timedelta(minutes=30)
    )
    assert issued is not None

    rows = (await db_session.scalars(select(PasswordResetToken))).all()
    assert [r.token_hash for r in rows] == [security.token_fingerprint(issued.token)]
    assert issued.token not in [r.token_hash for r in rows]


async def test_an_unknown_address_yields_no_reset_and_no_error(db_session: AsyncSession) -> None:
    assert (
        await svc.begin_password_reset(
            db_session, email="nobody@example.com", ttl=timedelta(minutes=30)
        )
        is None
    )


async def test_the_endpoint_answers_the_same_either_way(
    anon_client: AsyncClient, db_session: AsyncSession, reset_enabled: Settings
) -> None:
    """Whether an address has an account here is exactly what sign-in refuses to say; a reset
    endpoint that distinguishes hands the enumeration oracle back through another door."""
    learner = await _registered(db_session)
    await db_session.commit()

    known = await anon_client.post(f"{API}/auth/password-reset", json={"email": learner.email})
    unknown = await anon_client.post(
        f"{API}/auth/password-reset", json={"email": "nobody-at-all@example.com"}
    )
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


async def test_the_reset_endpoints_do_not_exist_unless_enabled(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    learner = await _registered(db_session)
    await db_session.commit()
    r = await anon_client.post(f"{API}/auth/password-reset", json={"email": learner.email})
    assert r.status_code == 404


async def test_production_refuses_reset_without_a_real_mail_transport() -> None:
    """A reset token written to the application log is not a delivered reset — and a reset
    nobody can deliver is worse than none, because it looks like one."""
    settings = get_settings().model_copy(update={"password_reset_enabled": True})
    problems = production_problems(settings)
    assert any("mail transport" in p for p in problems)


# --- changing your own credentials ----------------------------------------------------------


async def test_changing_a_password_requires_the_current_one(db_session: AsyncSession) -> None:
    """A session is not proof of the person: a borrowed laptop is a session."""
    learner = await _registered(db_session)

    with pytest.raises(svc.WrongPassword):
        await svc.change_password(db_session, learner, current="not-it-at-all", new=NEW_PASSWORD)
    await db_session.refresh(learner)
    assert security.verify_password(PASSWORD, learner.password_hash)


async def test_changing_a_password_ends_the_other_sessions_but_not_this_one(
    db_session: AsyncSession,
) -> None:
    """A password change is often a response to suspecting another device. Signing the learner
    out of the tab they are typing in would make the safe action the annoying one."""
    learner = await _registered(db_session)
    here = await svc.issue(db_session, learner, ttl=timedelta(hours=1))
    elsewhere = await svc.issue(db_session, learner, ttl=timedelta(hours=1))

    await svc.change_password(
        db_session, learner, current=PASSWORD, new=NEW_PASSWORD, keep_token=here.token
    )

    assert await svc.resolve(db_session, here.token) is not None
    assert await svc.resolve(db_session, elsewhere.token) is None


async def test_changing_an_email_requires_the_password(db_session: AsyncSession) -> None:
    learner = await _registered(db_session)
    with pytest.raises(svc.WrongPassword):
        await svc.change_email(
            db_session, learner, password="wrong-one-entirely", email="new@example.com"
        )


async def test_an_email_somebody_else_holds_is_refused(db_session: AsyncSession) -> None:
    taken = await _registered(db_session, email="taken@example.com")
    assert taken.email is not None
    learner = await _registered(db_session)

    with pytest.raises(svc.EmailTaken):
        await svc.change_email(db_session, learner, password=PASSWORD, email="taken@example.com")


async def test_changing_to_your_own_address_is_not_a_collision(db_session: AsyncSession) -> None:
    """Otherwise correcting the capitalisation of your own address locks you out of doing so."""
    learner = await _registered(db_session, email="me@example.com")
    await svc.change_email(db_session, learner, password=PASSWORD, email="ME@example.com")
    await db_session.refresh(learner)
    assert learner.email == "me@example.com"


async def test_a_failed_sign_in_through_the_endpoint_is_counted(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The throttle reads rows nothing else writes, so the endpoint recording them is the whole
    mechanism — a counter that is never incremented is an unthrottled endpoint with paperwork."""
    learner = await _registered(db_session)
    await db_session.commit()

    r = await anon_client.post(
        f"{API}/auth/login", json={"email": learner.email, "password": "not-the-password"}
    )
    assert r.status_code == 401

    recorded = (await db_session.scalars(select(SignInAttempt))).all()
    assert [a.email for a in recorded] == [learner.email]


async def test_a_failure_against_an_unregistered_address_is_counted_too(
    anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """These are the ones that most need counting: a stuffing run is mostly addresses that do
    not exist here, and skipping them would leave the client counter blind to it."""
    r = await anon_client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": "guessing"}
    )
    assert r.status_code == 401

    recorded = (await db_session.scalars(select(SignInAttempt))).all()
    assert [a.email for a in recorded] == ["nobody@example.com"]
