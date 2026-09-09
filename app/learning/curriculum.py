"""Curriculum generation: pure policy layer for turning a goal + materials into a Subject/Topic/KC breakdown.

Mirrors ``app/learning/item_generation.py``'s pattern — an LLM policy function that returns
a structured proposal (tolerant parsing, returns None on failure, not fatal).
"""

import json
from dataclasses import dataclass

import structlog

from app.core.redact import fingerprint
from app.llm import ChatMessage, ChatRole, LLMClient
from app.llm.types import ModelRole, Usage

log = structlog.get_logger(__name__)

CURRICULUM_SYSTEM_PROMPT = (
    "You are an expert curriculum designer. Given a learning goal and optional reference materials, "
    "propose a structured breakdown of the subject into topics and knowledge components (KCs). "
    "Each KC is a specific, testable concept. Aim for 3-5 topics with 2-4 KCs per topic. "
    "Return a JSON object only, no other text."
)


@dataclass(frozen=True)
class KCProposal:
    """A single knowledge component proposal."""

    name: str
    description: str


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
        f'"kcs": [{{"name": "...", "description": "..."}}]}}]}}'
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

        # Parse topics and KCs
        topics = []
        for topic_data in data["topics"]:
            if not isinstance(topic_data, dict):
                log.warning("curriculum.parse_failed", reason="topic_not_dict")
                return None, usage
            if not topic_data.get("name") or not topic_data.get("kcs"):
                log.warning("curriculum.parse_failed", reason="topic_missing_fields")
                return None, usage
            kcs = []
            for kc_data in topic_data["kcs"]:
                if not isinstance(kc_data, dict) or not kc_data.get("name"):
                    log.warning("curriculum.parse_failed", reason="kc_invalid")
                    return None, usage
                kcs.append(
                    KCProposal(
                        name=kc_data["name"],
                        description=kc_data.get("description", ""),
                    )
                )
            topics.append(
                TopicProposal(
                    name=topic_data["name"],
                    description=topic_data.get("description", ""),
                    kcs=tuple(kcs),
                )
            )

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


def _extract_json(content: str) -> str:
    """Extract JSON object from possibly-wrapped reply (e.g. markdown fences).

    Finds the first '{' and last '}' in the content.
    Raises ValueError if no JSON object is found.
    """
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]
