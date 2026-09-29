"""What a grade was measured against, frozen and referenced from the event (S56).

``answer_item`` calls ``record`` once per graded attempt; the returned block goes on every
event of the attempt. Snapshots live on the grading session, so a grade and its provenance
commit or roll back together.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.learning.grading import GradeResult
from app.learning.rubric_grading import GradedComponent
from app.models.assessment import Item, Rubric
from app.models.grading import GradingSnapshot


def digest(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def keep(session: AsyncSession, learner_id: uuid.UUID, kind: str, content: dict) -> str:
    sha = digest(content)
    await session.execute(
        pg_insert(GradingSnapshot)
        .values(learner_id=learner_id, sha256=sha, kind=kind, content=content)
        .on_conflict_do_nothing(index_elements=["learner_id", "sha256"])
    )
    return sha


def item_content(item: Item, components: Sequence[GradedComponent]) -> dict:
    return {
        "item_type": item.item_type,
        "stem": item.stem,
        "answer_key": item.answer_key,
        "difficulty": item.difficulty,
        "components": [
            {"kc_id": str(c.kc_id), "name": c.name, "description": c.description}
            for c in components
        ],
    }


def rubric_content(rubric: Rubric | None, components: Sequence[GradedComponent]) -> dict | None:
    """The criteria as the grader saw them: the rubric's own, and against which component."""
    if rubric is None or not rubric.criteria:
        return None
    return {
        "criteria": rubric.criteria,
        "components": [
            {"kc_id": str(c.kc_id), "criteria": c.criteria} for c in components if c.criteria
        ],
    }


async def record(
    session: AsyncSession,
    learner_id: uuid.UUID,
    item: Item,
    components: Sequence[GradedComponent],
    result: GradeResult,
) -> dict:
    provenance = result.provenance
    grader = provenance.grader if provenance is not None else "auto"
    rubric = rubric_content(item.rubric, components)
    prompt_sha = None
    if provenance is not None and provenance.system_prompt is not None:
        prompt_sha = await keep(
            session,
            learner_id,
            "prompt",
            {
                "system": provenance.system_prompt,
                "template_version": provenance.template_version,
            },
        )
    return {
        "grader": grader,
        "item": await keep(session, learner_id, "item", item_content(item, components)),
        "rubric": await keep(session, learner_id, "rubric", rubric) if rubric else None,
        "prompt": prompt_sha,
        "model": provenance.model if provenance is not None else None,
        "app_version": get_settings().app_version,
    }
