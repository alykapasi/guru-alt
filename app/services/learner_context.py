"""What every mode knows about the learner (S16).

Plain chat folded three things into its system prompt: the conversation's committed goal, the
lesson plan's active step, and the facts remembered about this learner from past conversations.
The agentic path folded in none of them. Guided practice folded in only the step it was already
practising, and neither the goal nor the memory.

The client offers the modes as a toggle on one conversation, so this was not three separate
products with three levels of knowledge — it was the same conversation forgetting who it was
talking to whenever the learner pressed a different button, and remembering again when they
pressed back. A learner who had spent ten turns establishing that they think in pictures and
are working towards a specific exam got a tool-using answer that knew neither.

This module is the single place that assembles that context and the single order it is composed
in, so the three flows cannot drift apart again by omission — which is how they drifted apart
the first time.

**Why one piece is conditional.** ``hint_density`` and the plan's focus describe *how to teach*
and go everywhere. The difficulty band and preferred item type describe *what task to set*, and
go only where no task is fixed yet. Guided practice is told in the same breath to pose one exact
problem and not to invent a different one; adding "aim at a challenging level" to that is an
instruction to do the thing it was just forbidden to do. The same applies to a plain-chat turn
holding an open check (S15). So the rule is uniform: once a specific item is in play, the
item-selection hints are withheld.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.untrusted import as_untrusted
from app.core.config import get_settings
from app.learning import difficulty
from app.llm.registry import LLMClient
from app.memory import retrieval as memory_retrieval
from app.memory.retrieval import MemoryHit
from app.models.chat import Conversation
from app.services.lesson_plan import PlanGroundingContext, get_active_step_context


@dataclass(frozen=True)
class LearnerContext:
    """The learner-facing state a turn should carry, whichever graph is running."""

    goal: str | None
    plan: PlanGroundingContext | None
    memories: Sequence[MemoryHit]


async def gather(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    query: str,
) -> LearnerContext:
    """Read the shared context for one turn.

    Two reads. The plan's active step is looked up with the conversation's ``subject_id`` when
    it has one, which makes the lookup exact; a subject-less conversation still gets a step,
    chosen by ``get_active_step_context``'s cross-subject heuristic, and that is deliberate —
    a "general" conversation is the one most likely to be the learner wandering away from a
    plan they are in the middle of.

    Memory retrieval stays learner-global rather than scoped to a subject: what someone told us
    about how they learn does not stop being true in another subject (MASTERPLAN §5).
    """
    plan = await get_active_step_context(session, learner_id, subject_id=conversation.subject_id)
    memories = await memory_retrieval.retrieve(
        session,
        llm,
        query,
        learner_id=learner_id,
        limit=get_settings().memory_retrieval_limit,
    )
    return LearnerContext(goal=conversation.goal, plan=plan, memories=memories)


def compose(
    base: str,
    context: LearnerContext,
    *,
    extra: Sequence[str] = (),
    grounding: str | None = None,
    task_fixed: bool = False,
) -> str:
    """The system prompt, in the one order every mode uses.

    ``base`` -> goal -> plan focus -> ``extra`` -> retrieval grounding -> memory. ``extra`` is
    where a flow puts what only it has: the exact practice problem, or the grade of the answer
    just given. Memory goes last because it is the least specific thing in the prompt and the
    most quotable; grounding goes second-last so citations sit next to the passages.

    ``task_fixed`` withholds the item-selection hints — see the module docstring.
    """
    parts = [base]
    if context.goal:
        parts.append(f"The learner's stated goal for this conversation: {context.goal}")
    focus = plan_note(context.plan, task_fixed=task_fixed)
    if focus is not None:
        parts.append(focus)
    parts.extend(p for p in extra if p)
    if grounding is not None:
        parts.append(grounding)
    memory = memory_note(context.memories)
    if memory is not None:
        parts.append(memory)
    return "\n\n".join(parts)


def plan_note(context: PlanGroundingContext | None, *, task_fixed: bool = False) -> str | None:
    """What the plan says about this learner right now, or ``None`` if there is no plan."""
    if context is None:
        return None
    parts = [
        f"The learner's current lesson-plan focus in {context.subject_name}: {context.kc_name}."
    ]
    if context.hint_density is not None:
        parts.append(f"Hint density: {context.hint_density}.")
    if not task_fixed:
        if context.target_difficulty is not None:
            # A band, not the number. This used to interpolate the raw logit, which meant the
            # tutor's system prompt carried the sentence "Target difficulty: 0.00." — and since
            # nothing had ever written a difficulty to an item, 0.00 was the only value it could
            # take. An instruction a model cannot act on is not a neutral one; it still steers.
            parts.append(f"Aim at a {difficulty.band(context.target_difficulty)} level.")
        if context.preferred_item_type is not None:
            parts.append(f"Preferred item type: {context.preferred_item_type}.")
    return " ".join(parts)


def memory_note(hits: Sequence[MemoryHit]) -> str | None:
    """Remembered facts about this learner, fenced as data, or ``None`` if there are none."""
    if not hits:
        return None
    facts = "; ".join(f"[{h.kind}] {h.content}" for h in hits)
    # Fenced as data (S31): a memory is extracted from conversation text, so a hostile passage
    # that reached one turn can be quoted back into every later one as remembered fact. This
    # matters more now than it did in plain chat alone — the agentic graph can act on what it
    # reads here, so an injected "memory" reaches a tool call rather than only a paragraph.
    return (
        "What you remember about this learner from past conversations:\n"
        f"{as_untrusted('LEARNER MEMORY', facts)}"
    )
