"""The author's side of publication: ask, look, withdraw (S25b).

Ownership is the existing gate, not a new one. Every route here resolves the subject through
`knowledge.require_visible_subject` and then `is_writable_by`, so a stranger's subject and an
id that names nothing produce the same 404 — which is the whole point of S25a's boundary and
would be undone by a route that answered 403 here instead.
"""

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentLearner, SessionDep
from app.models.publication import Publication
from app.schemas.publication import PublicationListRead, PublicationRead, PublicationRequest
from app.services import knowledge as knowledge_svc
from app.services import publication as svc

router = APIRouter(tags=["publications"])


async def _own_subject(session: SessionDep, subject_id: uuid.UUID, learner: CurrentLearner):
    """The caller's own subject, or the ordinary not-found answer.

    A curated subject is *visible* to everyone and writable by nobody, so it reaches the second
    branch: publishing the shared library back into itself is not a thing anyone may do.
    """
    subject = await knowledge_svc.require_visible_subject(session, subject_id, learner.id)
    if not knowledge_svc.is_writable_by(subject, learner.id):
        raise knowledge_svc.NotVisible("subject")
    return subject


@router.post(
    "/subjects/{subject_id}/publications",
    response_model=PublicationRead,
    status_code=status.HTTP_201_CREATED,
)
async def request_publication(
    subject_id: uuid.UUID,
    body: PublicationRequest,
    session: SessionDep,
    learner: CurrentLearner,
):
    """Freeze what would ship and put it in front of a reviewer."""
    subject = await _own_subject(session, subject_id, learner)
    try:
        return await svc.request_publication(session, subject, learner, body.note)
    except svc.AlreadyPending as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except svc.CannotPublish as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


@router.get("/subjects/{subject_id}/publications", response_model=PublicationListRead)
async def list_publications(subject_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Every request made for this subject, newest first, with the reviewer's note."""
    subject = await _own_subject(session, subject_id, learner)
    history = await svc.history_for(session, subject)
    return PublicationListRead(
        publications=[PublicationRead.model_validate(row) for row in history]
    )


@router.post("/publications/{publication_id}/cancel", response_model=PublicationRead)
async def cancel_publication(
    publication_id: uuid.UUID, session: SessionDep, learner: CurrentLearner
):
    """Withdraw a request before anybody has decided it."""
    publication = await session.get(Publication, publication_id)
    if publication is None or publication.author_id != learner.id:
        # Somebody else's request and one that does not exist answer identically: a caller who
        # could tell them apart could probe for other learners' publication activity.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such publication request")
    try:
        return await svc.cancel(session, publication)
    except svc.CannotPublish as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
