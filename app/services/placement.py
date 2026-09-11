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

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import difficulty, item_generation, mastery
from app.learning.placement_inference import INFERENCE_ROLE, KCCandidate, infer_levels
from app.learning.tracer import Estimate
from app.llm import LLMClient
from app.models.assessment import Item
from app.models.knowledge import KC, Subject
from app.models.learning import LearnerKCState
from app.services import assessment as assessment_svc
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
            learner_id=learner_id,
            role=INFERENCE_ROLE.value,
            spec=llm.spec(INFERENCE_ROLE),
            usage=infer_usage,
        )

    # What the learner's own description implies, per KC — used twice below: to pitch the
    # light test, and then to seed. A KC the inference gave no evidence for falls back to the
    # unknown prior, which is ability 0 and therefore a light test at population average.
    inferred: dict[uuid.UUID, Estimate] = {
        level.kc_id: _ESTIMATE_BY_LEVEL[level.level] for level in levels
    }

    root_kcs: list[KC] = list(await knowledge_svc.list_root_kcs(session, subject.id))
    light_test_items: list[Item] = []
    for kc in root_kcs[:light_test_size]:
        # A diagnostic wants the question it cannot predict, not the one the learner will
        # enjoy: an answer at a 50% expectation carries the most information about where they
        # actually are (S12). Practice targets the opposite end of the same scale.
        #
        # Placement seeds a new subject, so the inference is normally all there is. Re-running
        # it for a subject the learner has already answered in targets from the self-report
        # rather than from that evidence — a stale aim for one light test, not a wrong prior:
        # ``seed_prior`` still refuses to overwrite what was actually demonstrated.
        target = difficulty.target_for(
            inferred.get(kc.id, Estimate()),
            success_rate=difficulty.INFORMATIVE_SUCCESS_RATE,
        )
        item = await assessment_svc.find_item_for_kc(
            session, kc.id, learner_id=learner_id, target_difficulty=target
        )
        if item is None:
            item, gen_usage = await item_generation.generate_mcq_item(
                session, llm, kc, target_difficulty=target
            )
            if gen_usage.total_tokens:
                await log_llm_call(
                    learner_id=learner_id,
                    role=item_generation.GENERATION_ROLE.value,
                    spec=llm.spec(item_generation.GENERATION_ROLE),
                    usage=gen_usage,
                )
        if item is not None:
            light_test_items.append(item)

    seeded: list[LearnerKCState] = []
    for kc_id, estimate in inferred.items():
        state = await mastery.seed_prior(session, learner_id, kc_id, estimate)
        if state is not None:
            seeded.append(state)

    await session.commit()
    return PlacementResult(seeded=seeded, light_test_items=light_test_items)
