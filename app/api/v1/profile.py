"""The learner profile: view the current snapshot, refresh it, reset a dimension."""

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.schemas.profile import DimensionRead, ProfileSnapshotRead
from app.services import profile as svc

router = APIRouter(tags=["profile"])


@router.get("/profile", response_model=ProfileSnapshotRead)
async def get_profile(session: SessionDep, learner: CurrentLearner):
    dims = await svc.get_snapshot(session, learner.id)
    return ProfileSnapshotRead(dimensions=[DimensionRead.model_validate(d) for d in dims])


@router.post("/profile/refresh", response_model=ProfileSnapshotRead)
async def refresh_profile(session: SessionDep, learner: CurrentLearner, llm: LLMClientDep):
    dims = await svc.refresh_profile(session, learner.id, llm)
    return ProfileSnapshotRead(dimensions=[DimensionRead.model_validate(d) for d in dims])


@router.post("/profile/{key}/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_dimension(key: str, session: SessionDep, learner: CurrentLearner):
    try:
        await svc.reset_dimension(session, learner.id, key)
    except KeyError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown profile dimension: {key}") from err
