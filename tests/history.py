"""A learner's history at a stated size, seeded in bulk (S62).

Set-based SQL rather than the services: the report's power user has a hundred thousand
messages, and seeding them one service call at a time would take longer than everything it
measures. The shapes are what the services would have written — the columns the readers read
— not a replay of how they got there. Embeddings are random vectors in the fake client's
space, so no model is called.
"""

import json
import uuid
from dataclasses import dataclass, replace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.registry import fake_llm_client
from app.models.chat import Conversation
from app.services import lesson_plan as lesson_plan_svc
from tests.embedding import FAKE_SPACE

YEAR_SECONDS = 365 * 86400
NOTE_ATOMS = [
    {
        "id": "a-1",
        "kind": "concept",
        "kc_ids": [],
        "md": "Vectors add tip-to-tail.",
        "provenance": {},
    }
]


@dataclass(frozen=True)
class HistoryShape:
    conversations: int = 5
    messages_per_conversation: int = 40
    subjects: int = 1
    topics_per_subject: int = 4
    kcs_per_topic: int = 5
    events: int = 150
    memories: int = 30
    sources: int = 4
    chunks_per_source: int = 15
    note_revisions: int = 3
    llm_calls: int = 60
    vector_pool: int = 0
    """0: a fresh random vector per chunk and memory (what the report needs, for a realistic
    vector index). N: reuse N vectors, which is all a statement-and-row budget needs and is
    most of the seeding time saved."""

    def scaled(self, k: int) -> "HistoryShape":
        """``k`` times the history, on the same graph: history is evidence, not syllabus."""
        return replace(
            self,
            conversations=self.conversations * k,
            messages_per_conversation=self.messages_per_conversation * k,
            events=self.events * k,
            memories=self.memories * k,
            sources=self.sources * k,
            note_revisions=self.note_revisions * k,
            llm_calls=self.llm_calls * k,
        )


SMALL = HistoryShape()
POWER_USER = HistoryShape(
    conversations=2000,
    messages_per_conversation=50,
    subjects=15,
    topics_per_subject=30,
    kcs_per_topic=10,
    events=75_000,
    memories=15_000,
    sources=750,
    chunks_per_source=20,
    note_revisions=10,
    llm_calls=150_000,
)
ORDINARY = HistoryShape(
    conversations=200,
    messages_per_conversation=50,
    subjects=2,
    topics_per_subject=30,
    kcs_per_topic=10,
    events=7_500,
    memories=1_500,
    sources=75,
    chunks_per_source=20,
    note_revisions=10,
    llm_calls=15_000,
)


@dataclass(frozen=True)
class SeededHistory:
    learner_id: uuid.UUID
    subject_id: uuid.UUID  # the first subject; it has a lesson plan
    topic_id: uuid.UUID  # a topic of that subject with a note
    conversation_id: uuid.UUID  # the newest conversation, on the first subject
    practice_conversation_id: uuid.UUID  # empty, on the first subject


def _vector(var: str, pool: int) -> str:
    """The SQL for one embedding, per row or from the pool (see ``HistoryShape.vector_pool``)."""
    if pool:
        return f"(SELECT v FROM s62_vector_pool WHERE i = {var} % {pool})"
    # The inner column keeps the aggregate inside the subquery; the outer one re-runs it per row.
    return (
        f"(SELECT array_agg(random() - 0.5 + 0 * d + 0 * {var}) "
        "FROM generate_series(1, :dim) d)::vector"
    )


async def _ids(session: AsyncSession, sql: str, **params: object) -> list[uuid.UUID]:
    return list((await session.execute(text(sql), params)).scalars().all())


