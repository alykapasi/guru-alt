"""The concept-link judge: what it sees, what it concludes, how it fails (S24)."""

from typing import cast

from app.learning import link_judge
from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.registry import fake_llm_client
from app.models.knowledge import KCEdge
from app.services import concept_links as svc
from tests.test_concept_links import _concept, _kc, _learner, _subject


def test_a_verdict_is_read_out_of_the_models_reply() -> None:
    assert link_judge.parse_verdict('{"verdict": "endorse", "reason": "Same rule."}') == (
        link_judge.Verdict(endorse=True, reason="Same rule.")
    )
    assert link_judge.parse_verdict('noise {"verdict": "reject", "reason": "Different."} tail') == (
        link_judge.Verdict(endorse=False, reason="Different.")
    )


def test_an_unreadable_reply_is_no_verdict_rather_than_a_rejection() -> None:
    for bad in ("", "sure!", '{"verdict": "maybe"}', '{"verdict": "endorse"}', "[1]"):
        assert link_judge.parse_verdict(bad) is None


async def _private_pair(db_session):
    learner = await _learner(db_session)
    concept = await _concept(db_session, "derivatives")
    own = await _subject(db_session, name="My Physics", owner=learner.id)
    lib = await _subject(db_session, name="Calculus")
    a = await _kc(db_session, own, "Derivatives", concept)
    b = await _kc(db_session, lib, "Derivatives", concept)
    return learner, a, b


async def test_an_endorsement_is_recorded_with_its_reason(db_session) -> None:
    learner, _a, _b = await _private_pair(db_session)
    llm = fake_llm_client(
        script=[FakeTurn(text='{"verdict": "endorse", "reason": "Same rate of change."}')]
    )
    assert await svc.judge_pending(db_session, llm, learner.id) == 1
    (link,) = await svc.links_for_learner(db_session, learner.id)
    assert (link.verdict, link.endorsed_by, link.reason) == (
        "endorsed",
        "judge",
        "Same rate of change.",
    )


async def test_a_failed_judgement_leaves_the_pair_for_the_next_run(db_session) -> None:
    learner, _a, _b = await _private_pair(db_session)
    assert (
        await svc.judge_pending(
            db_session, fake_llm_client(script=[FakeTurn(text="??")]), learner.id
        )
        == 0
    )
    (link,) = await svc.links_for_learner(db_session, learner.id)
    assert link.verdict is None
    llm = fake_llm_client(script=[FakeTurn(text='{"verdict": "reject", "reason": "Different."}')])
    assert await svc.judge_pending(db_session, llm, learner.id) == 1


async def test_the_judge_is_told_nothing_from_another_learners_material(db_session) -> None:
    learner, _a, b = await _private_pair(db_session)
    stranger = await _learner(db_session)
    other = await _subject(db_session, name="Stranger's notes", owner=stranger.id)
    secret = await _kc(db_session, other, "Secret prerequisite", None)
    db_session.add(KCEdge(prereq_kc_id=secret.id, kc_id=b.id))  # a hand-made edge into the library
    await db_session.flush()
    llm = fake_llm_client(script=[FakeTurn(text='{"verdict": "endorse", "reason": "ok"}')])
    await svc.judge_pending(db_session, llm, learner.id)
    fake = cast(FakeProvider, llm._providers["fake"])
    system, messages = fake.prompts_sent[-1]
    prompt = f"{system}\n{messages}"
    assert "Secret prerequisite" not in prompt
    assert "Stranger's notes" not in prompt
