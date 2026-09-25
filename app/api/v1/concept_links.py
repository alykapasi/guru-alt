"""A learner's side of concept links (S24): what is suggested, and their decision on it."""

import dataclasses
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, SessionDep
from app.schemas.concept_links import ConceptLinkDecisionSubmit, ConceptLinkSuggestionRead
from app.services import concept_links as svc
from app.services import lesson_plan as lesson_plan_svc

router = APIRouter(tags=["concept-links"])


@router.get("/concept-links/suggestions", response_model=list[ConceptLinkSuggestionRead])
async def list_suggestions(session: SessionDep, learner: CurrentLearner):
    found = await svc.suggestions(session, learner.id)
    await session.commit()  # sync_candidates may have recorded new pairs
    return found


@router.post("/concept-links/{link_id}/decision", response_model=ConceptLinkSuggestionRead)
async def decide_link(
    link_id: uuid.UUID,
    data: ConceptLinkDecisionSubmit,
    session: SessionDep,
    learner: CurrentLearner,
):
    """404 for a link that is not endorsed or not the caller's to see; 409 for a decision that
    does not follow from the last one (accepting a declined link, revoking an undecided one)."""
    # Read before deciding: a decline takes the link out of the suggestions for good, and the
    # response still has to describe it. `None` here is the same 404 as `LinkNotFound`.
    before = next(
        (s for s in await svc.suggestions(session, learner.id) if s.link_id == link_id), None
    )
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link")
    try:
        row = await svc.decide(session, learner.id, link_id, data.decision)
    except svc.LinkNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link") from exc
    except svc.LinkConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    # A head start given or withdrawn changes what those plans should show (check-first,
    # external steps), so they are brought up to date now rather than on the next answer.
    for subject_id in {before.a.subject_id, before.b.subject_id}:
        await lesson_plan_svc.revise_plan(session, learner_id=learner.id, subject_id=subject_id)
    return dataclasses.replace(before, decision="accepted" if row.decision == "accepted" else None)
