"""Shared FastAPI dependencies.

``get_current_learner`` is the auth seam (S21). It used to resolve — and lazily *create* — a
single dev learner, which meant every route had a learner whether or not the caller had
proved anything. It now resolves a session token to the learner who owns it, and refuses the
request when there is none. Nothing above it changed: every route already receives a
``Learner`` / ``learner_id``, which is what made this a swap rather than a rewire.
"""

import hmac
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import engine, get_session
from app.llm import LLMClient, build_llm_client
from app.models.learner import Learner
from app.services import auth
from app.storage import BlobStore, build_blob_store

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_engine() -> AsyncEngine:
    """The engine a request may open its *own* connection on.

    Almost everything should use ``SessionDep`` and stay inside the request's transaction.
    The exception is the turn lock (S17), which holds a Postgres advisory lock on a connection
    of its own for the length of a streamed turn — it cannot share the request session, whose
    connection is returned to the pool at every commit. Injected rather than imported so a test
    can point it at the same engine the test itself is using; an advisory lock taken on a
    different engine is a lock on a different backend, which is no lock at all.
    """
    return engine


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]


def get_app_settings() -> Settings:
    """The process-wide settings. Overridden in tests (e.g. to shrink the upload cap)."""
    return get_settings()


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@lru_cache
def _llm_client() -> LLMClient:
    return build_llm_client(get_settings())


def get_llm_client() -> LLMClient:
    """The process-wide LLM registry. Overridden in tests with a FakeProvider client."""
    return _llm_client()


LLMClientDep = Annotated[LLMClient, Depends(get_llm_client)]


@lru_cache
def _blob_store() -> BlobStore:
    return build_blob_store(get_settings())


def get_blob_store() -> BlobStore:
    """The process-wide object store. Overridden in tests with an in-memory store."""
    return _blob_store()


BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]

IngestionEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def _enqueue_ingestion(source_id: uuid.UUID) -> None:
    from app.workers.tasks import ingest_source_task  # lazy: avoids an import cycle

    await ingest_source_task.kiq(str(source_id))


def get_ingestion_enqueuer() -> IngestionEnqueuer:
    """Returns the callable that queues an ingestion job. Overridden in tests."""
    return _enqueue_ingestion


IngestionEnqueuerDep = Annotated[IngestionEnqueuer, Depends(get_ingestion_enqueuer)]

RetagEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def _enqueue_retag(source_id: uuid.UUID) -> None:
    from app.workers.tasks import retag_source_task  # lazy: avoids an import cycle

    await retag_source_task.kiq(str(source_id))


def get_retag_enqueuer() -> RetagEnqueuer:
    """Returns the callable that queues a KC-retag job. Overridden in tests."""
    return _enqueue_retag


RetagEnqueuerDep = Annotated[RetagEnqueuer, Depends(get_retag_enqueuer)]

MemoryWriteBackEnqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def _enqueue_memory_write_back(conversation_id: uuid.UUID) -> None:
    from app.workers.tasks import memory_write_back_task  # lazy: avoids an import cycle

    await memory_write_back_task.kiq(str(conversation_id))


def get_memory_write_back_enqueuer() -> MemoryWriteBackEnqueuer:
    """Returns the callable that queues a memory write-back job. Overridden in tests."""
    return _enqueue_memory_write_back


MemoryWriteBackEnqueuerDep = Annotated[
    MemoryWriteBackEnqueuer, Depends(get_memory_write_back_enqueuer)
]


_BEARER_PREFIX = "bearer "


def session_token_from(request: Request, settings: Settings) -> str | None:
    """The session token this request carries, from either accepted place.

    An explicit ``Authorization: Bearer`` header wins over the cookie when both are present,
    because sending it is a deliberate act by a non-browser client, while the cookie is
    attached by the browser to whatever it is pointed at.
    """
    header = request.headers.get("authorization", "")
    if header[: len(_BEARER_PREFIX)].lower() == _BEARER_PREFIX:
        token = header[len(_BEARER_PREFIX) :].strip()
        if token:
            return token
    return request.cookies.get(settings.session_cookie_name) or None


# The methods an impersonated session may use (P10). Everything else is refused, at this one
# dependency rather than route by route: a rule each new endpoint has to remember is a rule
# that is eventually not remembered, and the endpoint that forgets it is the one that writes
# something into a learner's record which the learner did not do.
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def get_authenticated(
    request: Request, session: SessionDep, settings: SettingsDep
) -> auth.Authenticated:
    """Who this request is, and on whose behalf, or 401.

    Every reason for failing — no token, an unknown one, an expired one, a revoked one, one
    belonging to a deleted account — produces the same response, because the caller can do the
    same one thing about all of them, and telling them apart is free reconnaissance.
    """
    token = session_token_from(request, settings)
    resolved = await auth.resolve_session(session, token) if token else None
    if resolved is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return resolved


Authenticated = Annotated[auth.Authenticated, Depends(get_authenticated)]


async def get_current_learner(request: Request, who: Authenticated) -> Learner:
    """The learner this request acts as, refusing a write made on somebody else's behalf (P10).

    `/auth/logout` deliberately does not depend on this, which is what lets an administrator
    end a visit through the path that already exists rather than through an exception carved
    into the rule. `/auth/logout-all` does depend on it, and so an impersonated session cannot
    sign the learner out of their other devices — which is correct: that is an action on their
    account, not a view of it.
    """
    if who.impersonated_by_id is not None and request.method not in _READ_ONLY_METHODS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "this session is viewing an account, not acting on it",
        )
    return who.learner


CurrentLearner = Annotated[Learner, Depends(get_current_learner)]


async def get_current_admin(who: Authenticated) -> Learner:
    """The learner this request is authenticated as, if they administer this deployment (P10).

    403 rather than 401, and rather than the 404 that would hide the route: the caller has
    already proved who they are, so there is nothing left to conceal from them and nothing
    they could usefully retry with a different credential. Telling them plainly that this is
    not theirs is the answer that does not send somebody hunting for a broken session.

    An impersonated session is refused whatever the *target* learner's own flag says. Otherwise
    impersonating an administrator is a way for one administrator to launder their actions
    through another's name, and the audit would record the visit while recording none of what
    it was used for.
    """
    if who.impersonated_by_id is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an administrator")
    if not who.learner.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an administrator")
    return who.learner


CurrentAdmin = Annotated[Learner, Depends(get_current_admin)]


async def require_operator(request: Request, session: SessionDep, settings: SettingsDep) -> None:
    """Admit an administrator's session, or a monitor holding the ops token (P10).

    Two credentials because there are two callers and they fail in different weather. An
    administrator reading the portal is a session, which is a database row; a monitor polling
    every minute is a machine, which should not hold one that expires. The token also survives
    the case the endpoints exist for — a database too sick to resolve a session is exactly
    when somebody needs to read why — so it is checked first and without a query.

    ``/health`` and ``/ready`` are deliberately *not* behind this. They carry no business fact,
    an orchestrator holds no credential, and a readiness probe that can fail on authentication
    takes healthy instances out of rotation for the wrong reason.
    """
    expected = settings.ops_token
    if expected is not None:
        offered = request.headers.get("x-ops-token", "")
        if offered and hmac.compare_digest(offered, expected):
            return

    token = session_token_from(request, settings)
    resolved = await auth.resolve_session(session, token) if token else None
    if resolved is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Same reasoning as the admin gate: a borrowed identity is not a way to become an operator.
    if resolved.impersonated_by_id is not None or not resolved.learner.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an administrator")


OperatorDep = Depends(require_operator)
