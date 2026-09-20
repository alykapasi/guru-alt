"""Subject slugs stop reporting on other learners' libraries (S25b D8).

S25a closed listing and writing across the ownership boundary, but the slug survived it. The
de-duplication counted every slug in the table, so asking for a name a stranger had privately
used handed back ``name_2`` — an answer about their library, obtainable by anyone willing to
create a subject, read its slug and delete it again.

The test that matters is the first one. The second is here because the obvious fix — stop
de-duplicating — would pass the first and quietly destroy the guarantee that one learner's two
subjects stay distinguishable.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.learner import Learner
from app.services import knowledge as svc


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"slug-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _subject(session: AsyncSession, learner: Learner, name: str) -> str:
    result = await svc.create_subject_with_graph(
        session,
        subject_name=name,
        subject_description=None,
        topics_data=[],
        source_ids=None,
        learner_id=learner.id,
    )
    return result.subject.slug


async def test_a_strangers_subject_does_not_suffix_mine(db_session: AsyncSession) -> None:
    """The leak, stated as the behaviour that closes it."""
    a, b = await _learner(db_session), await _learner(db_session)
    await _subject(db_session, a, "Calculus")

    slug = await _subject(db_session, b, "Calculus")

    assert slug == "calculus", (
        "a suffix here tells B that somebody already has a subject called Calculus"
    )


async def test_my_own_second_subject_is_still_suffixed(db_session: AsyncSession) -> None:
    """Per-owner uniqueness is still uniqueness.

    Without this, "scope the query" and "delete the query" look identical from the first test,
    and the second would leave one learner with two subjects nothing can tell apart.
    """
    a = await _learner(db_session)
    await _subject(db_session, a, "Calculus")

    assert await _subject(db_session, a, "Calculus") == "calculus_2"


async def test_a_curated_slug_dedups_against_curated_only(db_session: AsyncSession) -> None:
    """Approval picks a curated slug through this helper, so it must not see owned rows.

    If it did, the shared library's names would be pushed along by private subjects nobody
    reviewing a publication can even see — and the suffix would leak in the other direction.
    """
    a = await _learner(db_session)
    await _subject(db_session, a, "Calculus")

    slug = await svc.unique_subject_slug(db_session, "Calculus", owner_learner_id=None)

    assert slug == "calculus", "a learner's private subject must not push the curated slug along"


async def test_the_curated_filter_is_not_silently_matching_nothing(
    db_session: AsyncSession,
) -> None:
    """Guards the `.is_(None)` that the scoped query depends on.

    ``Subject.owner_learner_id == None`` compiles to ``= NULL``, which is never true, so the
    filter would match no rows and every curated name would look free. That failure is
    invisible until the second curated subject of the same name hits the partial unique index
    — here it is a plain assertion instead.
    """
    curated = await svc.create_subject(
        db_session,
        svc.SubjectCreate(slug="physics", name="Physics"),
        owner_learner_id=None,
    )
    assert curated.owner_learner_id is None

    assert (
        await svc.unique_subject_slug(db_session, "Physics", owner_learner_id=None) == "physics_2"
    )


async def test_a_curated_subject_does_suffix_mine(db_session: AsyncSession) -> None:
    """The deliberate asymmetry, and the reason it is not a leak.

    Every learner can already list the whole curated library, so de-duplicating against it
    tells them nothing they could not read directly — unlike another learner's private
    subject, which is what the first test in this file is about. Skipping it would leave a
    learner's own subject sharing a slug with a curated one in the same catalog, for no gain.
    """
    a = await _learner(db_session)
    await svc.create_subject(
        db_session, svc.SubjectCreate(slug="algebra", name="Algebra"), owner_learner_id=None
    )

    assert await _subject(db_session, a, "Algebra") == "algebra_2"
