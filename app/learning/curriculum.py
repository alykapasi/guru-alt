"""Curriculum generation: pure policy layer for turning a goal + materials into a Subject/Topic/KC breakdown.

Mirrors ``app/learning/item_generation.py``'s pattern — an LLM policy function that returns
a structured proposal (tolerant parsing, returns None on failure, not fatal).
"""

import json
from dataclasses import dataclass

import structlog

from app.core.redact import fingerprint
from app.learning import prerequisites
from app.llm import ChatMessage, ChatRole, LLMClient
from app.llm.types import ModelRole, Usage

log = structlog.get_logger(__name__)

CURRICULUM_SYSTEM_PROMPT = (
    "You are an expert curriculum designer. Given a learning goal and optional reference materials, "
    "propose a structured breakdown of the subject into topics and knowledge components (KCs). "
    "Each KC is a specific, testable concept. Aim for 3-5 topics with 2-4 KCs per topic. "
    "For each KC also give `requires`: the names of the other KCs in this curriculum that a "
    "learner should master first. Name them exactly as you named them, refer only to KCs in "
    "this curriculum, and make sure the result has no cycles — if A requires B then B must not "
    "require A, directly or through any chain. Leave `requires` empty for a starting point. "
    "Return a JSON object only, no other text."
)


@dataclass(frozen=True)
class KCProposal:
    """A single knowledge component proposal."""

    name: str
    description: str
    # A stable handle for this KC within this proposal. Assigned here rather than taken from
    # the model, because it has to survive the learner renaming the KC in the review step
    # before commit — prerequisites resolved by name at parse time stay pointing at the right
    # concept afterwards, where a name-based reference would silently break.
    key: str = ""
    # Keys of the KCs that come first. Already resolved, deduplicated, self-references
    # removed, and guaranteed not to close a cycle (see app.learning.prerequisites).
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class TopicProposal:
    """A topic with its KCs."""

    name: str
    description: str
    kcs: tuple[KCProposal, ...]


@dataclass(frozen=True)
class CurriculumProposal:
    """The full curriculum proposal."""

    subject_name: str
    subject_description: str
    topics: tuple[TopicProposal, ...]


