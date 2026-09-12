"""A prerequisite order the graph does not justify is worse than no order (S23).

Two holes, at opposite ends. The direct endpoint refused a KC declaring *itself* a
prerequisite and nothing longer, so a client could assemble any cycle one individually valid
edge at a time. And ``topo_sort`` did not fail on a cyclic graph — it appended whatever it
could not place, in tiebreak order, which reads downstream exactly like a real ordering. The
learner is then taught a component before the thing it was declared to depend on, and nothing
anywhere reports it.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import prerequisites
from app.learning.lesson_plan import CyclicPrerequisites, Edge, topo_sort
from app.llm.registry import fake_llm_client
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.services import knowledge as knowledge_svc
from app.services import lesson_plan as lesson_plan_svc

API = "/api/v1"
T0 = datetime(2026, 6, 1, 9, 0)  # naive: created_at is TIMESTAMP WITHOUT TIME ZONE


# --- the same rule, now over KC ids ------------------------------------------


def test_the_cycle_rule_is_the_same_one_the_curriculum_parser_uses() -> None:
    """Generic over node type on purpose: two places have to apply this rule and must not
    drift. The parser sees proposal keys before anything is stored; plan generation sees KC
    ids from a graph that may predate every check."""
    a, b, c = (uuid.uuid4() for _ in range(3))
    kept, dropped = prerequisites.acyclic([(a, b), (b, c), (c, a)])
    assert kept == [(a, b), (b, c)]
    assert dropped == [(c, a)]


# --- the endpoint ------------------------------------------------------------


async def _subject_with_kcs(client: AsyncClient, names: list[str]) -> list[str]:
    slug = f"s-{uuid.uuid4().hex[:8]}"
    r = await client.post(f"{API}/subjects", json={"slug": slug, "name": slug})
    assert r.status_code == 201
    subject_id = r.json()["id"]
    r = await client.post(f"{API}/subjects/{subject_id}/topics", json={"slug": "t", "name": "T"})
    topic_id = r.json()["id"]
    ids = []
    for name in names:
        r = await client.post(f"{API}/topics/{topic_id}/kcs", json={"slug": name, "name": name})
        assert r.status_code == 201
        ids.append(r.json()["id"])
    return ids


async def _link(client: AsyncClient, *, kc: str, prereq: str):
    return await client.post(f"{API}/kcs/{kc}/prerequisites", json={"prereq_kc_id": prereq})


async def test_two_edges_cannot_be_walked_into_a_cycle(api_client: AsyncClient) -> None:
    a, b = await _subject_with_kcs(api_client, ["a", "b"])
    assert (await _link(api_client, kc=b, prereq=a)).status_code == 201
    refused = await _link(api_client, kc=a, prereq=b)
    assert refused.status_code == 409


async def test_a_longer_ring_is_refused_too(api_client: AsyncClient) -> None:
    # The case that motivated this: each of these three edges is unremarkable on its own,
    # and the self-loop check never sees a KC pointing at itself.
    a, b, c = await _subject_with_kcs(api_client, ["a", "b", "c"])
    assert (await _link(api_client, kc=b, prereq=a)).status_code == 201
    assert (await _link(api_client, kc=c, prereq=b)).status_code == 201
    assert (await _link(api_client, kc=a, prereq=c)).status_code == 409


async def test_a_diamond_is_not_a_cycle(api_client: AsyncClient) -> None:
    # Two paths reaching the same component is ordinary structure, and a reachability check
    # that refused it would make the endpoint useless for real curricula.
    a, b, c, d = await _subject_with_kcs(api_client, ["a", "b", "c", "d"])
    assert (await _link(api_client, kc=b, prereq=a)).status_code == 201
    assert (await _link(api_client, kc=c, prereq=a)).status_code == 201
    assert (await _link(api_client, kc=d, prereq=b)).status_code == 201
    assert (await _link(api_client, kc=d, prereq=c)).status_code == 201


async def test_a_self_prerequisite_is_still_the_other_kind_of_error(
    api_client: AsyncClient,
) -> None:
    """400, not 409, and the split is deliberate: a self-loop is wrong in isolation, while a
    longer cycle is only wrong against the graph that happens to be stored."""
    (a,) = await _subject_with_kcs(api_client, ["a"])
    assert (await _link(api_client, kc=a, prereq=a)).status_code == 400


async def test_a_cycle_that_leaves_the_subject_is_still_a_cycle(
    api_client: AsyncClient,
) -> None:
    """The schema permits a cross-subject edge, so a check scoped to one subject's edges
    would miss precisely the case nobody is watching for."""
    (a,) = await _subject_with_kcs(api_client, ["a"])
    (b,) = await _subject_with_kcs(api_client, ["b"])
    assert (await _link(api_client, kc=b, prereq=a)).status_code == 201
    assert (await _link(api_client, kc=a, prereq=b)).status_code == 409


async def test_the_check_terminates_on_a_graph_that_is_already_cyclic(
    db_session: AsyncSession,
) -> None:
    """The state this exists to stop growing. A recursive walk over a cyclic graph has to be
    the deduplicating kind or it never returns, and a hung request is not a better outcome
    than a bad order."""
    _, kcs = await _graph(db_session, 3)
    a, b, c = kcs
    db_session.add_all(
        [
            KCEdge(prereq_kc_id=a.id, kc_id=b.id),
            KCEdge(prereq_kc_id=b.id, kc_id=a.id),
        ]
    )
    await db_session.flush()

    assert await knowledge_svc.would_create_cycle(db_session, kc_id=a.id, prereq_kc_id=b.id)
    assert not await knowledge_svc.would_create_cycle(db_session, kc_id=c.id, prereq_kc_id=a.id)


# --- ordering ----------------------------------------------------------------


def test_ordering_a_cyclic_graph_raises_instead_of_guessing() -> None:
    x, y = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(CyclicPrerequisites):
        topo_sort([x, y], [Edge(prereq_kc_id=x, kc_id=y), Edge(prereq_kc_id=y, kc_id=x)], {})


async def _graph(session: AsyncSession, n: int) -> tuple[Subject, list[KC]]:
    subject = Subject(slug=f"s-{uuid.uuid4().hex[:8]}", name="S")
    session.add(subject)
    await session.flush()
    topic = Topic(subject_id=subject.id, slug="t", name="T")
    session.add(topic)
    await session.flush()
    kcs = [KC(topic_id=topic.id, slug=f"k{i}", name=f"K{i}") for i in range(n)]
    session.add_all(kcs)
    await session.flush()
    return subject, kcs


async def _edges(session: AsyncSession, pairs: list[tuple[KC, KC]]) -> None:
    """Edges with distinct, increasing creation times, in the order given."""
    for i, (prereq, dependent) in enumerate(pairs):
        session.add(
            KCEdge(
                prereq_kc_id=prereq.id,
                kc_id=dependent.id,
                created_at=T0 + timedelta(minutes=i),
            )
        )
    await session.flush()


async def _learner(session: AsyncSession) -> Learner:
    learner = Learner(handle=f"g-{uuid.uuid4().hex[:8]}")
    session.add(learner)
    await session.flush()
    return learner


async def _plan_order(session: AsyncSession, learner: Learner, subject: Subject) -> list[str]:
    plan = await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner.id, subject_id=subject.id, goal=None
    )
    return list(plan.objective_kc_ids)


async def test_a_stored_cycle_no_longer_costs_the_learner_their_plan(
    db_session: AsyncSession,
) -> None:
    """Edges predating the endpoint check still exist, and ``topo_sort`` now refuses them —
    so plan generation has to resolve the cycle itself, or one legacy bad edge would stop a
    subject producing a plan at all.

    Timestamps are explicit because which edge gets dropped follows the order edges are read
    in, and that is the whole reason the query grew an ``ORDER BY``. Written as one
    ``add_all`` they would share a ``created_at`` — it is the transaction clock — and fall
    through to a random primary key, which is stable per row but arbitrary, and would have
    made this test pass or fail by luck.
    """
    learner = await _learner(db_session)
    subject, (a, b, c) = await _graph(db_session, 3)
    await _edges(db_session, [(a, b), (b, c), (c, a)])  # the last one closes the ring

    order = await _plan_order(db_session, learner, subject)
    assert set(order) == {str(a.id), str(b.id), str(c.id)}
    # The two declared first are honoured; the one that closed the ring is what gives way.
    assert order.index(str(a.id)) < order.index(str(b.id))
    assert order.index(str(b.id)) < order.index(str(c.id))


async def test_which_prerequisite_gives_way_does_not_change_between_regenerations(
    db_session: AsyncSession,
) -> None:
    """Dropping an edge is a real teaching decision, so it must not be re-made differently
    every time the plan is rebuilt — the learner would be taught a different order for the
    same graph."""
    learner = await _learner(db_session)
    subject, (a, b, c) = await _graph(db_session, 3)
    await _edges(db_session, [(a, b), (b, c), (c, a)])

    assert await _plan_order(db_session, learner, subject) == await _plan_order(
        db_session, learner, subject
    )


async def test_an_honest_graph_is_ordered_by_every_edge_it_has(
    db_session: AsyncSession,
) -> None:
    learner = await _learner(db_session)
    subject, (a, b, c) = await _graph(db_session, 3)
    await _edges(db_session, [(c, b), (b, a)])

    order = await _plan_order(db_session, learner, subject)
    assert order == [str(c.id), str(b.id), str(a.id)]


# --- reporting the edge that gave way (S23) -----------------------------------


async def test_a_subject_with_no_cycle_reports_nothing_sacrificed(
    db_session: AsyncSession,
) -> None:
    """The ordinary answer. An empty list is the claim that the order the learner is taught
    in is justified by every prerequisite the graph declares."""
    subject, (a, b, c) = await _graph(db_session, 3)
    await _edges(db_session, [(a, b), (b, c)])

    assert await knowledge_svc.sacrificed_prerequisites(db_session, subject.id) == []


async def test_the_edge_planning_dropped_is_the_edge_reported(
    db_session: AsyncSession,
) -> None:
    """The report has to agree with what planning actually did, not offer a second opinion.
    Both read the same ordered edge set and apply the same rule, so the edge named here is
    the one the learner's order failed to honour."""
    learner = await _learner(db_session)
    subject, (a, b, c) = await _graph(db_session, 3)
    await _edges(db_session, [(a, b), (b, c), (c, a)])  # the last closes the ring

    sacrificed = await knowledge_svc.sacrificed_prerequisites(db_session, subject.id)
    assert len(sacrificed) == 1
    assert (sacrificed[0].prereq_kc_id, sacrificed[0].kc_id) == (c.id, a.id)

    # And it is genuinely the constraint the plan broke: a is taught before c, though the
    # dropped edge declared c a prerequisite of a.
    order = await _plan_order(db_session, learner, subject)
    assert order.index(str(a.id)) < order.index(str(c.id))


