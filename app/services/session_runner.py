"""The session runner: turns the lesson plan's active step into something to actually do.

"Follows" the plan by resolving its active step into a concrete practice item (reusing the
bank, generating only if nothing exists yet for that KC); "updates" the plan by doing nothing
new at all — answering the item through the existing, unchanged ``/items/{id}/answer`` endpoint
already runs the tracer and auto-``revise_plan``, which is what actually advances the active
step. No parallel "the conversation seemed to cover this" advancement mechanism: the tracer is
the only thing that moves mastery.

v1 scope note: the active step's ``target_difficulty``/``preferred_item_type`` hints are not
yet applied to item selection — ``find_item_for_kc``/``generate_mcq_item`` have no
difficulty/type filter, so the plan drives *which* KC gets practiced, not yet *what
difficulty/format* it's practiced at.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation
from app.llm import LLMClient
from app.models.assessment import Item
from app.models.knowledge import KC
from app.services import assessment as assessment_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.llm_log import log_llm_call


async def next_item(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> Item | None:
    """The plan's active step for ``subject_id``, turned into a practice item — or ``None`` if
    there's no plan yet, or the plan is fully done (no active step)."""
    context = await lesson_plan_svc.get_active_step_context(
        session, learner_id, subject_id=subject_id
    )
    if context is None:
        return None
    kc = await session.get(KC, context.kc_id)
    if kc is None:
        return None

    item = await assessment_svc.find_item_for_kc(session, kc.id)
    if item is not None:
        return item

    item, usage = await item_generation.generate_mcq_item(session, llm, kc)
    if usage.total_tokens:
        await log_llm_call(
            session,
            learner_id=learner_id,
            role=item_generation.GENERATION_ROLE.value,
            spec=llm.spec(item_generation.GENERATION_ROLE),
            usage=usage,
        )
    return item
