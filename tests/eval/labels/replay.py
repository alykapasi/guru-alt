"""Re-run today's prompt on frozen inputs — locally, unless someone says otherwise (S59).

A replay answers "would the current prompt agree with the labels better than the one that
made the recorded decision?". It is an evaluation run, so it is subject to the same rule as
every other: nothing pays by accident. :func:`require_free` refuses to start unless every role
the replay needs resolves to a local or stand-in provider, and ``--allow-paid`` is the only way
past it.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from app.learning import conversation_evidence
from app.learning.grading import GradeResult
from app.learning.rubric_grading import GRADING_ROLE, GradedComponent, grade_open
from app.llm import LLMClient
from app.llm.attribution import attributed
from app.llm.types import ModelRole
from app.models.assessment import Rubric
from app.services.eval_areas import grading_band

FREE_PROVIDERS = frozenset({"ollama", "shaped", "fake"})

ROLES: dict[str, tuple[ModelRole, ...]] = {
    "grading": (GRADING_ROLE,),
    "diagnosis": (GRADING_ROLE,),
    "intent": (conversation_evidence.CHECK_ROLE,),
    "source_support": (),
}


class PaidProviderRefused(RuntimeError):
    pass


def require_free(llm: LLMClient, roles: Iterable[ModelRole], *, allow_paid: bool) -> None:
    if allow_paid:
        return
    paid = sorted(
        {f"{role.name}={llm.spec(role).provider}" for role in roles}
        - {f"{role.name}={p}" for role in roles for p in FREE_PROVIDERS}
    )
    if paid:
        raise PaidProviderRefused(
            f"refusing to replay on a paid provider ({', '.join(paid)}); pass --allow-paid to spend"
        )


async def _grade(
    llm: LLMClient, snapshot: dict, component: str | None
) -> tuple[GradeResult, uuid.UUID]:
    kc_id = uuid.uuid4()
    components = (
        [GradedComponent(kc_id=kc_id, name=component, description="", criteria=None)]
        if component
        else []
    )
    criteria = snapshot.get("criteria")
    result, _usage = await grade_open(
        llm,
        stem=snapshot["stem"],
        response={"text": snapshot["answer"]},
        rubric=Rubric(criteria=criteria) if criteria else None,
        components=components,
    )
    return result, kc_id


async def current_decision(llm: LLMClient, area: str, snapshot: dict) -> str | None:
    """What today's prompt decides on the frozen inputs, in the area's label vocabulary."""
    with attributed(feature="label_replay", learner_id=None, conversation_id=None, background=True):
        if area == "grading":
            result, _ = await _grade(llm, snapshot, None)
            return grading_band(result.score)
        if area == "diagnosis":
            result, kc_id = await _grade(llm, snapshot, snapshot.get("component") or "the idea")
            diagnosis = result.diagnoses.get(kc_id)
            return diagnosis.kind.value if diagnosis is not None else None
        if area == "intent":
            intent, _usage = await conversation_evidence.classify_intent(
                llm, question=snapshot["stem"], message=snapshot["message"]
            )
            return intent.value
        # Source support's decision is a rule, not a prompt: there is nothing to replay.
        return None
