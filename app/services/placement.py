"""The placement diagnostic: ask + infer + light-test to seed a new subject's KC priors.

Deliberately a plain synchronous request, not a multi-turn graph: "asking" is one fixed
question (no LLM call to generate it), "inference" is one LLM call mapping the learner's
free-text answer to rough per-KC starting levels, and the "light test" surfaces a few real
items for the *existing* answer-and-grade endpoint to handle normally. Seeding only ever
writes a KC that has no state yet (see ``mastery.seed_prior``), so re-running placement, or a
KC later being answered for real, never clobbers evidence — no "finish placement" step needed.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.learning import item_generation, mastery
from app.learning.placement_inference import INFERENCE_ROLE, KCCandidate, infer_levels
from app.learning.tracer import Estimate
from app.llm import LLMClient
from app.models.assessment import Item, ItemKC
from app.models.knowledge import KC, Subject
from app.models.learning import LearnerKCState
from app.services import knowledge as knowledge_svc
from app.services.llm_log import log_llm_call

BACKGROUND_QUESTION = (
    "Before we dive in, tell me a bit about your background with {subject}: what have you "
    "already studied, worked with, or feel comfortable with?"
)

MAX_CANDIDATES = 40
"""Cap on how many KCs go into the inference prompt — keeps it bounded for large subjects."""

_ESTIMATE_BY_LEVEL: dict[str, Estimate] = {
    "some": Estimate(ability=0.75, uncertainty=0.75),
    "strong": Estimate(ability=1.75, uncertainty=0.6),
}
"""Reasonable-but-arbitrary v1 placements on the logit scale; revisit once calibration data
exists. High uncertainty (vs. a real observation's) reflects that this is a soft signal."""


@dataclass
class PlacementResult:
    seeded: list[LearnerKCState] = field(default_factory=list)
    light_test_items: list[Item] = field(default_factory=list)


async def _existing_item_for_kc(session: AsyncSession, kc_id: uuid.UUID) -> Item | None:
    return await session.scalar(
        select(Item)
        .join(ItemKC, ItemKC.item_id == Item.id)
        .where(ItemKC.kc_id == kc_id)
        .options(selectinload(Item.kc_links), selectinload(Item.rubric))
        .limit(1)
    )


async def run_placement(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    subject: Subject,
    background: str,
    light_test_size: int,
) -> PlacementResult:
    kcs = await knowledge_svc.list_kcs_for_subject(session, subject.id)
    if not kcs:
        return PlacementResult()

    candidates = [KCCandidate(id=kc.id, name=kc.name, description=kc.description) for kc in kcs]
    levels, infer_usage = await infer_levels(llm, background, candidates[:MAX_CANDIDATES])
    if infer_usage.total_tokens:
        await log_llm_call(
            session,
            learner_id=learner_id,
            role=INFERENCE_ROLE.value,
            spec=llm.spec(INFERENCE_ROLE),
            usage=infer_usage,
        )

    root_kcs: list[KC] = list(await knowledge_svc.list_root_kcs(session, subject.id))
    light_test_items: list[Item] = []
    for kc in root_kcs[:light_test_size]:
        item = await _existing_item_for_kc(session, kc.id)
        if item is None:
            item, gen_usage = await item_generation.generate_mcq_item(session, llm, kc)
            if gen_usage.total_tokens:
                await log_llm_call(
                    session,
                    learner_id=learner_id,
                    role=item_generation.GENERATION_ROLE.value,
                    spec=llm.spec(item_generation.GENERATION_ROLE),
                    usage=gen_usage,
                )
        if item is not None:
            light_test_items.append(item)

    seeded: list[LearnerKCState] = []
    for inferred in levels:
        estimate = _ESTIMATE_BY_LEVEL[inferred.level]
        state = await mastery.seed_prior(session, learner_id, inferred.kc_id, estimate)
        if state is not None:
            seeded.append(state)

    await session.commit()
    return PlacementResult(seeded=seeded, light_test_items=light_test_items)
