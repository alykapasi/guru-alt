"""Is a new memory the same as, an update to, or something beside an existing one? (S42)

Distance alone cannot tell "studies in the evenings now" (replaces "studies in the mornings")
from "is revising for chemistry" (sits beside "is revising for physics"). Write-back sends the
candidates that have near neighbours here, in one FAST call per write-back, and applies what
comes back.

Doubt never deletes. A failed call, an unparseable reply, a missing verdict, or one naming a
neighbour that does not exist all mean ``coexists``: the worst case is two memories where one
should have replaced the other, which the learner can see and fix, rather than a true memory
silently retired.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import structlog

from app.agent.untrusted import as_untrusted
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage
from app.llm.meter import CallRefused

log = structlog.get_logger(__name__)

JUDGE_ROLE = ModelRole.FAST

Verdict = Literal["same", "updates", "coexists"]


@dataclass(frozen=True)
class Candidate:
    """A newly extracted memory and the existing memories close enough to be about it."""

    content: str
    neighbours: Sequence[str]


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict
    # 0-based index into the candidate's neighbours; set only for "updates".
    target: int | None = None


COEXISTS = Judgement("coexists")
_PLAIN: dict[str, Judgement] = {"same": Judgement("same"), "coexists": COEXISTS}

_SYSTEM_PROMPT = (
    "You maintain what a tutor remembers about one learner. For each numbered NEW statement you "
    "get the learner's existing memories that look related. Decide, for each NEW statement:\n"
    '- "same": it says the same thing as one of them (a rewording); nothing new.\n'
    '- "updates": it replaces one of them — the learner changed or corrected it, so the old '
    'one is no longer true. Give that memory\'s number as "replaces".\n'
    '- "coexists": both can be true at once; keep both.\n'
    "When unsure, answer coexists. Respond with ONLY a JSON object "
    '{"verdicts": [{"candidate": <n>, "verdict": "same"|"updates"|"coexists", '
    '"replaces": <m, only for updates>}]} and nothing else.'
)


async def judge(llm: LLMClient, candidates: Sequence[Candidate]) -> tuple[list[Judgement], Usage]:
    """One verdict per candidate, in order. No candidates → no call."""
    if not candidates:
        return [], Usage()
    try:
        completion = await llm.complete(
            JUDGE_ROLE,
            [ChatMessage(role=ChatRole.USER, content=_build_prompt(candidates))],
            system=_SYSTEM_PROMPT,
            max_tokens=64 + 48 * len(candidates),
        )
    except CallRefused:
        raise  # refused, not "coexists": the write-back defers, keeping its watermark (S47)
    except Exception:
        log.warning("memory.supersession_judge_failed", exc_info=True)
        return [COEXISTS] * len(candidates), Usage()
    return _parse(completion.content, candidates), completion.usage


def _build_prompt(candidates: Sequence[Candidate]) -> str:
    blocks = []
    for n, candidate in enumerate(candidates, start=1):
        existing = "\n".join(f"{m}. {text}" for m, text in enumerate(candidate.neighbours, start=1))
        blocks.append(
            f"NEW {n}: {candidate.content}\n"
            f"{as_untrusted(f'EXISTING MEMORIES FOR NEW {n}', existing)}"
        )
    return "\n\n".join(blocks)


def _parse(content: str, candidates: Sequence[Candidate]) -> list[Judgement]:
    judgements: list[Judgement | None] = [None] * len(candidates)
    # A candidate's first entry is its answer, valid or not: a later entry for the same
    # candidate is ignored rather than allowed to overrule it.
    seen: set[int] = set()
    try:
        start, end = content.find("{"), content.rfind("}")
        raw = json.loads(content[start : end + 1])["verdicts"] if start != -1 else []
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raw = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            index = int(entry["candidate"]) - 1
            verdict = str(entry["verdict"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= index < len(candidates) or index in seen:
            continue  # out of range, or a second answer for the same candidate
        seen.add(index)
        if verdict == "updates":
            try:
                target = int(entry["replaces"]) - 1
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= target < len(candidates[index].neighbours):
                continue
            judgements[index] = Judgement("updates", target)
        else:
            judgements[index] = _PLAIN.get(verdict)
    return [j if j is not None else COEXISTS for j in judgements]
