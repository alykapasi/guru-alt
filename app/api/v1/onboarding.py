"""Onboarding endpoints: goal-refinement gate and curriculum generation."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.deps import CurrentLearner, LLMClientDep, SessionDep
from app.services import onboarding

router = APIRouter(tags=["onboarding"])


def _sse(obj: dict[str, Any]) -> str:
    """Format a dict as an SSE data frame."""
    return f"data: {json.dumps(obj)}\n\n"


class GoalTurnRequest(BaseModel):
    session_id: str
    content: str
    satisfied: bool = False
    mode: Annotated[str, "start or resume"] = "start"


class CurriculumRequest(BaseModel):
    goal: str
    source_ids: list[uuid.UUID] | None = None


class CurriculumResponse(BaseModel):
    subject_name: str
    subject_description: str
    topics: list[dict]  # Topic dicts with 'name', 'description', 'kcs'


@router.post("/onboarding/goal-turns")
async def goal_refinement_turn(
    request: GoalTurnRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
) -> StreamingResponse:
    """Start or resume goal refinement, stream TurnEvent responses as SSE.

    Streams: token, awaiting_reply, committed, or error events.
    """

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in onboarding.run_goal_refinement_turn(
                llm=llm,
                session_id=request.session_id,
                user_content=request.content,
                satisfied=request.satisfied,
                resume=request.mode == "resume",
            ):
                if event.type == "token":
                    yield _sse({"type": "token", "text": event.text})
                elif event.type == "awaiting_reply":
                    yield _sse(
                        {"type": "awaiting_reply", "text": event.text, "detail": event.detail}
                    )
                elif event.type == "committed":
                    yield _sse({"type": "committed", "goal": event.text, "detail": event.detail})
                elif event.type == "error":
                    yield _sse({"type": "error", "detail": event.detail})
        except Exception as exc:
            yield _sse({"type": "error", "detail": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/onboarding/curriculum", response_model=CurriculumResponse)
async def generate_curriculum_endpoint(
    request: CurriculumRequest,
    session: SessionDep,
    learner: CurrentLearner,
    llm: LLMClientDep,
) -> CurriculumResponse:
    """Generate a curriculum proposal from goal and optional source excerpts.

    Returns: CurriculumResponse or 400 on LLM failure.
    """
    proposal = await onboarding.generate_curriculum_for_onboarding(
        session=session,
        llm=llm,
        goal=request.goal,
        source_ids=request.source_ids,
        learner_id=learner.id,
    )
    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Curriculum generation failed. Please try again.",
        )

    # Convert CurriculumProposal to response format
    topics = []
    for topic in proposal.topics:
        topic_dict = {
            "name": topic.name,
            "description": topic.description,
            "kcs": [{"name": kc.name, "description": kc.description} for kc in topic.kcs],
        }
        topics.append(topic_dict)

    return CurriculumResponse(
        subject_name=proposal.subject_name,
        subject_description=proposal.subject_description,
        topics=topics,
    )
