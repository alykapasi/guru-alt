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
any-type reuse then generation of ``DEFAULT_GENERATED_TYPE`` — see ``item_for_kc``.

It is also difficulty-aware (S12): every path through here resolves a *practice* target from
the learner's own ability for that KC — the difficulty at which they would succeed
``settings.practice_target_success_rate`` of the time — and hands it to both selection and
generation. The plan step carries a ``target_difficulty`` of its own, from the
``optimal_challenge`` profile dimension, and this deliberately does not use it: that dimension
is one number for the whole learner, averaged over every component they have answered, while
the tracer holds a separate ability per KC. Per-KC mastery is the premise the whole engine
rests on; collapsing it to a single number to choose an item for one specific component throws
away exactly the distinction that made it worth keeping.

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

from app.core.config import get_settings
from app.learning import difficulty, item_generation, mastery
from app.learning.mastery import ReviewItem
from app.learning.tracer import Estimate
from app.llm import LLMClient
from app.models.assessment import Item, ItemType
from app.models.knowledge import KC
from app.services import assessment as assessment_svc
from app.services import lesson_plan as lesson_plan_svc
from app.services.lesson_plan import PlanGroundingContext
from app.services.llm_log import log_llm_call

DEFAULT_GENERATED_TYPE = ItemType.SHORT
"""What gets generated when nothing has asked for a particular type (S09/S10).

This was MCQ, on the reasoning that a deterministic key is the safe thing to invent when you
have no instruction. That is true about *grading* and false about *teaching*: a multiple-choice
question has one outcome, so it yields no failure kind and no per-component split, and both of
those are now load-bearing — the teaching move, the repair, the learner-facing report and the
component breakdown all read fields an MCQ cannot populate. An open question is the only
generatable type that fills them, so it is what an unspecified default should produce.

It is not free, and that is the trade being made deliberately rather than by inheritance: a
SHORT answer costs a SMART-role rubric call to grade, where an MCQ costs nothing. Placement
still generates MCQs on purpose (``app.services.placement``) — a light test wants breadth per
token and is not trying to teach anything — and that call names the generator directly rather
than relying on this.
"""


def practice_target(estimate: Estimate) -> float:
    """The difficulty to ask for when the point is to teach, given what we believe about the
    learner. Assessment wants the opposite end of the same scale — see ``app.learning.difficulty``."""
    return difficulty.target_for(estimate, success_rate=get_settings().practice_target_success_rate)


async def practice_target_for_kc(
    session: AsyncSession, *, learner_id: uuid.UUID, kc_id: uuid.UUID
) -> float:
    """``practice_target`` for one KC, reading the learner's current estimate."""
    return practice_target(await mastery.estimate_kc(session, learner_id, kc_id))


async def item_for_kc(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    kc: KC,
    preferred_type: ItemType | None,
    target_difficulty: float | None = None,
    default_type: ItemType = DEFAULT_GENERATED_TYPE,
) -> Item | None:
    """Resolve something answerable for ``kc``, preferring ``preferred_type`` if given.

    Order: an *unseen* bank item of ``preferred_type`` → generate one (if a generator exists)
    → any bank item of that type, seen or not → any bank item for the KC → generate a
    ``default_type`` item. See ``_fresh_or_generate`` for why exhaustion triggers generation
    rather than only emptiness (S14).

    ``default_type`` is a parameter rather than a constant so the choice is made by whoever
    knows what the item is for; see ``DEFAULT_GENERATED_TYPE`` for why the default is open.
    """
    if preferred_type is not None:
        item = await _fresh_or_generate(
            session,
            llm,
            kc,
            learner_id=learner_id,
            item_type=preferred_type,
            target_difficulty=target_difficulty,
        )
        if item is not None:
            return item

    item = await assessment_svc.find_item_for_kc(
        session, kc.id, learner_id=learner_id, target_difficulty=target_difficulty
    )
    if item is not None:
        return item
    generator = item_generation.GENERATORS.get(default_type)
    if generator is None:
        return None  # a type nothing can generate is not a default anything can fall back to
    return await _generate_and_log(
        session,
        llm,
        kc,
        learner_id=learner_id,
        generator=generator,
        target_difficulty=target_difficulty,
    )


async def short_answer_item_for_kc(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, kc: KC
) -> Item | None:
    """A SHORT (open, rubric-graded) item for ``kc`` — reuse-then-generate only.

    Unlike ``item_for_kc``, this has no any-type/MCQ fallback: a ``{"text": ...}`` submission
    only grades against a SHORT item. MCQ grading reads ``response["choice"]`` and rejects the
    submission outright (``InvalidResponse``), so a prose answer to an MCQ is not a wrong
    answer — it is an ungradable one, and the learner's attempt is lost rather than scored.
    Returns ``None`` only if generation itself fails to parse.

    Resolves its own practice target rather than taking one: neither caller — the
    guided-practice workflow, and the conversational check in ``app.services.chat`` (S15) —
    has a reason to hold an opinion about difficulty, and leaving the parameter for them to
    pass would have meant both quietly opting out of S12.
    """
    target_difficulty = await practice_target_for_kc(session, learner_id=learner_id, kc_id=kc.id)
    return await _fresh_or_generate(
        session,
        llm,
        kc,
        learner_id=learner_id,
        item_type=ItemType.SHORT,
        target_difficulty=target_difficulty,
    )


