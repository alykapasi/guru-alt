"""CRUD over the knowledge graph.

Functions own their transactions (commit + refresh). Existence checks for clean 404s
live in the router; uniqueness/constraint violations surface as ``IntegrityError`` for
the router to translate into 409s.
"""

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.learning import prerequisites
from app.models.knowledge import KC, Concept, KCEdge, Subject, Topic
from app.models.learning import LearnerKCState
from app.models.source import Chunk, ChunkKC, Source
from app.schemas.knowledge import KCCreate, SubjectCreate, TopicCreate

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CurriculumResult:
    """The new subject, plus the sources whose scope this call invalidated.

    The caller needs the second list: moving a source between subjects makes its chunk KC tags
    describe the wrong graph, and rebuilding them is a model call per chunk — background work,
    not something to hold a curriculum-creation request open for.
    """

    subject: Subject
    reassigned_source_ids: list[uuid.UUID]


# --- Helpers ----------------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert text to a slug: lowercase, remove non-word chars, collapse spaces/hyphens to -."""
    text = text.strip().lower()
    # Replace non-word chars (except hyphens) with nothing
    text = re.sub(r"[^\w\s-]", "", text)
    # Collapse multiple spaces/hyphens to single hyphen
    text = re.sub(r"[-\s]+", "-", text)
    # Remove trailing hyphen
    text = text.rstrip("-")
    return text


def concept_key(name: str) -> str:
    """The canonical form of a KC name — the identity two presentations share (S24).

    Deliberately not ``_slugify``. That one keeps underscores and unicode word characters
    because it builds URL slugs, where preserving what the author typed is the point. This one
    is an *identity*, so it is narrower on purpose: two presentations should reach the same key
    despite differing in case and punctuation, and a key that varied with an underscore would
    fail at exactly the job it exists for.

    Mirrored in SQL by migration 0045's backfill. ``test_knowledge_concepts`` runs both over the
    same names and asserts they agree, because a backfill that canonicalises differently from
    the code would split every concept it touched.
    """
    text = re.sub(r"[^a-z0-9\s]", "", name.lower())
    return re.sub(r"\s+", "-", text).strip("-")


async def resolve_concept(session: AsyncSession, name: str) -> Concept | None:
    """The concept this name denotes, created if this is the first presentation of it.

    ``None`` when the name canonicalises to nothing — "???" or "" is not an identity, and
    giving every such KC one shared empty-string concept would claim they are all the same
    thing, which is the one answer that is certainly wrong.

    Flushes rather than commits: creating a KC and giving it an identity is one act, and a
    concept row committed beside a KC that then failed to save would leave an identity for a
    presentation that does not exist.
    """
    key = concept_key(name)
    if not key:
        return None
    existing = await session.scalar(select(Concept).where(Concept.key == key))
    if existing is not None:
        return existing
    concept = Concept(key=key, name=name)
    session.add(concept)
    await session.flush()
    return concept


async def subject_name_exists(
    session: AsyncSession, name: str, *, owner_learner_id: uuid.UUID | None
) -> bool:
    """Whether this owner already has a subject by this name (case-insensitive).

    Scoped to the owner, and that is the fix rather than a refinement (S25): the check was
    global, so the first learner to study Calculus took the name from every learner after
    them — a conflict on somebody else's private curriculum, reported as though they had
    made a mistake. A learner is still stopped from creating the same subject twice, which is
    the duplicate this was ever meant to catch, and a clash with a *curated* name is allowed
    because the two are different things: one is the shared library, the other is theirs.
    """
    result = await session.scalar(
        select(Subject)
        .where(
            func.lower(Subject.name) == name.strip().lower(),
            Subject.owner_learner_id == owner_learner_id
            if owner_learner_id is not None
            else Subject.owner_learner_id.is_(None),
        )
        .limit(1)
    )
    return result is not None


# --- Subjects ---------------------------------------------------------------


async def create_subject(
    session: AsyncSession, data: SubjectCreate, *, owner_learner_id: uuid.UUID | None = None
) -> Subject:
    """``owner_learner_id=None`` creates a *curated* subject: shared, and read-only through
    the learner API (S25). No route a learner can reach passes None."""
    subject = Subject(
        slug=data.slug,
        name=data.name,
        description=data.description,
        owner_learner_id=owner_learner_id,
    )
    session.add(subject)
    await session.commit()
    await session.refresh(subject)
    return subject


