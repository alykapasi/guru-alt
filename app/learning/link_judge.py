"""Whether two presentations in different subjects are the same idea (S24).

Used only for pairs touching a learner's own material, where there is no administrator to ask
and no administrator should read the material. Its endorsement is half of a link; the
learner's acceptance is the other half, so a wrong endorsement costs one declined suggestion,
not a silently merged estimate.
"""

import json
from dataclasses import dataclass, field

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

JUDGE_ROLE = ModelRole.SMART
"""A judgement about meaning across contexts, with a learner's trust riding on it — not FAST."""

_SYSTEM_PROMPT = (
    "You decide whether two knowledge components, taught in two different subjects, are the "
    "same underlying idea — so that someone who has demonstrated one has demonstrated most of "
    "the other. Sharing a name is not enough: 'Functions' in calculus and 'Functions' in "
    "programming share a name and little else. Reply with JSON only: "
    '{"verdict": "endorse" | "reject", "reason": "<one sentence a learner will read>"}'
)


@dataclass(frozen=True)
class Side:
    kc_name: str
    description: str | None
    topic_name: str
    subject_name: str
    prerequisites: list[str] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    endorse: bool
    reason: str


def _describe(label: str, side: Side) -> str:
    lines = [
        f"{label}: {side.kc_name}",
        f"  Subject: {side.subject_name}; topic: {side.topic_name}",
        f"  Description: {side.description or '(none)'}",
        f"  Builds on: {', '.join(side.prerequisites) or '(nothing listed)'}",
        f"  Leads to: {', '.join(side.dependents) or '(nothing listed)'}",
    ]
    return "\n".join(lines)


def parse_verdict(content: str) -> Verdict | None:
    """The model's verdict, or ``None`` when the reply cannot be read as one. ``None`` is not a
    rejection: the pair stays unjudged and is asked again next run."""
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    verdict, reason = payload.get("verdict"), payload.get("reason")
    if verdict not in ("endorse", "reject") or not isinstance(reason, str) or not reason.strip():
        return None
    return Verdict(endorse=verdict == "endorse", reason=reason.strip())


async def judge_pair(client: LLMClient, a: Side, b: Side) -> tuple[Verdict | None, Usage]:
    """Ask the model. Never raises: a provider failure is no verdict, like an unreadable one."""
    try:
        completion = await client.complete(
            JUDGE_ROLE,
            [
                ChatMessage(
                    role=ChatRole.USER, content=f"{_describe('A', a)}\n\n{_describe('B', b)}"
                )
            ],
            system=_SYSTEM_PROMPT,
            max_tokens=200,
        )
    except Exception:
        return None, Usage()
    return parse_verdict(completion.content), completion.usage
