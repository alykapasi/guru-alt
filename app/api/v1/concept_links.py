"""A learner's side of concept links (S24): what is suggested, and their decision on it."""

import uuid

import structlog
from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentLearner, SessionDep
from app.schemas.concept_links import ConceptLinkDecisionSubmit, ConceptLinkSuggestionRead
from app.services import concept_links as svc
from app.services import lesson_plan as lesson_plan_svc

log = structlog.get_logger(__name__)

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
    # `describe`, not `suggestions`: `suggestions` is the *menu* and drops a link the learner
    # already declined (it is not offered again), so using it here as the existence probe made
    # every decision *after* a decline — a repeat decline, or the 409 for accepting it — 404
    # instead. `describe` answers for any link `decide` itself would recognise.
    try:
        before = await svc.describe(session, learner.id, link_id)
    except svc.LinkNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link") from exc
    try:
        await svc.decide(session, learner.id, link_id, data.decision)
    except svc.LinkNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such link") from exc
    except svc.LinkConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await session.commit()
    # A head start given or withdrawn changes what those plans should show (check-first,
    # external steps), so they are brought up to date now rather than on the next answer.
    # Best-effort, like `assessment._revise_plans`: the decision is already durable, so a
    # revision failure here must not turn a successful decision into a 500.
    await _revise_plans(
        session, learner_id=learner.id, subject_ids={before.a.subject_id, before.b.subject_id}
    )
    return await svc.describe(session, learner.id, link_id)


async def _revise_plans(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_ids: set[uuid.UUID]
) -> None:
    """Bring any plan on these subjects back in line with the decision just committed.

    Mirrors ``app.services.assessment._revise_plans``: on failure, rolls back whatever partial
    revision the session was left holding (a half-applied step list must not be committed along
    with the flag below) and flags every affected plan so the next read repairs it, rather than
    reporting the already-committed decision back to the caller as failed.
    """
    try:
        for subject_id in subject_ids:
            await lesson_plan_svc.revise_plan(session, learner_id=learner_id, subject_id=subject_id)
        return
    except Exception:
        log.exception("concept_links.plan_revision_failed", learner_id=str(learner_id))
    try:
        await session.rollback()
        for subject_id in subject_ids:
            await lesson_plan_svc.mark_revision_pending(
                session, learner_id=learner_id, subject_id=subject_id
            )
    except Exception:
        log.exception("concept_links.plan_repair_flag_failed", learner_id=str(learner_id))
        await session.rollback()