async def generate_curriculum(
    llm: LLMClient,
    goal: str,
    materials: list[str] | None,
) -> tuple[CurriculumProposal | None, Usage]:
    """Generate a curriculum proposal from a goal and optional materials.

    Returns ``(proposal, usage)`` — usage on **every** path, including the parse failures.
    A call that produced unusable output still cost what it cost, and returning only the
    proposal meant a failed generation was billed to nobody.

    Args:
        llm: LLM client (uses SMART role for higher-stakes structural output)
        goal: Refined goal string from the onboarding gate
        materials: Optional list of representative chunk excerpts (50-200 words each),
                   or None for purely knowledge-based curriculum

    Returns:
        CurriculumProposal or None if parsing fails (not fatal).
    """
    materials_section = ""
    if materials:
        materials_text = "\n\n".join(f"- {m}" for m in materials)
        materials_section = (
            f"\n\nGround your breakdown in these reference materials:\n{materials_text}"
        )
    else:
        materials_section = "\n\nNo reference materials provided; use your domain knowledge."

    prompt = (
        f"Goal: {goal}{materials_section}\n\n"
        f"Propose a curriculum breakdown as a JSON object with this structure:\n"
        f'{{"subject_name": "...", "subject_description": "...", '
        f'"topics": [{{"name": "...", "description": "...", '
        f'"kcs": [{{"name": "...", "description": "...", '
        f'"requires": ["name of an earlier KC", "..."]}}]}}]}}'
    )

    completion = await llm.complete(
        ModelRole.SMART,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=CURRICULUM_SYSTEM_PROMPT,
        max_tokens=2048,
    )
    reply, usage = completion.content, completion.usage

    try:
        # Tolerant JSON parsing — extract JSON robustly from possibly-wrapped reply
        reply_clean = _extract_json(reply)
        data = json.loads(reply_clean)

        # Validate required fields
        if not isinstance(data, dict):
            log.warning("curriculum.parse_failed", reason="not_a_dict", reply=fingerprint(reply))
            return None, usage
        if not data.get("subject_name") or not data.get("topics"):
            log.warning(
                "curriculum.parse_failed", reason="missing_fields", reply=fingerprint(reply)
            )
            return None, usage
        if not isinstance(data["topics"], list) or len(data["topics"]) == 0:
            log.warning("curriculum.parse_failed", reason="empty_topics", reply=fingerprint(reply))
            return None, usage

        # Parse topics and KCs. Prerequisites are gathered by name on this pass and resolved
        # on a second one, because a KC may require something defined in a later topic.
        topics = []
        raw_requires: dict[str, list[str]] = {}
        named_keys: list[tuple[str, str]] = []
        for ti, topic_data in enumerate(data["topics"]):
            if not isinstance(topic_data, dict):
                log.warning("curriculum.parse_failed", reason="topic_not_dict")
                return None, usage
            if not topic_data.get("name") or not topic_data.get("kcs"):
                log.warning("curriculum.parse_failed", reason="topic_missing_fields")
                return None, usage
            kcs_raw = topic_data.get("kcs")
            if not isinstance(kcs_raw, list):
                log.warning("curriculum.parse_failed", reason="kcs_not_a_list")
                return None, usage
            kcs = []
            for ki, kc_data in enumerate(kcs_raw):
                if not isinstance(kc_data, dict):
                    log.warning("curriculum.parse_failed", reason="kc_invalid")
                    return None, usage
                kc_name = kc_data.get("name")
                if not isinstance(kc_name, str) or not kc_name:
                    log.warning("curriculum.parse_failed", reason="kc_invalid")
                    return None, usage
                kc_desc = kc_data.get("description")
                key = f"t{ti}k{ki}"
                named_keys.append((key, kc_name))
                wanted = kc_data.get("requires")
                raw_requires[key] = (
                    [r for r in wanted if isinstance(r, str)] if isinstance(wanted, list) else []
                )
                kcs.append(
                    KCProposal(
                        name=kc_name,
                        description=kc_desc if isinstance(kc_desc, str) else "",
                        key=key,
                    )
                )
            topic_name = topic_data.get("name")
            topic_desc = topic_data.get("description")
            if not isinstance(topic_name, str):
                log.warning("curriculum.parse_failed", reason="topic_missing_fields")
                return None, usage
            topics.append(
                TopicProposal(
                    name=topic_name,
                    description=topic_desc if isinstance(topic_desc, str) else "",
                    kcs=tuple(kcs),
                )
            )

        resolved = _resolve_prerequisites(named_keys, raw_requires)
        topics = [
            TopicProposal(
                name=topic.name,
                description=topic.description,
                kcs=tuple(
                    KCProposal(
                        name=kc.name,
                        description=kc.description,
                        key=kc.key,
                        requires=tuple(resolved.get(kc.key, ())),
                    )
                    for kc in topic.kcs
                ),
            )
            for topic in topics
        ]

        return (
            CurriculumProposal(
                subject_name=data["subject_name"],
                subject_description=data.get("subject_description", ""),
                topics=tuple(topics),
            ),
            usage,
        )

    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log.warning(
            "curriculum.parse_failed",
            reason="json_or_validation_error",
            # The exception type, not its message: a pydantic ValidationError repeats
            # the input it rejected, which is the model's reply to a learner goal.
            error=type(exc).__name__,
            reply=fingerprint(reply),
        )
        return None, usage


def _resolve_prerequisites(
    named_keys: list[tuple[str, str]], raw_requires: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Turn the model's by-name prerequisites into keys, dropping whatever cannot be used.

    Everything dropped is logged and nothing raises: a curriculum whose edges are partly
    wrong is still better than the one we generated before this existed, which had none.
    """
    index, duplicated = prerequisites.index_by_name(named_keys)
    if duplicated:
        log.warning("curriculum.duplicate_kc_names", names=duplicated)

    edges: list[prerequisites.KeyEdge] = []
    unresolved: list[str] = []
    for key, wanted in raw_requires.items():
        found, missing = prerequisites.resolve(key, wanted, index)
        unresolved.extend(missing)
        edges.extend((prereq, key) for prereq in found)
    if unresolved:
        # The model naming something outside the curriculum it just wrote. Common enough to
        # be routine, and a foreign-key error at commit time if it were passed through.
        log.warning("curriculum.unresolved_prerequisites", count=len(unresolved))

    kept, dropped = prerequisites.acyclic(edges)
    if dropped:
        log.warning("curriculum.cyclic_prerequisites_dropped", count=len(dropped))

    by_kc: dict[str, list[str]] = {}
    for prereq, dependent in kept:
        by_kc.setdefault(dependent, []).append(prereq)
    return by_kc


def _extract_json(content: str) -> str:
    """Extract JSON object from possibly-wrapped reply (e.g. markdown fences).

    Finds the first '{' and last '}' in the content.
    Raises ValueError if no JSON object is found.
    """
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]
