"""The review tab's routes: administrator only, and blind until labelled (S59)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import IntentRecord
from app.models.learner import Learner

API = "/api/v1/admin/eval"


async def _intent(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", eval_eligible=True)
    session.add(learner)
    await session.flush()
    session.add(
        IntentRecord(
            learner_id=learner.id, stem="What is a basis?", message="Hm?", intent="deferral"
        )
    )
    await session.flush()
    return learner


async def test_counts_cover_every_area(admin_client: AsyncClient, db_session: AsyncSession) -> None:
    await _intent(db_session)
    r = await admin_client.get(f"{API}/areas")
    assert r.status_code == 200
    by_area = {a["area"]: a for a in r.json()}
    assert set(by_area) == {"grading", "diagnosis", "intent", "source_support"}
    assert by_area["intent"]["available"] == 1


async def test_an_example_is_served_blind_and_revealed_once_labelled(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _intent(db_session)
    drawn = await admin_client.post(f"{API}/intent/draw", json={})
    assert drawn.status_code == 200 and drawn.json() == {"drawn": 1}

    served = await admin_client.get(f"{API}/intent/next")
    assert served.status_code == 200
    body = served.json()
    assert "decision" not in body
    assert body["snapshot"] == {"stem": "What is a basis?", "message": "Hm?"}

    labelled = await admin_client.put(
        f"{API}/examples/{body['id']}/label",
        json={"label": {"intent": "attempt"}, "note": "it was a try"},
    )
    assert labelled.status_code == 200
    assert labelled.json()["decision"] == {"intent": "deferral"}
    assert labelled.json()["label"] == {"intent": "attempt"}

    listed = await admin_client.get(f"{API}/intent/labelled")
    assert [e["id"] for e in listed.json()] == [body["id"]]
    assert (await admin_client.get(f"{API}/intent/next")).json() is None


async def test_skipping_moves_past_an_example(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _intent(db_session)
    await _intent(db_session)
    await admin_client.post(f"{API}/intent/draw", json={})
    first = (await admin_client.get(f"{API}/intent/next")).json()
    second = (await admin_client.get(f"{API}/intent/next", params={"skip": first["id"]})).json()
    assert second is not None and second["id"] != first["id"]
    assert "decision" not in second


async def test_a_label_outside_the_vocabulary_is_422(
    admin_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _intent(db_session)
    await admin_client.post(f"{API}/intent/draw", json={})
    example = (await admin_client.get(f"{API}/intent/next")).json()
    r = await admin_client.put(
        f"{API}/examples/{example['id']}/label", json={"label": {"intent": "shrug"}}
    )
    assert r.status_code == 422


async def test_an_unknown_area_is_404(admin_client: AsyncClient) -> None:
    assert (await admin_client.get(f"{API}/vibes/next")).status_code == 404
    assert (await admin_client.post(f"{API}/vibes/draw", json={})).status_code == 404


async def test_an_unknown_example_is_404(admin_client: AsyncClient) -> None:
    r = await admin_client.put(
        f"{API}/examples/{uuid.uuid4()}/label", json={"label": {"intent": "attempt"}}
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/areas"),
        ("post", "/intent/draw"),
        ("get", "/intent/next"),
        ("get", "/intent/labelled"),
    ],
)
async def test_an_ordinary_learner_is_refused(
    api_client: AsyncClient, method: str, path: str
) -> None:
    r = await getattr(api_client, method)(
        f"{API}{path}", **({"json": {}} if method == "post" else {})
    )
    assert r.status_code == 403


async def test_an_anonymous_request_is_refused(anon_client: AsyncClient) -> None:
    assert (await anon_client.get(f"{API}/areas")).status_code == 401
