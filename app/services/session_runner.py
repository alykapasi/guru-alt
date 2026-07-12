"""The session runner: turns the lesson plan's active step into something to actually do.

"Follows" the plan by resolving its active step into a concrete practice item (reusing the
bank, generating only if nothing exists yet for that KC); "updates" the plan by doing nothing
new at all — answering the item through the existing, unchanged ``/items/{id}/answer`` endpoint
already runs the tracer and auto-``revise_plan``, which is what actually advances the active
step. No parallel "the conversation seemed to cover this" advancement mechanism: the tracer is
the only thing that moves mastery.

Item selection is type-aware: the active step's ``preferred_item_type`` (profile-driven, from
``format_effectiveness``) is tried first, falling back to a flashcard default for ``"review"``
steps (spaced-repetition surfacing) when there's no explicit preference, and finally to
any-type reuse then MCQ generation — see ``item_for_kc``. ``target_difficulty`` remains an
unapplied v1 gap: no item-level difficulty targeting exists for generated items yet.

``due_review_items`` is the plan-independent counterpart: a learner clearing their FSRS review
queue doesn't need to be mid-lesson, so it resolves flashcards directly off
``mastery.due_reviews`` rather than a lesson plan's active step. It's the first read-only
endpoint in this codebase with a generation side effect (every other generation call site sits
behind a POST) — defensible on the same reuse-then-generate-forever economics as everywhere
else, bounded by ``reviews_due_item_limit`` so a large backlog can't trigger unbounded LLM
calls on one request.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import item_generation, mastery
from app.learning.mastery import ReviewItem
from app.llm import LLMClient
from app.models.assessment import Item, ItemType
from app.models.knowledge import KC
from app.services import assessment as assessment_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.lesson_plan import PlanGroundingContext
from app.services.llm_log import log_llm_call


async def item_for_kc(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    kc: KC,
    preferred_type: ItemType | None,
) -> Item | None:
    """Resolve something answerable for ``kc``, preferring ``preferred_type`` if given.

    Order: reuse a bank item of ``preferred_type`` (a free win even for types we can't
    generate) → generate one of ``preferred_type`` (only if a generator exists) → reuse any
    bank item for the KC → generate an MCQ (the safe default).
    """
    if preferred_type is not None:
        item = await assessment_svc.find_item_for_kc(session, kc.id, item_type=preferred_type)
        if item is not None:
            return item
        generator = item_generation.GENERATORS.get(preferred_type)
        if generator is not None:
            item = await _generate_and_log(
                session, llm, kc, learner_id=learner_id, generator=generator
            )
            if item is not None:
                return item

    item = await assessment_svc.find_item_for_kc(session, kc.id)
    if item is not None:
        return item
    return await _generate_and_log(
        session, llm, kc, learner_id=learner_id, generator=item_generation.generate_mcq_item
    )


async def short_answer_item_for_kc(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, kc: KC
) -> Item | None:
    """A SHORT (open, rubric-graded) item for ``kc`` — reuse-then-generate only.

    Unlike ``item_for_kc``, this has no any-type/MCQ fallback: the guided-practice workflow's
    ``{"text": ...}`` submission shape only grades correctly against a SHORT item (MCQ grading
    reads ``response["choice"]``, which would always be ``None`` and always score
    "incorrect" — a silent correctness bug, not a crash). Returns ``None`` only if generation
    itself fails to parse.
    """
    item = await assessment_svc.find_item_for_kc(session, kc.id, item_type=ItemType.SHORT)
    if item is not None:
        return item
    return await _generate_and_log(
        session, llm, kc, learner_id=learner_id, generator=item_generation.generate_short_item
    )


async def _generate_and_log(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    learner_id: uuid.UUID,
    generator: item_generation.GeneratorFn,
) -> Item | None:
    item, usage = await generator(session, llm, kc)
    if usage.total_tokens:
        await log_llm_call(
            session,
            learner_id=learner_id,
            role=item_generation.GENERATION_ROLE.value,
            spec=llm.spec(item_generation.GENERATION_ROLE),
            usage=usage,
        )
    return item


def _effective_item_type(context: PlanGroundingContext) -> ItemType | None:
    """The plan's steer on item type: an explicit profile preference wins; otherwise review
    steps default to a flashcard (spaced-repetition surfacing); new steps have no preference."""
    if context.preferred_item_type is not None:
        try:
            return ItemType(context.preferred_item_type)
        except ValueError:
            return None  # profile stored a format string with no matching ItemType — degrade
    if context.step_type == "review":
        return ItemType.FLASHCARD
    return None


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
    return await item_for_kc(
        session, llm, learner_id=learner_id, kc=kc, preferred_type=_effective_item_type(context)
    )


async def due_review_items(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, item_limit: int
) -> list[tuple[ReviewItem, Item | None]]:
    """Every due review, paired with a resolved flashcard for the first ``item_limit`` (the
    due list is soonest-due-first, so this caps the *nearest* reviews, not an arbitrary slice).
    Entries past the cap carry ``None`` — still due, just not eagerly resolved this call.
    """
    reviews = await mastery.DEFAULT_TRACER.due_reviews(session, learner_id)
    results: list[tuple[ReviewItem, Item | None]] = []
    # Sequential, not gathered: item_for_kc can call session.commit() on this one shared
    # AsyncSession, and concurrent operations on a single session are unsafe.
    for i, review in enumerate(reviews):
        item = None
        if i < item_limit:
            kc = await session.get(KC, review.kc_id)
            if kc is not None:
                item = await item_for_kc(
                    session, llm, learner_id=learner_id, kc=kc, preferred_type=ItemType.FLASHCARD
                )
        results.append((review, item))
    return results
