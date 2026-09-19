"""An administrator viewing a learner's account: what it permits, refuses, and records (P10).

The dangerous parts of impersonation are not the feature, they are the three ways it goes
wrong: acting as somebody in their own record, becoming an administrator through somebody
else's session, and a visit nobody can see afterwards. Each of those has tests here rather
than a paragraph in a design document.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings
from app.core.config import get_settings
from app.main import app
from app.models.auth import Impersonation, LearnerSession
from app.models.learner import Learner
from app.services import auth, impersonation
from tests.conftest import sign_in

API = "/api/v1"
REASON = "learner reports their upload never became a lesson"


@pytest.fixture(autouse=True)
def _impersonation_on():
    """The capability is off by default; these tests are about it being on."""
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    yield
    app.dependency_overrides.pop(get_app_settings, None)


async def _learner(session: AsyncSession, handle: str, *, admin: bool = False) -> Learner:
    row = Learner(handle=f"{handle}-{uuid.uuid4().hex[:6]}", is_admin=admin)
    session.add(row)
    await session.flush()
    return row


async def _visit(
    admin_client: AsyncClient, learner: Learner, *, reason: str = REASON
) -> tuple[int, dict]:
    r = await admin_client.post(
        f"{API}/admin/impersonate", json={"learner_id": str(learner.id), "reason": reason}
    )
    return r.status_code, (r.json() if r.content else {})


# --- the credential --------------------------------------------------------------------------


async def test_a_visit_authenticates_as_the_learner(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject = await _learner(db_session, "subject")

    status, body = await _visit(admin_client, subject)
    assert status == 200, body

    anon_client.headers["authorization"] = f"Bearer {body['token']}"
    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == str(subject.id)


async def test_the_administrators_own_session_is_untouched(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A second credential, not a transformed first one — so ending a visit cannot sign them
    out, and losing its token costs them nothing."""
    subject = await _learner(db_session, "subject")
    before = (await admin_client.get(f"{API}/auth/me")).json()["id"]

    await _visit(admin_client, subject)

    assert (await admin_client.get(f"{API}/auth/me")).json()["id"] == before
    assert (await admin_client.get(f"{API}/ops/spend")).status_code == 200


# --- the scope -------------------------------------------------------------------------------


