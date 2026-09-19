"""Alpha sudo writes are attributable and the originating admin must remain authorized."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_app_settings
from app.core.config import get_settings
from app.main import app
from app.models.learner import Learner
from tests.test_impersonation import REASON, _visit


@pytest.fixture(autouse=True)
def sudo_enabled():
    settings = get_settings().model_copy(update={"impersonation_enabled": True})
    app.dependency_overrides[get_app_settings] = lambda: settings
    yield
    app.dependency_overrides.pop(get_app_settings, None)


async def test_sudo_can_write_and_records_operator_target_reason_and_result(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_learner: Learner,
) -> None:
    admin_id = (await admin_client.get("/api/v1/auth/me")).json()["id"]
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    result = await anon_client.post("/api/v1/conversations", json={})
    assert result.status_code == 201, result.text
    log = await admin_client.get(
        f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
    )
    assert log.status_code == 200
    action = log.json()[0]
    assert action["method"] == "POST"
    assert action["route"] == "/api/v1/conversations"
    assert action["status_code"] == 201
    assert action["completed_at"] is not None
    exported = await anon_client.get("/api/v1/me/export")
    assert exported.status_code == 200
    assert any(row["id"] == action["id"] for row in exported.json()["admin_actions"])
    assert visit["impersonation"]["admin_learner_id"] == admin_id
    assert visit["impersonation"]["learner_id"] == str(api_learner.id)
    assert visit["impersonation"]["reason"] == REASON
    assert (
        await anon_client.get(
            f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
        )
    ).status_code == 403


async def test_failed_sudo_request_keeps_a_durable_audit(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_learner: Learner,
) -> None:
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    result = await anon_client.post("/api/v1/sources/link", json={"url": "https://example.com/"})
    assert result.status_code == 403
    log = await admin_client.get(
        f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
    )
    assert log.status_code == 200
    assert log.json()[0]["status_code"] == 403


@pytest.mark.parametrize("withdrawal", ["demotion", "deletion", "disabled"])
async def test_sudo_stops_when_its_authority_is_withdrawn(
    withdrawal: str,
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_learner: Learner,
    db_session: AsyncSession,
) -> None:
    admin_id = uuid.UUID((await admin_client.get("/api/v1/auth/me")).json()["id"])
    _, visit = await _visit(admin_client, api_learner)
    if withdrawal == "disabled":
        settings = get_settings().model_copy(update={"impersonation_enabled": False})
        app.dependency_overrides[get_app_settings] = lambda: settings
    else:
        admin = await db_session.get(Learner, admin_id)
        assert admin is not None
        if withdrawal == "deletion":
            await db_session.delete(admin)
        else:
            admin.is_admin = False
        await db_session.commit()
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    assert (await anon_client.get("/api/v1/conversations")).status_code == 401


@pytest.mark.parametrize("outcome", ["rollback", "server_error", "stream", "interrupted_stream"])
async def test_audit_survives_rollback_and_finishes_response_stream(
    outcome: str,
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_learner: Learner,
) -> None:
    from fastapi import HTTPException
    from starlette.responses import StreamingResponse

    from app.api.deps import CurrentLearner, SessionDep

    async def endpoint(learner: CurrentLearner, session: SessionDep):
        if outcome in {"stream", "interrupted_stream"}:

            async def chunks():
                yield b"first"
                if outcome == "interrupted_stream":
                    raise RuntimeError("deliberate offline failure")
                yield b"last"

            return StreamingResponse(chunks())
        await session.rollback()
        if outcome == "server_error":
            raise RuntimeError("deliberate offline failure")
        raise HTTPException(409, "deliberate rollback")

    path = f"/api/v1/test-audit-{outcome}"
    app.add_api_route(path, endpoint, methods=["POST"])
    route = app.router.routes[-1]
    try:
        _, visit = await _visit(admin_client, api_learner)
        anon_client.headers["authorization"] = f"Bearer {visit['token']}"
        if outcome in {"server_error", "interrupted_stream"}:
            with pytest.raises(RuntimeError, match="deliberate offline failure"):
                await anon_client.post(path)
        else:
            response = await anon_client.post(path)
            assert response.status_code == (200 if outcome == "stream" else 409)
            if outcome == "stream":
                assert response.content == b"firstlast"
        response = await admin_client.get(
            f"/api/v1/admin/impersonations/{visit['impersonation']['id']}/actions"
        )
        (action,) = response.json()
        if outcome == "interrupted_stream":
            assert action["status_code"] is None
            assert action["completed_at"] is None
            return
        assert (
            action["status_code"] == {"stream": 200, "rollback": 409, "server_error": 500}[outcome]
        )
        assert action["completed_at"] is not None
    finally:
        app.router.routes.remove(route)


async def test_normal_learner_request_clears_admin_learning_metadata(
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_client: AsyncClient,
    api_learner: Learner,
    db_session: AsyncSession,
) -> None:
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    assert (await anon_client.get("/api/v1/conversations")).status_code == 200
    assert db_session.info.get("admin_actor_id") is not None
    assert (await api_client.get("/api/v1/conversations")).status_code == 200
    assert "admin_actor_id" not in db_session.info
    assert "admin_action_id" not in db_session.info


async def test_learner_cannot_reuse_an_admin_attempt_as_personal_evidence(
    api_client: AsyncClient,
    admin_client: AsyncClient,
    anon_client: AsyncClient,
    api_learner: Learner,
    db_session: AsyncSession,
) -> None:
    from sqlalchemy import select

    from app.models.learning import LearnerKCState, LearningEvent
    from tests.test_assessment import _mcq_body, _seed_kcs

    (kc,) = await _seed_kcs(db_session)
    item_id = (await api_client.post("/api/v1/items", json=_mcq_body(kc.id))).json()["id"]
    _, visit = await _visit(admin_client, api_learner)
    anon_client.headers["authorization"] = f"Bearer {visit['token']}"
    attempt = {"response": {"choice": 1}, "attempt_id": str(uuid.uuid4())}
    assert (
        await anon_client.post(f"/api/v1/items/{item_id}/answer", json=attempt)
    ).status_code == 200
    rejected = await api_client.post(f"/api/v1/items/{item_id}/answer", json=attempt)
    assert rejected.status_code == 422
    assert "another actor" in rejected.text
    assert (
        await db_session.scalar(
            select(LearnerKCState).where(LearnerKCState.learner_id == api_learner.id)
        )
        is None
    )
    attempt["attempt_id"] = str(uuid.uuid4())
    assert (
        await api_client.post(f"/api/v1/items/{item_id}/answer", json=attempt)
    ).status_code == 200
    events = list(
        await db_session.scalars(select(LearningEvent).where(LearningEvent.kc_id == kc.id))
    )
    assert {event.event_type for event in events} == {"admin_observation", "observation"}