def _add_prerequisite_edges(
    session: AsyncSession,
    kc_by_key: dict[str, KC],
    named_keys: list[tuple[str, str]],
    wanted_edges: list[tuple[str, list[str]]],
) -> int:
    """Create the ``KCEdge`` rows a committed curriculum asks for, dropping what it cannot.

    Re-validated here even though curriculum parsing already resolved and de-cycled these.
    That pass runs on the model's output; this runs on a request body, which a client is free
    to have edited between the two — and the alternatives to validating are a foreign-key
    error that fails the whole commit, or a stored cycle that quietly corrupts every plan
    built from this subject afterwards.
    """
    index, _duplicated = prerequisites.index_by_name(named_keys)
    known = set(kc_by_key)
    edges: list[prerequisites.KeyEdge] = []
    for key, requires in wanted_edges:
        for raw in requires:
            # Committed payloads carry resolved keys; a name is accepted too, so a
            # hand-written or hand-edited curriculum can express prerequisites the same way
            # the model is asked to.
            target = raw if raw in known else index.get(prerequisites.normalise(raw))
            if target is None or target == key:
                continue
            edges.append((target, key))

    kept, dropped = prerequisites.acyclic(edges)
    if dropped:
        log.warning("knowledge.cyclic_prerequisites_dropped", count=len(dropped))
    seen: set[prerequisites.KeyEdge] = set()
    for prereq, dependent in kept:
        if (prereq, dependent) in seen:
            continue
        seen.add((prereq, dependent))
        session.add(KCEdge(prereq_kc_id=kc_by_key[prereq].id, kc_id=kc_by_key[dependent].id))
    return len(seen)


async def mark_source_derived(session: AsyncSession, subject_id: uuid.UUID | None) -> None:
    """Latch ``private_source_derived`` on a subject source material has reached (S25b D4).

    A latch, not a setter: every trigger sets it, nothing clears it, and calling this on a
    subject that already carries it is a no-op rather than an error — which is what lets each
    trigger call it without first asking whether one of the others got there already.

    ``subject_id=None`` does nothing, so a caller with an unscoped source does not have to
    branch. The update runs in the caller's transaction on purpose: a flag set in a later one
    is a window in which the subject is publishable.
    """
    if subject_id is None:
        return
    await session.execute(
        update(Subject)
        .where(Subject.id == subject_id, Subject.private_source_derived.is_(False))
        .values(private_source_derived=True)
    )


async def unique_subject_slug(
    session: AsyncSession, name: str, *, owner_learner_id: uuid.UUID | None
) -> str:
    """A slug free among the subjects sharing this owner (S25b D8).

    Scoped, not global. Counting every slug in the table made the de-duplication suffix an
    existence oracle: ask for a name a stranger privately used and the ``name_2`` you got back
    answered a question about their library, for any name worth trying. It also read every slug
    in the table to create one row.

    The scope is exactly what the creator can already see, which is the rule that makes the
    suffix say nothing new. For a learner that is their own subjects *plus* the curated ones:
    every learner can already list the shared library, so de-duplicating against it reveals
    nothing, and leaving it out would let a learner's subject sit in their catalog sharing a
    slug with a curated one for no gain. ``owner_learner_id=None`` — approval creating a
    published copy — scopes to curated subjects alone, because a curated slug must not be
    pushed along by a private subject nobody reviewing it can see.
    """
    owner = (
        # `== None` would compile to `= NULL`, which is never true: the filter would match
        # nothing, every curated name would look free, and the failure would stay invisible
        # until the second curated subject of one name hit the partial unique index.
        Subject.owner_learner_id.is_(None)
        if owner_learner_id is None
        else or_(
            Subject.owner_learner_id == owner_learner_id,
            Subject.owner_learner_id.is_(None),
        )
    )
    result = await session.execute(select(Subject.slug).where(owner))
    taken = {slug for (slug,) in result.all()}

    base = _slugify(name)
    slug, counter = base, 2
    while slug in taken:
        slug = f"{base}_{counter}"
        counter += 1
    return slug