async def test_a_visit_can_write_with_audit(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The point of the whole design. Writing as somebody else puts evidence in their record
    that they did not create, and no audit makes that recoverable — their own history becomes
    something they cannot trust."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    anon_client.headers["authorization"] = f"Bearer {body['token']}"

    assert (await anon_client.get(f"{API}/conversations")).status_code == 200
    r = await anon_client.post(f"{API}/conversations", json={})
    assert r.status_code == 201


async def test_a_visit_can_sign_the_learner_out_of_other_devices(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`logout-all` is an action on the account rather than a view of it."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    anon_client.headers["authorization"] = f"Bearer {body['token']}"

    assert (await anon_client.post(f"{API}/auth/logout-all")).status_code == 204


async def test_a_visit_is_never_an_administrator(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Even onto an administrator's account — otherwise impersonating one is a way for an
    administrator to launder their own actions through another's name."""
    other_admin = await _learner(db_session, "other-admin", admin=True)
    _, body = await _visit(admin_client, other_admin)
    anon_client.headers["authorization"] = f"Bearer {body['token']}"

    assert (await anon_client.get(f"{API}/auth/me")).json()["is_admin"] is True
    assert (await anon_client.get(f"{API}/admin/learners")).status_code == 403
    assert (await anon_client.get(f"{API}/ops/spend")).status_code == 403
    assert (await anon_client.post(f"{API}/admin/impersonate", json={})).status_code == 403


async def test_a_visit_expires_on_its_own_clock(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)

    row = await db_session.scalar(
        select(LearnerSession).where(LearnerSession.impersonated_by_id.is_not(None))
    )
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    anon_client.headers["authorization"] = f"Bearer {body['token']}"
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


# --- the audit -------------------------------------------------------------------------------


async def test_the_record_is_written_with_the_credential_not_after_it(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """One transaction, so there is no path that grants access and then fails to log it."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)

    record = body["impersonation"]
    assert record["learner_id"] == str(subject.id)
    assert record["reason"] == REASON
    assert record["ended_at"] is None
    assert record["admin_handle"]


async def test_a_thin_reason_is_refused_before_the_request_reaches_the_handler(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two layers refuse this and each would mask the other's absence, so the assertion is on
    *which* one answered. A schema refusal is FastAPI's, and its `detail` is a list of field
    errors; the handler's is a string. Asserting only the 422 would let the schema floor be
    deleted without a single test noticing, because the service would answer 422 in its place.
    """
    subject = await _learner(db_session, "subject")
    r = await admin_client.post(
        f"{API}/admin/impersonate", json={"learner_id": str(subject.id), "reason": "support"}
    )

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list), "the handler answered; validation did not"

    count = await db_session.scalar(select(Impersonation.id).where(Impersonation.reason == ""))
    assert count is None


async def test_the_service_refuses_a_thin_reason_as_well(db_session: AsyncSession) -> None:
    """The schema guards the HTTP door; this guards the function. They are not the same door —
    anything that is not a request (a script, a future endpoint, a test) reaches only this one.
    """
    admin = await _learner(db_session, "admin", admin=True)
    subject = await _learner(db_session, "subject")

    with pytest.raises(impersonation.ReasonRequired):
        await impersonation.begin(
            db_session,
            admin=admin,
            learner_id=subject.id,
            reason="   help   ",
            ttl=timedelta(minutes=15),
        )

    assert await db_session.scalar(select(Impersonation.id)) is None


async def test_signing_the_visit_out_ends_the_record(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Through the path that already exists, so an end does not depend on the polite endpoint
    being the one somebody happened to call."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    anon_client.headers["authorization"] = f"Bearer {body['token']}"

    assert (await anon_client.post(f"{API}/auth/logout")).status_code == 204

    db_session.expire_all()
    record = await db_session.get(Impersonation, uuid.UUID(body["impersonation"]["id"]))
    assert record is not None and record.ended_at is not None
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_ending_it_as_the_administrator_leaves_their_own_session_alone(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The browser path. `/auth/logout` clears the session cookie on its way out, and the
    cookie in that browser is the administrator's own — ending a visit that way would sign
    them out of their account."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    record_id = body["impersonation"]["id"]

    r = await admin_client.delete(f"{API}/admin/impersonations/{record_id}")
    assert r.status_code == 200
    assert r.json()["ended_at"] is not None

    # Still signed in, still an administrator.
    assert (await admin_client.get(f"{API}/admin/learners")).status_code == 200

    anon_client.headers["authorization"] = f"Bearer {body['token']}"
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_a_visit_cannot_end_itself_through_the_administrator_route(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """It is not an administrator, whatever it is viewing — so it uses logout, like anyone."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    anon_client.headers["authorization"] = f"Bearer {body['token']}"

    r = await anon_client.delete(f"{API}/admin/impersonations/{body['impersonation']['id']}")
    assert r.status_code == 403


async def test_ending_an_unknown_visit_is_a_404(admin_client: AsyncClient) -> None:
    r = await admin_client.delete(f"{API}/admin/impersonations/{uuid.uuid4()}")
    assert r.status_code == 404


async def test_a_visit_can_be_ended_after_it_has_already_expired(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The record is about the visit, not about its credential. Refusing to close a row whose
    session had lapsed would leave the log permanently showing an open visit."""
    subject = await _learner(db_session, "subject")
    _, body = await _visit(admin_client, subject)
    record_id = uuid.UUID(body["impersonation"]["id"])

    row = await db_session.get(Impersonation, record_id)
    assert row is not None
    session_row = await db_session.get(LearnerSession, row.session_id)
    assert session_row is not None
    session_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.flush()

    r = await admin_client.delete(f"{API}/admin/impersonations/{record_id}")
    assert r.status_code == 200 and r.json()["ended_at"] is not None


async def test_the_log_lists_what_happened(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject = await _learner(db_session, "subject")
    await _visit(admin_client, subject)

    r = await admin_client.get(f"{API}/admin/impersonations")
    assert r.status_code == 200
    row = next(x for x in r.json() if x["learner_id"] == str(subject.id))
    assert row["reason"] == REASON


async def test_the_learner_can_see_who_looked_at_their_account(
    admin_client: AsyncClient, anon_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A record of access the person accessed cannot see is a record kept for somebody else."""
    subject = await _learner(db_session, "subject")
    await _visit(admin_client, subject)

    await sign_in(anon_client, db_session, subject)
    export = (await anon_client.get(f"{API}/me/export")).json()

    assert [row["reason"] for row in export["impersonations"]] == [REASON]


# --- the seam --------------------------------------------------------------------------------


async def test_the_capability_is_absent_rather_than_forbidden_when_switched_off(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """404, not 403: a capability a deployment has not enabled should not announce itself."""
    app.dependency_overrides[get_app_settings] = lambda: get_settings().model_copy(
        update={"impersonation_enabled": False}
    )
    subject = await _learner(db_session, "subject")
    status, _ = await _visit(admin_client, subject)
    assert status == 404


async def test_switching_it_off_does_not_hide_what_was_done_while_it_was_on(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Otherwise the switch is a way to erase the record rather than to withdraw the power."""
    subject = await _learner(db_session, "subject")
    await _visit(admin_client, subject)

    app.dependency_overrides[get_app_settings] = lambda: get_settings().model_copy(
        update={"impersonation_enabled": False}
    )
    r = await admin_client.get(f"{API}/admin/impersonations")
    assert r.status_code == 200
    assert any(x["learner_id"] == str(subject.id) for x in r.json())


async def test_an_ordinary_learner_cannot_start_one(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    subject = await _learner(db_session, "subject")
    r = await api_client.post(
        f"{API}/admin/impersonate", json={"learner_id": str(subject.id), "reason": REASON}
    )
    assert r.status_code == 403


async def test_impersonating_yourself_is_refused(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    """You already have the account; recording it as a visit would only muddy the log."""
    me = (await admin_client.get(f"{API}/auth/me")).json()
    r = await admin_client.post(
        f"{API}/admin/impersonate", json={"learner_id": me["id"], "reason": REASON}
    )
    assert r.status_code == 400


# --- the record outliving the accounts --------------------------------------------------------


async def test_closing_the_learners_account_clears_them_from_the_record_but_keeps_it(
    db_session: AsyncSession,
) -> None:
    """A record of access that the subject of it can erase is not an audit of access."""
    from app.services import retention
    from app.storage.memory import InMemoryBlobStore

    admin = await _learner(db_session, "admin", admin=True)
    subject = await _learner(db_session, "subject")
    began = await impersonation.begin(
        db_session, admin=admin, learner_id=subject.id, reason=REASON, ttl=timedelta(minutes=15)
    )
    record_id = began.impersonation.id
    admin_handle = admin.handle

    await retention.delete_learner(db_session, InMemoryBlobStore(), subject.id)

    # The foreign key clears the column in the database; this session is still holding the
    # object it loaded before that happened.
    db_session.expire_all()
    record = await db_session.get(Impersonation, record_id)
    assert record is not None, "the audit row went with the account"
    assert record.learner_id is None and record.learner_handle is None
    assert record.admin_handle == admin_handle
    assert record.reason == REASON


async def test_the_session_it_issued_is_marked_as_one(db_session: AsyncSession) -> None:
    """What the request path reads. Every authenticated request loads this row already, so the
    alternative is a join on the hot path to answer a question that is NULL almost always."""
    admin = await _learner(db_session, "admin", admin=True)
    subject = await _learner(db_session, "subject")
    began = await impersonation.begin(
        db_session, admin=admin, learner_id=subject.id, reason=REASON, ttl=timedelta(minutes=15)
    )

    resolved = await auth.resolve_session(db_session, began.token, impersonation_enabled=True)
    assert resolved is not None
    assert resolved.learner.id == subject.id
    assert resolved.impersonated_by_id == admin.id


async def test_an_ordinary_session_is_not_marked(
    db_session: AsyncSession, api_learner: Learner
) -> None:
    issued = await auth.issue(db_session, api_learner, ttl=timedelta(hours=1), commit=False)
    resolved = await auth.resolve_session(db_session, issued.token)
    assert resolved is not None and resolved.impersonated_by_id is None