async def test_the_report_names_the_components_not_just_their_ids(
    db_session: AsyncSession,
) -> None:
    """An operator reading this has to be able to act on it. Two UUIDs do not say which
    piece of curriculum is wrong."""
    subject, (a, b) = await _graph(db_session, 2)
    await _edges(db_session, [(a, b), (b, a)])

    (edge,) = await knowledge_svc.sacrificed_prerequisites(db_session, subject.id)
    assert (edge.prereq_name, edge.prereq_slug) == (b.name, b.slug)
    assert (edge.kc_name, edge.kc_slug) == (a.name, a.slug)


async def test_a_ring_closing_through_another_subject_is_not_reported_here(
    db_session: AsyncSession,
) -> None:
    """Pinning the boundary, because it is easy to read this endpoint as stronger than it is.

    The subject's edge view holds only edges whose *dependent* lives in it, and a ring needs
    every node to appear as a dependent — so a ring closing through a KC in another subject
    never reaches this edge set. Nothing is reported, and nothing was dropped: planning loads
    exactly the same set, so this is a constraint neither subject's ordering ever enforced,
    not one that was honoured and then quietly sacrificed. Crossing that boundary needs the
    concept identity S24 covers.
    """
    subject, (a, b) = await _graph(db_session, 2)
    _, (outsider,) = await _graph(db_session, 1)
    await _edges(db_session, [(a, b), (b, outsider), (outsider, a)])

    assert await knowledge_svc.sacrificed_prerequisites(db_session, subject.id) == []


async def test_the_endpoint_reports_the_conflict_to_the_learner(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The whole point of the item: a weakened ordering was visible only in a log line."""
    subject, (a, b) = await _graph(db_session, 2)
    await _edges(db_session, [(a, b), (b, a)])
    await db_session.commit()

    r = await api_client.get(f"{API}/subjects/{subject.id}/prerequisite-conflicts")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["prereq_kc_id"] == str(b.id)
    assert body[0]["kc_id"] == str(a.id)


async def test_the_endpoint_404s_on_a_subject_that_does_not_exist(
    api_client: AsyncClient,
) -> None:
    r = await api_client.get(f"{API}/subjects/{uuid.uuid4()}/prerequisite-conflicts")
    assert r.status_code == 404