async def create_subject_with_graph(
    session: AsyncSession,
    subject_name: str,
    subject_description: str | None,
    topics_data: list[dict],
    source_ids: list[uuid.UUID] | None,
    learner_id: uuid.UUID,
    private_source_derived: bool = False,
) -> CurriculumResult:
    """Create a Subject with Topics and KCs in one atomic transaction.

    Handles multi-level slug deduplication and reassigns owned sources.
    - Subject slug: unique per owner (S25b D8 — a global scan reported on other learners'
      private subject names through the suffix it returned; see `unique_subject_slug`)
    - Topic slug: unique within the subject (dedups within topics_data)
    - KC slug: unique within its topic (dedups within topic's kcs)

    Args:
        session: Database session
        subject_name: Name of the subject (will be slugified)
        subject_description: Optional subject description
        topics_data: List of dicts with "name", "description", "kcs" keys.
                     Each kc dict has "name" and "description".
        source_ids: Optional list of source IDs to reassign to this subject.
                    Only reassigns if owned by learner_id.
        learner_id: ID of the learner for source ownership check.

    Returns:
        The created Subject (with id set, relationships populated).
    """
    subject_slug = await unique_subject_slug(session, subject_name, owner_learner_id=learner_id)

    # Create the subject and flush to get its ID
    subject = Subject(
        slug=subject_slug,
        name=subject_name,
        description=subject_description,
        # A curriculum generated for a learner from their own goal is theirs (S25). Curated
        # subjects are created deliberately and carry NULL; nothing reaches this path.
        owner_learner_id=learner_id,
        # Decided by the caller from what the *server* observed — the proposal row, or the
        # presence of sources to move in. Never from anything the request body claims (S25b D4).
        private_source_derived=private_source_derived,
    )
    session.add(subject)
    await session.flush()

    # Deduplicate and create topics
    topic_seen_slugs: set[str] = set()
    # Prerequisites arrive as keys naming KCs anywhere in this payload, including topics not
    # created yet, so edges are built after the whole hierarchy exists.
    kc_by_key: dict[str, KC] = {}
    named_keys: list[tuple[str, str]] = []
    wanted_edges: list[tuple[str, list[str]]] = []
    for topic_data in topics_data:
        topic_name = topic_data.get("name", "")
        topic_desc = topic_data.get("description")
        kcs_data = topic_data.get("kcs", [])

        # Deduplicate topic slug within this subject
        base_topic_slug = _slugify(topic_name)
        topic_slug = base_topic_slug
        counter = 2
        while topic_slug in topic_seen_slugs:
            topic_slug = f"{base_topic_slug}_{counter}"
            counter += 1
        topic_seen_slugs.add(topic_slug)

        topic = Topic(
            subject_id=subject.id,
            slug=topic_slug,
            name=topic_name,
            description=topic_desc,
        )
        session.add(topic)
        await session.flush()

        # Deduplicate and create KCs within this topic
        kc_seen_slugs: set[str] = set()
        for kc_data in kcs_data:
            kc_name = kc_data.get("name", "")
            kc_desc = kc_data.get("description")

            # Deduplicate KC slug within this topic
            base_kc_slug = _slugify(kc_name)
            kc_slug = base_kc_slug
            counter = 2
            while kc_slug in kc_seen_slugs:
                kc_slug = f"{base_kc_slug}_{counter}"
                counter += 1
            kc_seen_slugs.add(kc_slug)

            # Resolved per KC rather than in a batch afterwards: a generated curriculum can
            # name the same concept in two of its own topics, and a batch would then race
            # itself into two rows for one key.
            concept = await resolve_concept(session, kc_name)
            kc = KC(
                topic_id=topic.id,
                slug=kc_slug,
                name=kc_name,
                description=kc_desc,
                concept_id=concept.id if concept else None,
            )
            session.add(kc)
            # `key` is assigned by curriculum parsing and carried through the review step.
            # A payload without one (a hand-built subject, or anything predating S22) simply
            # contributes no edges rather than failing.
            key = kc_data.get("key")
            if isinstance(key, str) and key and key not in kc_by_key:
                kc_by_key[key] = kc
                named_keys.append((key, kc_name))
                requires = kc_data.get("requires") or []
                if isinstance(requires, list):
                    wanted_edges.append((key, [r for r in requires if isinstance(r, str)]))
        await session.flush()

    _add_prerequisite_edges(session, kc_by_key, named_keys, wanted_edges)

    # Reassign owned sources to this subject.
    reassigned: list[uuid.UUID] = []
    if source_ids:
        for source_id in source_ids:
            source = await session.get(Source, source_id)
            if source is None or source.learner_id != learner_id:
                continue
            if source.subject_id == subject.id:
                continue  # nothing moved, so nothing derived from it is stale
            source.subject_id = subject.id
            # A topic from the old subject cannot describe a source in this one, and a source
            # scoped to a topic outside its subject is retrievable through neither filter.
            source.topic_id = None
            reassigned.append(source.id)
    # Chunk KC tags name KCs in the *previous* graph, so after a move they are worse than
    # absent: they assert this material teaches concepts it was never read against. Drop them
    # here, in the same transaction as the move, and let retagging rebuild them.
    if reassigned:
        await session.execute(
            delete(ChunkKC).where(
                ChunkKC.chunk_id.in_(select(Chunk.id).where(Chunk.source_id.in_(reassigned)))
            )
        )
        # Sources genuinely moved into this subject, whatever the caller believed when it
        # passed `private_source_derived`. The server saw it happen, so the server sets it.
        subject.private_source_derived = True

    # Single atomic commit
    await session.commit()
    # Refresh to get all relationships populated
    await session.refresh(subject)
    return CurriculumResult(subject=subject, reassigned_source_ids=reassigned)


