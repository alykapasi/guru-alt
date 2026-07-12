"""Memory endpoints: view/erase a learner's memory, and trigger write-back for a conversation.

Nested-resource routes get their own file rather than folding into the parent's router —
mirrors ``lesson_plan.py``/``placement.py`` living apart from ``knowledge.py`` despite being
nested under ``/subjects/{id}``. So ``POST /conversations/{id}/memory/write-back`` lives here,
not in ``chat.py``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentLearner, MemoryWriteBackEnqueuerDep, SessionDep
from app.schemas.memory import MemoryRead, WriteBackAck
from app.services import chat as chat_svc
from app.services import memory as svc

router = APIRouter(tags=["memory"])


@router.post(
    "/conversations/{conversation_id}/memory/write-back",
    response_model=WriteBackAck,
    status_code=status.HTTP_202_ACCEPTED,
)
async def write_back(
    conversation_id: uuid.UUID,
    session: SessionDep,
    learner: CurrentLearner,
    enqueue: MemoryWriteBackEnqueuerDep,
):
    conversation = await chat_svc.get_conversation(session, conversation_id)
    if conversation is None or conversation.learner_id != learner.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    await enqueue(conversation_id)
    return WriteBackAck()


@router.get("/memory", response_model=list[MemoryRead])
async def list_memory(
    session: SessionDep,
    learner: CurrentLearner,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    return await svc.list_memories(session, learner.id, limit=limit)


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    if not await svc.delete_memory(session, learner.id, memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_memory(session: SessionDep, learner: CurrentLearner):
    """Erase every memory for the caller — the bulk "forget me" endpoint."""
    await svc.delete_all_memories(session, learner.id)
