"""Criteria and a rated level for a check that has none, written on its first attempt (S56).

A check the tutor declares in conversation is an item with no rubric and no judged difficulty
(``chat._materialise_declared_check``); a posed check whose generated criteria did not parse
is the same. Writing them when the check is declared would pay for questions nobody answers,
and letting the tutor write them into its marker would stream the answer onto the learner's
screen. So they are written here, once, when an answer has passed the intent gate and before
it is graded — from the question alone, never from the answer.
"""

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import difficulty, item_generation
from app.llm import LLMClient
from app.llm.attribution import metered
from app.models.assessment import Item, Rubric
from app.models.knowledge import KC

log = structlog.get_logger(__name__)


@metered("check_criteria", learner="learner_id")
async def ensure_criteria(
    session: AsyncSession, llm: LLMClient, learner_id: uuid.UUID, item: Item
) -> Item:
    """Give ``item`` criteria and a rated difficulty if it has no rubric yet; returns it.

    The row is locked first, so two first attempts racing cannot both write: the second waits,
    finds the first one's rubric, and makes no call. A reply without usable criteria leaves
    the item as it was — it is then graded against the grader's fallback, as before — and a
    level that is not a band name leaves the difficulty alone.
    """
    await session.refresh(item, ["rubric_id"], with_for_update=True)
    if item.rubric_id is not None:
        await session.refresh(item, ["rubric"])
        return item
    kc = await session.get(KC, item.kc_links[0].kc_id) if item.kc_links else None
    if kc is None:
        return item
    criteria, level, _usage = await item_generation.write_criteria(
        llm, stem=item.stem, component_name=kc.name, component_description=kc.description or ""
    )
    if criteria:
        rubric = Rubric(
            kc_id=kc.id,
            owner_learner_id=learner_id,
            name="Criteria for a conversational check",
            criteria={"criteria": criteria},
        )
        session.add(rubric)
        await session.flush()
        item.rubric_id = rubric.id
        item.rubric = rubric
    else:
        log.info("check.criteria_unavailable", item_id=str(item.id))
    if level is not None:
        item.difficulty = difficulty.midpoint(level)
    await session.flush()
    return item
