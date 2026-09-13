"""Content engine endpoints: generate/assemble blocks for a KC and read cached ones."""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.models.content import ContentType
from app.schemas.content import ContentBlockRead, GenerateRequest, SupportReportRead
from app.services import content as svc

router = APIRouter(prefix="/content", tags=["content"])


@router.post("/generate", response_model=list[ContentBlockRead])
async def generate_content(
    data: GenerateRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    """Generate a block of the given ``type`` (or assemble the default set) for a KC.

    Cache-aware: an identical request reuses stored blocks rather than regenerating.
    """
    if data.type is not None:
        block = await svc.generate_block(
            session, llm, learner_id=learner.id, kc_id=data.kc_id, block_type=data.type
        )
        return [block]
    return await svc.assemble(session, llm, learner_id=learner.id, kc_id=data.kc_id)


@router.post("/blocks/{block_id}/citation-check", response_model=SupportReportRead)
async def check_block_citations(
    block_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    """Check whether the block's claims are carried by the passages it cited (S28).

    A paid model call, so it is a POST a caller asks for rather than something generation does
    for every block. Nothing gates on the result: what rate of unsupported claims is tolerable
    is a threshold nobody has set.
    """
    try:
        return await svc.check_block_citations(
            session, llm, learner_id=learner.id, block_id=block_id
        )
    except LookupError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err


@router.get("/kc/{kc_id}", response_model=list[ContentBlockRead])
async def get_kc_content(
    kc_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    block_type: Annotated[ContentType | None, Query(alias="type")] = None,
):
    """Read the cached content blocks for a KC, scoped to the learner (no generation)."""
    return await svc.list_blocks(session, learner_id=learner.id, kc_id=kc_id, block_type=block_type)