class ScopeConflict(ValueError):
    """A source's scope is inconsistent, or names a subject or topic the caller cannot see.

    Raised both when a source is scoped to a topic that does not belong to its subject, and
    when the supplied subject or topic is missing or another learner's private one (S25). The
    message names only ids the caller sent.
    """


async def resolve_source_scope(
    session: AsyncSession,
    *,
    learner_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    topic_id: uuid.UUID | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Check a source's scope is internally consistent, filling in what it implies.

    A topic belongs to exactly one subject, so a source claiming both must agree with the
    graph. Nothing checked this: a source could sit in a topic from a different subject, and
    since retrieval filters on *both*, such a source was reachable through neither — indexed,
    embedded, paid for, and invisible.

    A topic given without a subject is not an error; the topic determines the subject, so it
    is filled in rather than rejected.

    Both ids pass the visibility gate first (S25). A stranger's private subject or topic is
    refused exactly like one that does not exist. Every message names only ids the caller sent:
    the old one named the subject a topic belongs to, which told anybody who uploaded against a
    stranger's topic whose curriculum it was.
    """
    if subject_id is not None:
        try:
            await require_visible_subject(session, subject_id, learner_id)
        except NotVisible as exc:
            raise ScopeConflict(f"subject {subject_id} does not exist") from exc
    if topic_id is None:
        return subject_id, None
    try:
        _, topic = await require_visible_topic(session, topic_id, learner_id)
    except NotVisible as exc:
        raise ScopeConflict(f"topic {topic_id} does not exist") from exc
    if subject_id is not None and topic.subject_id != subject_id:
        raise ScopeConflict(f"topic {topic_id} does not belong to subject {subject_id}")
    return topic.subject_id, topic_id


async def list_subjects(
    session: AsyncSession, *, learner_id: uuid.UUID | None = None
) -> Sequence[Subject]:
    """Curated subjects plus this learner's own (S25).

    ``learner_id=None`` returns everything and is for internal callers with no learner in
    hand — never for a request, which would put every learner's private curriculum in front
    of whoever asked.
    """
    statement = select(Subject).order_by(Subject.slug)
    if learner_id is not None:
        statement = statement.where(
            or_(Subject.owner_learner_id.is_(None), Subject.owner_learner_id == learner_id)
        )
    result = await session.scalars(statement)
    return result.all()


async def get_subject(session: AsyncSession, subject_id: uuid.UUID) -> Subject | None:
    return await session.get(Subject, subject_id)


def is_visible_to(subject: Subject, learner_id: uuid.UUID) -> bool:
    """Curated, or this learner's own. Anything else does not exist as far as they know."""
    return subject.owner_learner_id is None or subject.owner_learner_id == learner_id


def is_writable_by(subject: Subject, learner_id: uuid.UUID) -> bool:
    """Only the owner may change a subject's graph.

    A curated subject is deliberately writable by nobody through this API: it is the shared
    library, and letting any authenticated learner add components to it is how the shared
    library becomes one learner's notes.
    """
    return subject.owner_learner_id == learner_id


class NotVisible(LookupError):
    """A graph id that names nothing, or names another learner's private material (S25).

    One exception for both, on purpose. A caller who could tell them apart could map somebody
    else's curriculum one id at a time. ``kind`` is what the id was meant to name, and the
    message is the entire 404 body a route sends (see the handler in ``app.main``).
    """

    def __init__(self, kind: Literal["subject", "topic", "kc"]) -> None:
        super().__init__(f"{kind} not found")
        self.kind: Literal["subject", "topic", "kc"] = kind


async def require_visible_subject(
    session: AsyncSession, subject_id: uuid.UUID, learner_id: uuid.UUID
) -> Subject:
    """The subject, if ``learner_id`` may see it; otherwise ``NotVisible``."""
    subject = await session.get(Subject, subject_id)
    if subject is None or not is_visible_to(subject, learner_id):
        raise NotVisible("subject")
    return subject


async def require_visible_topic(
    session: AsyncSession, topic_id: uuid.UUID, learner_id: uuid.UUID
) -> tuple[Subject, Topic]:
    """The topic and the subject that decides its visibility, or ``NotVisible``."""
    row = (
        await session.execute(
            select(Subject, Topic)
            .join(Topic, Topic.subject_id == Subject.id)
            .where(Topic.id == topic_id)
        )
    ).first()
    if row is None or not is_visible_to(row[0], learner_id):
        raise NotVisible("topic")
    return row[0], row[1]


async def require_visible_kc(
    session: AsyncSession, kc_id: uuid.UUID, learner_id: uuid.UUID
) -> tuple[Subject, KC]:
    """The component and the subject that decides its visibility, or ``NotVisible``."""
    row = (
        await session.execute(
            select(Subject, KC)
            .join(Topic, Topic.subject_id == Subject.id)
            .join(KC, KC.topic_id == Topic.id)
            .where(KC.id == kc_id)
        )
    ).first()
    if row is None or not is_visible_to(row[0], learner_id):
        raise NotVisible("kc")
    return row[0], row[1]


async def subject_of_topic(session: AsyncSession, topic_id: uuid.UUID) -> Subject | None:
    """The subject a topic belongs to — the unit ownership is decided at."""
    return await session.scalar(
        select(Subject).join(Topic, Topic.subject_id == Subject.id).where(Topic.id == topic_id)
    )


async def subject_of_kc(session: AsyncSession, kc_id: uuid.UUID) -> Subject | None:
    """The subject a component belongs to, through its topic."""
    return await session.scalar(
        select(Subject)
        .join(Topic, Topic.subject_id == Subject.id)
        .join(KC, KC.topic_id == Topic.id)
        .where(KC.id == kc_id)
    )


# --- Topics -----------------------------------------------------------------


async def create_topic(session: AsyncSession, subject_id: uuid.UUID, data: TopicCreate) -> Topic:
    topic = Topic(
        subject_id=subject_id,
        slug=data.slug,
        name=data.name,
        description=data.description,
    )
    session.add(topic)
    await session.commit()
    await session.refresh(topic)
    return topic


async def list_topics(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[Topic]:
    result = await session.scalars(
        select(Topic).where(Topic.subject_id == subject_id).order_by(Topic.slug)
    )
    return result.all()


async def get_topic(session: AsyncSession, topic_id: uuid.UUID) -> Topic | None:
    return await session.get(Topic, topic_id)


# --- KCs --------------------------------------------------------------------


async def create_kc(session: AsyncSession, topic_id: uuid.UUID, data: KCCreate) -> KC:
    concept = await resolve_concept(session, data.name)
    kc = KC(
        topic_id=topic_id,
        slug=data.slug,
        name=data.name,
        description=data.description,
        concept_id=concept.id if concept else None,
    )
    session.add(kc)
    await session.commit()
    await session.refresh(kc)
    return kc


async def list_kcs(session: AsyncSession, topic_id: uuid.UUID) -> Sequence[KC]:
    result = await session.scalars(select(KC).where(KC.topic_id == topic_id).order_by(KC.slug))
    return result.all()


async def get_kc(session: AsyncSession, kc_id: uuid.UUID) -> KC | None:
    return await session.get(KC, kc_id)


async def list_kcs_for_subject(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KC]:
    """Every KC across a subject's topics (placement's candidate pool)."""
    result = await session.scalars(
        select(KC)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
        .order_by(Topic.slug, KC.slug)
    )
    return result.all()


async def list_root_kcs(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KC]:
    """KCs in a subject with no prerequisite from another KC in the same subject.

    The highest-leverage, most-foundational sample for a placement light test.
    """
    subject_kc_ids = (
        select(KC.id).join(Topic, KC.topic_id == Topic.id).where(Topic.subject_id == subject_id)
    )
    dependent_ids = select(KCEdge.kc_id).where(KCEdge.prereq_kc_id.in_(subject_kc_ids))
    result = await session.scalars(
        select(KC)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id, KC.id.notin_(dependent_ids))
        .order_by(Topic.slug, KC.slug)
    )
    return result.all()


# --- Prerequisite edges -----------------------------------------------------


async def would_create_cycle(
    session: AsyncSession, *, kc_id: uuid.UUID, prereq_kc_id: uuid.UUID
) -> bool:
    """Whether declaring ``prereq_kc_id`` a prerequisite of ``kc_id`` closes a cycle (S23).

    True exactly when ``kc_id`` already reaches ``prereq_kc_id`` by following prereq →
    dependent edges, because the new edge would then complete the loop.

    A recursive CTE rather than an in-memory walk, for two reasons. Edges are not confined to
    one subject — the schema permits a cross-subject prerequisite, so loading "the subject's
    edges" and checking those would miss exactly the case nobody expects. And ``UNION``
    (not ``UNION ALL``) makes the recursion terminate on a graph that is *already* cyclic,
    which is the state this check exists to stop growing.
    """
    reachable = (
        select(KCEdge.kc_id.label("node"))
        .where(KCEdge.prereq_kc_id == kc_id)
        .cte("reachable", recursive=True)
    )
    reachable = reachable.union(
        select(KCEdge.kc_id).join(reachable, KCEdge.prereq_kc_id == reachable.c.node)
    )
    hit = await session.scalar(
        select(reachable.c.node).where(reachable.c.node == prereq_kc_id).limit(1)
    )
    return hit is not None


async def add_prerequisite(
    session: AsyncSession, kc_id: uuid.UUID, prereq_kc_id: uuid.UUID, weight: float
) -> KCEdge:
    edge = KCEdge(kc_id=kc_id, prereq_kc_id=prereq_kc_id, weight=weight)
    session.add(edge)
    await session.commit()
    await session.refresh(edge)
    return edge


async def remove_prerequisite(
    session: AsyncSession, *, kc_id: uuid.UUID, prereq_kc_id: uuid.UUID
) -> bool:
    """Delete one prerequisite edge. Returns whether there was one to delete (S23).

    The repair path the conflict report had no counterpart for. Reporting a cycle without a way
    to act on it leaves the bad edge in the database and the ordering unjustified for as long
    as anyone takes to open a psql prompt.

    **Removal needs no cycle check, and that asymmetry is the point.** Deleting an edge removes
    a constraint, and removing constraints cannot create a cycle — so this is always safe in a
    way that adding an edge is not. It is also why the system offers removal and not repair: it
    can delete an edge a *person* names, and it cannot choose which edge of a cycle is the
    wrong one, because a cycle means two components each claim to come first and the graph does
    not know which claim is mistaken.

    Idempotent: deleting an edge that is not there is not an error, so a client retrying after
    a dropped response gets the same answer as one that succeeded.
    """
    edge = await session.scalar(
        select(KCEdge).where(KCEdge.kc_id == kc_id, KCEdge.prereq_kc_id == prereq_kc_id)
    )
    if edge is None:
        return False
    await session.delete(edge)
    await session.commit()
    return True


async def list_prerequisites(session: AsyncSession, kc_id: uuid.UUID) -> Sequence[KCEdge]:
    """This KC's direct prerequisites, in declaration order.

    Ordered for the same reason ``list_edges_for_subject`` is (S23): a prerequisite detour
    picks the *first* outstanding one (S11), so an unordered scan would let the query planner
    choose where a stuck learner is sent. Same caveat — edges written in one transaction tie
    on the clock and fall through to the primary key.
    """
    result = await session.scalars(
        select(KCEdge).where(KCEdge.kc_id == kc_id).order_by(KCEdge.created_at, KCEdge.id)
    )
    return result.all()


async def get_kcs(session: AsyncSession, kc_ids: Sequence[uuid.UUID]) -> Sequence[KC]:
    """Several KCs by id, in one query. Order is unspecified — callers key by id."""
    if not kc_ids:
        return []
    return (await session.scalars(select(KC).where(KC.id.in_(list(kc_ids))))).all()


async def list_edges_for_subject(session: AsyncSession, subject_id: uuid.UUID) -> Sequence[KCEdge]:
    """Every prerequisite edge within a subject — the lesson plan's in-memory closure/topo-sort
    needs the whole edge set at once rather than one KC at a time.

    Ordered, and load-bearing (S23). ``prerequisites.acyclic`` resolves a cycle by dropping
    whichever edge closes it *given the order it sees*, so an unordered scan would let the
    query planner decide which prerequisite gets sacrificed — and two regenerations of the
    same plan could then honour different constraints.

    ``created_at`` first, then the primary key. Be clear about what each half buys: edges
    added one at a time through the API separate properly on the timestamp, and that is the
    path cycles actually arrive by, since a generated curriculum is de-cycled at parse time
    before anything is stored. Edges written in one transaction all tie — ``created_at`` is
    the transaction clock — and fall through to a random UUID. That settles the order, which
    is what the validation needs; it does not make it meaningful.
    """
    result = await session.scalars(
        select(KCEdge)
        .join(KC, KCEdge.kc_id == KC.id)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
        .order_by(KCEdge.created_at, KCEdge.id)
    )
    return result.all()


@dataclass(frozen=True)
class SacrificedEdge:
    """A prerequisite the stored graph declares that planning cannot honour (S23)."""

    prereq_kc_id: uuid.UUID
    prereq_slug: str
    prereq_name: str
    kc_id: uuid.UUID
    kc_slug: str
    kc_name: str


async def sacrificed_prerequisites(
    session: AsyncSession, subject_id: uuid.UUID
) -> list[SacrificedEdge]:
    """The edges plan generation has to drop for this subject to admit an order at all.

    Plan generation already survives a stored cycle: it runs the edge set through
    ``prerequisites.acyclic``, drops whichever edges close a ring, and orders by what remains
    (see ``app.services.lesson_plan``). That is the right behaviour — refusing to plan would
    strand the learner over an edge they did not create — but it was only ever written to a
    log. A subject whose dependencies were quietly sacrificed looked exactly like one with
    none, so nothing could tell a learner their ordering was weakened, and nothing could tell
    an operator which edge to go and fix.

    This answers that question without changing any of it. It loads the same edges in the same
    order and applies the same rule, so what it reports is what planning actually dropped
    rather than a second opinion that could disagree with it — the two would drift the moment
    they were computed differently.

    Read-only by design. Removing the edge is a curriculum decision with no obviously correct
    side: the cycle means two components each claim to come first, and which claim is wrong is
    not something the graph knows. Reporting it puts that decision in front of someone who can
    make it.
    """
    edges = await list_edges_for_subject(session, subject_id)
    _, dropped = prerequisites.acyclic([(e.prereq_kc_id, e.kc_id) for e in edges])
    if not dropped:
        return []

    # Every node of a reported ring is necessarily inside this subject, because
    # ``list_edges_for_subject`` loads only edges whose *dependent* is in it: a ring needs
    # each node to appear as a dependent, so a ring closing through a KC elsewhere never
    # reaches this edge set at all. That is a real limit rather than a lost case — planning
    # loads the same set, so such a constraint is not one it honours and then breaks; it is
    # one neither subject's ordering ever sees. Enforcing across subjects needs the concept
    # identity S24 covers. The lookup below still tolerates a missing row so a KC deleted
    # between the two queries degrades to a named gap instead of a crash.
    named = {
        kc.id: kc
        for kc in await session.scalars(
            select(KC).where(KC.id.in_({kc_id for edge in dropped for kc_id in edge}))
        )
    }
    sacrificed: list[SacrificedEdge] = []
    for prereq_id, kc_id in dropped:
        prereq, dependent = named.get(prereq_id), named.get(kc_id)
        sacrificed.append(
            SacrificedEdge(
                prereq_kc_id=prereq_id,
                prereq_slug=prereq.slug if prereq else "",
                prereq_name=prereq.name if prereq else "(unknown component)",
                kc_id=kc_id,
                kc_slug=dependent.slug if dependent else "",
                kc_name=dependent.name if dependent else "(unknown component)",
            )
        )
    return sacrificed


@dataclass(frozen=True)
class ForeignPrerequisite:
    """A prerequisite this subject declares on a component another subject owns (S24).

    The schema permits these and ``POST /kcs/{id}/prerequisites`` creates them, so they are a
    real state of the graph rather than a hypothetical one.
    """

    prereq_kc_id: uuid.UUID
    prereq_name: str
    prereq_subject_id: uuid.UUID
    prereq_subject_name: str
    kc_id: uuid.UUID
    kc_name: str
    # Whether the learner has any presentation of that concept in their history. Not "has
    # mastered it" — see ``Concept``: sharing a canonical name is evidence about names.
    met_elsewhere: bool


async def cross_subject_prerequisites(
    session: AsyncSession, subject_id: uuid.UUID, *, learner_id: uuid.UUID | None = None
) -> list[ForeignPrerequisite]:
    """Prerequisites of this subject's components that live in another subject.

    **The policy, stated.** A lesson plan is a sequence of one subject's components, so a
    prerequisite outside it cannot be ordered inside it — there is no step that could teach it.
    Planning therefore drops these edges, and until now it dropped them into a `TypeError`: the
    foreign component reached the topological sort, which had no tiebreak entry for it, and
    comparing that fallback against the integers used for local components raised. A
    cross-subject prerequisite did not order the plan badly, it stopped the plan existing.

    So the edge is dropped deliberately and reported here, which is the same shape S23 settled
    on for cycles and for the same reason: which of two subjects should absorb the other's
    component is a curriculum decision, and the graph does not know it.

    ``learner_id`` adds the one fact that makes the report actionable — whether this learner has
    met that concept at all. It is deliberately *not* "has mastered it": two presentations share
    a concept on the evidence of their names (see ``Concept``), and treating that as transferred
    mastery would silently stop teaching something the learner has never seen.
    """
    edges = await list_edges_for_subject(session, subject_id)
    if not edges:
        return []
    local = {kc.id for kc in await list_kcs_for_subject(session, subject_id)}
    foreign_ids = {e.prereq_kc_id for e in edges if e.prereq_kc_id not in local}
    if not foreign_ids:
        return []

    rows = (
        await session.execute(
            select(KC, Subject)
            .join(Topic, KC.topic_id == Topic.id)
            .join(Subject, Topic.subject_id == Subject.id)
            .where(KC.id.in_(foreign_ids | {e.kc_id for e in edges}))
        )
    ).all()
    named = {kc.id: (kc, subject) for kc, subject in rows}

    met: set[uuid.UUID] = set()
    if learner_id is not None:
        concept_ids = {kc.concept_id for kc, _ in named.values() if kc.concept_id is not None}
        if concept_ids:
            found = await session.scalars(
                select(KC.concept_id)
                .join(LearnerKCState, LearnerKCState.kc_id == KC.id)
                .where(
                    LearnerKCState.learner_id == learner_id,
                    KC.concept_id.in_(concept_ids),
                    # The foreign component itself does not count as meeting the concept
                    # elsewhere — that would report every prerequisite the learner has so
                    # much as been estimated on as already met.
                    KC.id.notin_(foreign_ids),
                )
                .distinct()
            )
            met = {cid for cid in found.all() if cid is not None}

    out: list[ForeignPrerequisite] = []
    for edge in edges:
        if edge.prereq_kc_id in local:
            continue
        prereq = named.get(edge.prereq_kc_id)
        dependent = named.get(edge.kc_id)
        if prereq is None or dependent is None:
            continue  # deleted between the two queries; a gap, not a crash
        prereq_kc, prereq_subject = prereq
        out.append(
            ForeignPrerequisite(
                prereq_kc_id=prereq_kc.id,
                prereq_name=prereq_kc.name,
                prereq_subject_id=prereq_subject.id,
                prereq_subject_name=prereq_subject.name,
                kc_id=dependent[0].id,
                kc_name=dependent[0].name,
                met_elsewhere=prereq_kc.concept_id is not None and prereq_kc.concept_id in met,
            )
        )
    return out


async def subjects_for_kcs(session: AsyncSession, kc_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """Distinct subject ids that own any of ``kc_ids`` (KC -> Topic -> Subject)."""
    kc_ids = list(kc_ids)
    if not kc_ids:
        return set()
    result = await session.scalars(
        select(Topic.subject_id)
        .join(KC, KC.topic_id == Topic.id)
        .where(KC.id.in_(kc_ids))
        .distinct()
    )
    return set(result.all())


@dataclass(frozen=True)
class KCCoverage:
    """How much of a learner's own library is tagged to one KC."""

    kc_id: uuid.UUID
    slug: str
    name: str
    chunk_count: int


async def kc_coverage(
    session: AsyncSession, *, learner_id: uuid.UUID, subject_id: uuid.UUID
) -> list[KCCoverage]:
    """Per-KC count of the learner's chunks tagged to it, zeros included.

    Ingestion pays a FAST model call per chunk to write ``ChunkKC`` rows, and until this
    nothing read them — the tags were a recurring bill with no consumer (S55). This is the
    cheapest honest consumer: it answers "can this KC be taught from what the learner actually
    uploaded, or only from the model's own knowledge?", which is a question the planner and
    the learner both have, and it does it without letting an unvalidated signal touch
    retrieval ranking. Whether tags *improve retrieval* is a separate question that needs a
    real corpus to answer, and is deliberately not assumed here.

    Zero-coverage KCs are kept: a gap in the library is the more actionable half of the answer.
    """
    # A correlated subquery rather than a chain of outer joins: with joins, a KC covered only
    # by *another* learner's chunks produced a row that the ownership filter then removed,
    # taking the KC out of the report entirely instead of showing it as uncovered. Counting
    # per KC keeps every KC unconditionally, which is the whole point.
    covered = (
        select(func.count(func.distinct(Chunk.id)))
        .select_from(ChunkKC)
        .join(Chunk, Chunk.id == ChunkKC.chunk_id)
        .join(Source, Source.id == Chunk.source_id)
        .where(ChunkKC.kc_id == KC.id, Source.learner_id == learner_id)
        .correlate(KC)
        .scalar_subquery()
    )
    rows = await session.execute(
        select(KC.id, KC.slug, KC.name, covered)
        .join(Topic, KC.topic_id == Topic.id)
        .where(Topic.subject_id == subject_id)
        .order_by(KC.slug)
    )
    return [
        KCCoverage(kc_id=kc_id, slug=slug, name=name, chunk_count=count)
        for kc_id, slug, name, count in rows.all()
    ]