async def seed_history(
    session: AsyncSession, learner_id: uuid.UUID, shape: HistoryShape
) -> SeededHistory:
    """Seed ``shape`` for an existing learner and commit. Timestamps spread over the past year."""
    dim = get_settings().embed_dim
    p: dict[str, object] = {"learner": learner_id, "space": FAKE_SPACE, "dim": dim}
    if shape.vector_pool:
        await session.execute(
            text("CREATE TEMP TABLE IF NOT EXISTS s62_vector_pool (i int PRIMARY KEY, v vector)")
        )
        await session.execute(text("TRUNCATE s62_vector_pool"))
        await session.execute(
            text(
                "INSERT INTO s62_vector_pool (i, v) SELECT i, "
                "(SELECT array_agg(random() - 0.5 + 0 * d + 0 * i) "
                "FROM generate_series(1, :dim) d)::vector FROM generate_series(0, :n - 1) i"
            ),
            {"dim": dim, "n": shape.vector_pool},
        )

    subjects = await _ids(
        session,
        "INSERT INTO subjects (id, slug, name, owner_learner_id) "
        "SELECT gen_random_uuid(), 'h-' || md5(random()::text), 'History subject ' || g, :learner "
        "FROM generate_series(1, :n) g RETURNING id",
        n=shape.subjects,
        learner=learner_id,
    )
    first = subjects[0]
    p |= {"subjects": subjects, "ns": len(subjects), "first": first}
    await session.execute(
        text(
            "INSERT INTO topics (id, subject_id, slug, name) "
            "SELECT gen_random_uuid(), s, 't' || g, 'Topic ' || g "
            "FROM unnest(CAST(:subjects AS uuid[])) s, generate_series(1, :n) g"
        ),
        {"subjects": subjects, "n": shape.topics_per_subject},
    )
    await session.execute(
        text(
            "INSERT INTO kcs (id, topic_id, slug, name) "
            "SELECT gen_random_uuid(), t.id, 'k' || g, t.name || ' component ' || g "
            "FROM topics t, generate_series(1, :n) g "
            "WHERE t.subject_id = ANY(CAST(:subjects AS uuid[]))"
        ),
        {"subjects": subjects, "n": shape.kcs_per_topic},
    )
    kcs = await _ids(
        session,
        "SELECT k.id FROM kcs k JOIN topics t ON t.id = k.topic_id "
        "WHERE t.subject_id = ANY(CAST(:subjects AS uuid[])) "
        "ORDER BY (t.subject_id = :first) DESC, t.slug, k.slug",
        subjects=subjects,
        first=first,
    )
    p |= {"kcs": kcs, "nk": len(kcs)}

    # One bank item per component, so practice never asks a model for one. Deterministic ids
    # link the item to its component without a round trip.
    await session.execute(
        text(
            "INSERT INTO items (id, item_type, stem, difficulty, origin, owner_learner_id, "
            "author_learner_id) "
            "SELECT md5('item' || k)::uuid, 'short', 'Explain component ' || k || "
            "' in your own words.', 0.0, 'learner', :learner, :learner "
            "FROM unnest(CAST(:kcs AS uuid[])) k"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO item_kcs (id, item_id, kc_id, weight) "
            "SELECT gen_random_uuid(), md5('item' || k)::uuid, k, 1.0 "
            "FROM unnest(CAST(:kcs AS uuid[])) k"
        ),
        p,
    )
    # A third due now: a review queue a long-standing learner actually has. Deterministic, so
    # two learners on the same graph differ only in history, never in luck.
    await session.execute(
        text(
            "INSERT INTO learner_kc_state (id, learner_id, kc_id, ability, uncertainty, "
            "last_seen_at, due_at) "
            "SELECT gen_random_uuid(), :learner, k, ((i % 7) - 3) * 0.3, 0.5, "
            "now() - interval '2 days', "
            "now() + CASE WHEN i % 3 = 0 THEN interval '-1 day' ELSE interval '7 days' END "
            "FROM unnest(CAST(:kcs AS uuid[])) WITH ORDINALITY AS u(k, i)"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO learning_events (id, learner_id, kc_id, event_type, attempt_id, "
            "payload, created_at) "
            "SELECT gen_random_uuid(), :learner, k, 'observation', gen_random_uuid(), "
            "jsonb_build_object('score', sc, 'item_score', sc, 'component_scored', false, "
            "'correct', sc >= 0.6, 'detail', null, 'diagnosis', null, 'response', "
            "jsonb_build_object('text', 'answer ' || g), 'hints_used', 0, 'prior_attempts', 0, 'item_id', "
            "md5('item' || k)::uuid::text, 'grading', null), "
            "timezone('utc', now()) - g * :step * interval '1 second' "
            "FROM (SELECT g, (CAST(:kcs AS uuid[]))[1 + g % :nk] AS k, "
            "round(random()::numeric, 2)::float8 AS sc FROM generate_series(1, :n) g) e"
        ),
        p | {"n": shape.events, "step": YEAR_SECONDS / max(shape.events, 1)},
    )
    conversations = await _ids(
        session,
        "INSERT INTO conversations (id, learner_id, subject_id, title, phase, created_at, "
        "updated_at) "
        "SELECT gen_random_uuid(), :learner, (CAST(:subjects AS uuid[]))[1 + (g - 1) % :ns], "
        "'Conversation ' || g, 'chatting', ts, ts "
        "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
        "FROM generate_series(1, :n) g) x RETURNING id",
        **(p | {"n": shape.conversations, "step": YEAR_SECONDS / max(shape.conversations, 1)}),
    )
    p |= {"convs": conversations, "nc": len(conversations)}
    await session.execute(
        text(
            "INSERT INTO messages (id, conversation_id, role, content, created_at) "
            "SELECT gen_random_uuid(), c.id, CASE WHEN m % 2 = 1 THEN 'user' ELSE 'assistant' END, "
            "'Message ' || m || ' of a long conversation about the subject.', "
            "c.created_at + m * interval '1 second' "
            "FROM conversations c, generate_series(1, :m) m WHERE c.id = ANY(CAST(:convs AS uuid[]))"
        ),
        p | {"m": shape.messages_per_conversation},
    )
    await session.execute(
        text(
            "INSERT INTO turns (id, conversation_id, flow, status, content, user_message_id, "
            "created_at, updated_at) "
            "SELECT gen_random_uuid(), m.conversation_id, 'tutor', 'completed', m.content, m.id, "
            "m.created_at, m.created_at FROM messages m "
            "WHERE m.conversation_id = ANY(CAST(:convs AS uuid[])) AND m.role = 'user'"
        ),
        p,
    )
    await session.execute(
        text(
            "INSERT INTO memories (id, learner_id, kind, content, embedding, embedding_space, "
            "status, origin_conversation_id, created_at, updated_at) "
            "SELECT gen_random_uuid(), :learner, 'fact', 'The learner remembers fact ' || g || '.', "
            f"{_vector('g', shape.vector_pool)}, "
            ":space, 'current', (CAST(:convs AS uuid[]))[1 + g % :nc], ts, ts "
            "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
            "FROM generate_series(1, :n) g) x"
        ),
        p | {"n": shape.memories, "step": YEAR_SECONDS / max(shape.memories, 1)},
    )
    await session.execute(
        text(
            "INSERT INTO sources (id, learner_id, subject_id, kind, origin, status, meta, "
            "attempts, content_type, created_at, updated_at) "
            "SELECT gen_random_uuid(), :learner, (CAST(:subjects AS uuid[]))[1 + (g - 1) % :ns], "
            "'file', 'document-' || g || '.txt', 'done', '{}'::jsonb, 1, 'text/plain', ts, ts "
            "FROM (SELECT g, timezone('utc', now()) - g * :step * interval '1 second' AS ts "
            "FROM generate_series(1, :n) g) x"
        ),
        p | {"n": shape.sources, "step": YEAR_SECONDS / max(shape.sources, 1)},
    )
    await session.execute(
        text(
            "INSERT INTO chunks (id, source_id, ordinal, text, provenance, embedding, "
            "embedding_space, created_at, updated_at) "
            "SELECT gen_random_uuid(), s.id, o, t.txt, "
            "jsonb_build_object('source_id', s.id::text, 'method', 'text'), "
            f"{_vector('o', shape.vector_pool)}, "
            ":space, s.created_at, s.created_at "
            "FROM sources s, generate_series(0, :m - 1) o, "
            "LATERAL (SELECT 'Passage ' || o || ' of ' || s.origin || "
            "' explains component ' || (o % 7) || ' in detail.' AS txt) t "
            "WHERE s.learner_id = :learner"
        ),
        p | {"m": shape.chunks_per_source},
    )
    await session.execute(
        text(
            "INSERT INTO notes (id, learner_id, topic_id, substrate, revision_ordinal) "
            "SELECT gen_random_uuid(), :learner, t.id, CAST(:atoms AS jsonb), :r "
            "FROM topics t WHERE t.subject_id = :first"
        ),
        p | {"atoms": json.dumps(NOTE_ATOMS), "r": shape.note_revisions},
    )
    await session.execute(
        text(
            "INSERT INTO note_revisions (id, note_id, ordinal, substrate, cause) "
            "SELECT gen_random_uuid(), n.id, o, n.substrate, 'distill' "
            "FROM notes n, generate_series(1, :r) o WHERE n.learner_id = :learner"
        ),
        p | {"r": shape.note_revisions},
    )
    # Older than a day: a year of spend must not count against today's caps, or every turn the
    # report times would be refused.
    await session.execute(
        text(
            "INSERT INTO llm_calls (id, learner_id, conversation_id, role, provider, model, "
            "input_tokens, output_tokens, cost_usd, created_at) "
            "SELECT gen_random_uuid(), :learner, (CAST(:convs AS uuid[]))[1 + g % :nc], 'smart', "
            "'fake', 'fake-1', 800, 200, 0.0001, "
            "timezone('utc', now()) - interval '1 day' - g * :step * interval '1 second' "
            "FROM generate_series(1, :n) g"
        ),
        p | {"n": shape.llm_calls, "step": YEAR_SECONDS / max(shape.llm_calls, 1)},
    )
    await session.commit()

    await lesson_plan_svc.generate_lesson_plan(
        session, fake_llm_client(), learner_id=learner_id, subject_id=first, goal=None
    )
    practice = Conversation(learner_id=learner_id, subject_id=first, title="Practice")
    session.add(practice)
    await session.commit()
    topic_id = (
        await _ids(
            session,
            "SELECT id FROM topics WHERE subject_id = :first ORDER BY slug LIMIT 1",
            first=first,
        )
    )[0]
    return SeededHistory(
        learner_id=learner_id,
        subject_id=first,
        topic_id=topic_id,
        conversation_id=conversations[0],
        practice_conversation_id=practice.id,
    )
