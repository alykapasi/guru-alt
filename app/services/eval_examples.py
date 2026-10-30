"""Draw, serve and label evaluation examples (S59).

The service behind the admin portal's review tab. Drawing copies frozen candidates from the
area adapters (``app.services.eval_areas``); serving hands back the snapshot alone so the
labeller judges before seeing what the model decided; labelling stores the label and who wrote
it. Nothing here commits — the route does, so a request is one transaction.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation import AREAS, EvalExample
from app.services.eval_areas import ADAPTERS, GRADING_BANDS, VOCABULARY, Frozen, available

# How many candidates an adapter is asked for per example wanted: enough to stratify grading
# across bands without freezing the whole history to draw ten.
_POOL = 10


class UnknownArea(LookupError):
    pass


def _check_area(area: str) -> None:
    if area not in AREAS:
        raise UnknownArea(area)


def _stratified(candidates: list[Frozen], limit: int) -> list[Frozen]:
    """Round-robin over grading bands, so ten draws are not ten passes."""
    buckets: dict[str | None, list[Frozen]] = defaultdict(list)
    for c in candidates:
        buckets[c.stratum].append(c)
    order = [b for b in (*GRADING_BANDS, None) if buckets.get(b)]
    picked: list[Frozen] = []
    while len(picked) < limit and any(buckets[b] for b in order):
        for band in order:
            if buckets[band] and len(picked) < limit:
                picked.append(buckets[band].pop(0))
    return picked


async def draw(session: AsyncSession, area: str, limit: int = 10) -> int:
    """Copy up to ``limit`` new examples for ``area``; returns how many were copied."""
    _check_area(area)
    candidates = await ADAPTERS[area](session, limit * _POOL)
    picked = _stratified(candidates, limit) if area == "grading" else candidates[:limit]
    for c in picked:
        session.add(
            EvalExample(
                area=area,
                learner_id=c.learner_id,
                source_kind=c.source_kind,
                source_id=c.source_id,
                snapshot=c.snapshot,
                decision=c.decision,
            )
        )
    await session.flush()
    return len(picked)


async def next_unlabelled(
    session: AsyncSession, area: str, *, skip: list[uuid.UUID] | None = None
) -> EvalExample | None:
    """The oldest unlabelled example, past any the labeller skipped."""
    _check_area(area)
    query = select(EvalExample).where(EvalExample.area == area, EvalExample.label.is_(None))
    if skip:
        query = query.where(EvalExample.id.not_in(skip))
    return await session.scalar(query.order_by(EvalExample.created_at, EvalExample.id).limit(1))


async def labelled(session: AsyncSession, area: str, limit: int = 50) -> list[EvalExample]:
    _check_area(area)
    rows = await session.scalars(
        select(EvalExample)
        .where(EvalExample.area == area, EvalExample.label.isnot(None))
        .order_by(EvalExample.labelled_at.desc())
        .limit(limit)
    )
    return list(rows.all())


def validate_label(area: str, label: dict) -> dict:
    """The label as stored, or ``ValueError`` when it is not one of the area's values."""
    key, values = VOCABULARY[area]
    if set(label) != {key} or label[key] not in values:
        raise ValueError(f"a {area} label is {{{key!r}: one of {', '.join(values)}}}")
    return {key: label[key]}


async def save_label(
    session: AsyncSession,
    example_id: uuid.UUID,
    *,
    label: dict,
    note: str | None,
    by: uuid.UUID,
) -> EvalExample:
    example = await session.get(EvalExample, example_id)
    if example is None:
        raise LookupError(example_id)
    example.label = validate_label(example.area, label)
    example.note = note or None
    example.labelled_by_id = by
    example.labelled_at = datetime.now(UTC)
    await session.flush()
    return example


async def counts(session: AsyncSession) -> dict[str, dict[str, int]]:
    rows = await session.execute(
        select(
            EvalExample.area,
            func.count().filter(EvalExample.label.is_(None)),
            func.count().filter(EvalExample.label.isnot(None)),
        ).group_by(EvalExample.area)
    )
    by_area = {area: (unlabelled, done) for area, unlabelled, done in rows.all()}
    return {
        area: {
            "unlabelled": by_area.get(area, (0, 0))[0],
            "labelled": by_area.get(area, (0, 0))[1],
            "available": await available(session, area),
        }
        for area in AREAS
    }
