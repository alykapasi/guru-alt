"""The keyword arm matches questions, not only exact phrases (review, 2026-11-16).

``plainto_tsquery`` joins every term with AND, so a question matched only passages containing
every word of it — "how does photosynthesis work in plants" needs "work" and "plant" in the
passage too. Hybrid retrieval's keyword arm was close to silent on natural questions; OR-ing
the terms lets it match, and ranking by cover density puts passages sharing more of them first.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.rag.retrieval import keyword_tsquery

CASES = [
    (
        "How does photosynthesis work in plants?",
        "Photosynthesis converts light energy into chemical energy inside chloroplasts.",
    ),
    (
        "what is an eigenvector of a matrix",
        "An eigenvector keeps its direction when the linear map is applied; it is only scaled.",
    ),
    (
        "Why do bonds lose value when interest rates rise?",
        "Bond prices fall as rates climb, because existing coupons look worse than new issues.",
    ),
]


@pytest.mark.parametrize(("question", "passage"), CASES)
async def test_a_question_matches_a_passage_answering_it(
    db_session: AsyncSession, question: str, passage: str
) -> None:
    matched = await db_session.scalar(
        select(func.to_tsvector("english", passage).op("@@")(keyword_tsquery(question)))
    )
    assert matched is True


async def test_a_passage_sharing_more_of_the_question_ranks_first(
    db_session: AsyncSession,
) -> None:
    q = keyword_tsquery("eigenvalue of a symmetric matrix")
    row = (
        await db_session.execute(
            select(
                func.ts_rank_cd(
                    func.to_tsvector("english", "A symmetric matrix has real eigenvalues."), q
                ),
                func.ts_rank_cd(func.to_tsvector("english", "Every matrix has a determinant."), q),
            )
        )
    ).one()
    assert row[0] > row[1] > 0


async def test_a_question_of_only_stopwords_matches_nothing(db_session: AsyncSession) -> None:
    matched = await db_session.scalar(
        select(
            func.to_tsvector("english", "Anything at all.").op("@@")(keyword_tsquery("what is it"))
        )
    )
    assert matched is False