async def _fresh_or_generate(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    learner_id: uuid.UUID,
    item_type: ItemType,
    target_difficulty: float | None,
) -> Item | None:
    """An item of ``item_type`` this learner has not answered before, generating one if the
    bank is used up (S14).

    Exposure-ordered reuse makes a revisit a different question only while there is a spare
    question to be different. A component with one item repeated it, which is the failure the
    ordering existed to stop: an answer recalled from the last time it was given measures
    memory of that exchange, not retention of the component — and it still moved the estimate
    upwards, so the thinnest banks produced the most confident numbers.

    Generation is therefore triggered by *exhaustion* rather than only by emptiness. The
    seen-item fallback stays as the last resort: a repeated question is worse evidence than a
    fresh one and far better than telling a learner there is nothing to practise, so a model
    that will not produce a parseable item does not end the session.
    """
    item = await assessment_svc.find_item_for_kc(
        session,
        kc.id,
        learner_id=learner_id,
        item_type=item_type,
        target_difficulty=target_difficulty,
        unseen_only=True,
    )
    if item is not None:
        return item
    generator = item_generation.GENERATORS.get(item_type)
    if generator is not None:
        generated = await _generate_and_log(
            session,
            llm,
            kc,
            learner_id=learner_id,
            generator=generator,
            target_difficulty=target_difficulty,
        )
        if generated is not None:
            return generated
    return await assessment_svc.find_item_for_kc(
        session,
        kc.id,
        learner_id=learner_id,
        item_type=item_type,
        target_difficulty=target_difficulty,
    )


async def _generate_and_log(
    session: AsyncSession,
    llm: LLMClient,
    kc: KC,
    *,
    learner_id: uuid.UUID,
    generator: item_generation.GeneratorFn,
    target_difficulty: float | None = None,
) -> Item | None:
    item, usage = await generator(session, llm, kc, target_difficulty=target_difficulty)
    if usage.total_tokens:
        await log_llm_call(
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
        session,
        llm,
        learner_id=learner_id,
        kc=kc,
        preferred_type=_effective_item_type(context),
        target_difficulty=await practice_target_for_kc(session, learner_id=learner_id, kc_id=kc.id),
    )


async def review_item_type(
    session: AsyncSession, *, learner_id: uuid.UUID, kc_id: uuid.UUID
) -> ItemType:
    """Whether this due review is still worth self-rating, or needs diagnosing (S09/S10).

    A flashcard is graded by the learner's own rating. That is the right instrument for
    ordinary spaced repetition — it is fast, it costs nothing, and recall is the thing being
    rehearsed. It is the wrong instrument for a component the learner keeps getting wrong,
    because a run of low self-ratings drives the ability estimate down while recording nothing
    at all about *why*, and "why" is what decides whether the answer is a notation slip, a
    missing prerequisite, or a genuine misconception.

    So a component that has failed its last few reviews is served an open question instead: the
    same review, in the one format the grader can diagnose and split by component. The learner
    is asked to produce the answer rather than rate their own recall of it, which is also the
    stronger retention measure (S14).

    Reads the same run of attempts the detour rule reads (``mastery.recent_struggle``) and the
    same definition of "failed", deliberately — see ``review_diagnose_min_failures``.
    """
    settings = get_settings()
    struggle = await mastery.recent_struggle(
        session, learner_id, kc_id, threshold=settings.detour_failure_threshold
    )
    if struggle.consecutive_failures >= settings.review_diagnose_min_failures:
        return ItemType.SHORT
    return ItemType.FLASHCARD


async def due_review_items(
    session: AsyncSession, llm: LLMClient, *, learner_id: uuid.UUID, item_limit: int
) -> list[tuple[ReviewItem, Item | None]]:
    """Every due review, paired with a resolved item for the first ``item_limit`` (the
    due list is soonest-due-first, so this caps the *nearest* reviews, not an arbitrary slice).
    Entries past the cap carry ``None`` — still due, just not eagerly resolved this call.

    Usually a flashcard; an open question where the component has been failing (see
    ``review_item_type``). The struggle read is per resolved review, so it is bounded by
    ``item_limit`` along with everything else this loop does.
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
                # The estimate is already in hand: ReviewItem carries the learner's ability
                # for this KC, and decay only widens uncertainty, so the undecayed row is the
                # same ability estimate_kc would return — no second query to target.
                item = await item_for_kc(
                    session,
                    llm,
                    learner_id=learner_id,
                    kc=kc,
                    preferred_type=await review_item_type(
                        session, learner_id=learner_id, kc_id=kc.id
                    ),
                    target_difficulty=practice_target(
                        Estimate(ability=review.ability, uncertainty=review.uncertainty)
                    ),
                )
        results.append((review, item))
    return results
