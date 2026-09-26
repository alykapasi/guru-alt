"""Memory endpoints: view/erase a learner's memory, and trigger write-back for a conversation.

Nested-resource routes get their own file rather than folding into the parent's router —
mirrors ``lesson_plan.py``/``placement.py`` living apart from ``knowledge.py`` despite being
nested under ``/subjects/{id}``. So ``POST /conversations/{id}/memory/write-back`` lives here,
not in ``chat.py``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentLearner, LLMClientDep, MemoryWriteBackEnqueuerDep, SessionDep
from app.models.chat import Conversation
from app.schemas.memory import ForgetOriginRead, MemoryCorrection, MemoryRead, WriteBackAck
from app.services import chat as chat_svc
from app.services import memory as svc
from app.services import removal

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
    conversation = await chat_svc.get_conversation(session, conversation_id, learner_id=learner.id)
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
    memories = await svc.list_memories(session, learner.id, limit=limit)
    origins = {m.origin_conversation_id for m in memories if m.origin_conversation_id}
    live = {
        c.id: c.title or c.goal
        for c in (
            await session.scalars(
                select(Conversation).where(
                    Conversation.id.in_(origins), Conversation.learner_id == learner.id
                )
            )
        ).all()
    }
    return [
        MemoryRead.model_validate(m).model_copy(
            update={
                "origin_title": live.get(m.origin_conversation_id),
                "origin_live": m.origin_conversation_id in live,
            }
        )
        for m in memories
    ]


@router.post("/memory/forget-origin/{conversation_id}", response_model=ForgetOriginRead)
async def forget_origin(conversation_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    """Forget every memory learned in one conversation, even one already deleted (S42)."""
    return ForgetOriginRead(
        forgotten=await removal.forget_conversation_memories(session, learner.id, conversation_id)
    )


@router.patch("/memory/{memory_id}", response_model=MemoryRead)
async def correct_memory(
    memory_id: uuid.UUID,
    body: MemoryCorrection,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
):
    """Say what is actually true, rather than only being able to erase what is not (S16).

    Returns the *new* memory: a correction supersedes rather than overwrites, so the id
    changes and a client holding the old one is holding a superseded row (see the service).
    """
    corrected = await svc.correct_memory(session, llm, learner.id, memory_id, content=body.content)
    if corrected is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")
    return corrected


@router.delete("/memory/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: uuid.UUID, session: SessionDep, learner: CurrentLearner):
    if not await svc.delete_memory(session, learner.id, memory_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "memory not found")


@router.delete("/memory", status_code=status.HTTP_204_NO_CONTENT)
async def delete_all_memory(session: SessionDep, learner: CurrentLearner):
    """Erase every memory for the caller — the bulk "forget me" endpoint."""
    await svc.delete_all_memories(session, learner.id)
