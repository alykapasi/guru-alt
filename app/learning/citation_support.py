"""Does the cited passage actually say what the block says? (S28, second half.)

Citation resolution checks that an index is in range and maps it to a real chunk. That makes a
citation a *valid pointer*; it says nothing about whether the passage supports the sentence
attached to it. A block can cite six real chunks and be wrong about all of them, and every check
in the system would pass — the indices resolve, the chunks exist, the learner sees footnotes.

The failure this addresses is the one grounding was supposed to prevent. An ungrounded claim
that is *presented* as grounded is worse than an obviously ungrounded one, because the citation
is what tells a learner they need not check it.

**Claim-first, not citation-first.** The obvious shape is to walk the citations and ask whether
each is relevant. That misses the more dangerous case: a claim nothing supports at all, which
has no citation to walk. So the model is asked to extract the block's checkable claims and, for
each, name the passages supporting it or say that none do. A block whose citations are all
relevant can still fail here, which is the point.

**No verdict is enforced anywhere.** This measures; it does not gate generation. Every check
costs a model call on the SMART tier, and what rate of unsupported claims is tolerable is a
threshold nobody has set — the same reasoning that keeps thresholds out of S59.
"""

import json
import uuid
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel

from app.agent.untrusted import as_untrusted
from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

SUPPORT_ROLE = ModelRole.SMART
"""Judging entailment between a claim and a passage is a comprehension task, not a cheap one."""


class CitationSupportError(RuntimeError):
    """The model's reply could not be parsed into verdicts."""


class Verdict(StrEnum):
    """How well the cited material carries the claim."""

    SUPPORTED = "supported"
    PARTIAL = "partial"  # the passage bears on it but does not establish it
    UNSUPPORTED = "unsupported"


class CitedPassage(BaseModel):
    """One chunk the block cited, as the checker needs to see it.

    Decoupled from the ORM in the same spirit as ``GradedComponent``: the checker takes text and
    ids, not rows, so it stays a pure model call with no database reachable from it.
    """

    chunk_id: uuid.UUID
    source_id: uuid.UUID
    text: str


class ClaimVerdict(BaseModel):
    """One checkable claim from the block, against the passages offered for it."""

    claim: str
    verdict: Verdict
    # Indices into the passages given to the checker. Empty on an unsupported claim, which is
    # the case worth finding: something the block asserts that nothing cited establishes.
    supported_by: list[int] = []
    reason: str = ""


class SupportReport(BaseModel):
    """What a block's citations were actually worth."""

    claims: list[ClaimVerdict]
    # Pairs of cited passages the model judged to disagree with each other, by index. S28 names
    # contradictory sources beside insufficient ones, and a block that silently picks a side is
    # not distinguishable from one whose sources agreed.
    contradictions: list[tuple[int, int]] = []

    @property
    def n(self) -> int:
        return len(self.claims)

    @property
    def unsupported(self) -> list[ClaimVerdict]:
        """The claims nothing cited establishes — the finding, when there is one."""
        return [c for c in self.claims if c.verdict is Verdict.UNSUPPORTED]

    @property
    def supported_fraction(self) -> float | None:
        """Share fully supported. ``None`` with no claims — not 1.0, which would read as
        a clean bill of health for a block nobody managed to extract a claim from."""
        if not self.claims:
            return None
        return sum(1 for c in self.claims if c.verdict is Verdict.SUPPORTED) / len(self.claims)

    @property
    def clean(self) -> bool:
        """Every claim fully supported, and no citation left uncited. Never true of an empty
        report, for the reason above."""
        return bool(self.claims) and not any(
            c.verdict is not Verdict.SUPPORTED for c in self.claims
        )


_SYSTEM_PROMPT = (
    "You check whether written material is supported by the sources it cites. You are given a "
    "passage of instructional text and the numbered source snippets it was written from. "
    "Extract every checkable factual claim the text makes — not its framing, examples or "
    "transitions — and for each, decide whether the snippets establish it. A claim is "
    '"supported" only if a snippet states or directly entails it; "partial" if a snippet bears '
    'on it without establishing it; "unsupported" if no snippet does. Judge only against the '
    "snippets, never against your own knowledge: a claim that is true in the world but absent "
    "from the snippets is unsupported, and that is the answer wanted. Also report any pair of "
    "snippets that contradict each other. Respond with ONLY a JSON object of the form "
    '{"claims": [{"claim": "<the claim, quoted from the text>", "verdict": '
    '"supported"|"partial"|"unsupported", "supported_by": [<snippet numbers>], "reason": '
    '"<one short sentence>"}], "contradictions": [[<snippet number>, <snippet number>]]} '
    "and nothing else."
)


async def check_support(
    client: LLMClient,
    *,
    body: str,
    passages: Sequence[CitedPassage],
    max_tokens: int = 1024,
) -> tuple[SupportReport, Usage]:
    """Check a block's claims against the passages it cited.

    Returns the report plus the call's token ``Usage`` so the caller can log cost, matching
    ``grade_open``. A block with no body or no cited passages returns an empty report and makes
    no model call — there is nothing to check, and an empty report is not a pass.
    """
    text = body.strip()
    if not text or not passages:
        return SupportReport(claims=[]), Usage()

    completion = await client.complete(
        SUPPORT_ROLE,
        [ChatMessage(role=ChatRole.USER, content=_build_prompt(text, passages))],
        system=_SYSTEM_PROMPT,
        max_tokens=max_tokens,
    )
    return _parse(completion.content, n_passages=len(passages)), completion.usage


def _build_prompt(body: str, passages: Sequence[CitedPassage]) -> str:
    # Both halves are model-authored or learner-supplied text being *reasoned about*, not
    # instructions to follow — the same treatment the agent's tools give retrieved material.
    snippets = "\n\n".join(f"[{i}] {p.text}" for i, p in enumerate(passages))
    return (
        f"{as_untrusted('TEXT UNDER REVIEW', body)}\n\n{as_untrusted('SOURCE SNIPPETS', snippets)}"
    )


def _extract_json(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise CitationSupportError(f"no JSON object in reply: {content!r}")
    return content[start : end + 1]


def _parse(content: str, *, n_passages: int) -> SupportReport:
    """Parse the reply, dropping anything that does not describe a real snippet.

    Out-of-range snippet numbers are discarded rather than trusted, on the same grounds as
    ``_resolve_citations``: a number that points at nothing is not evidence, and keeping it
    would let a hallucinated index count as support.
    """
    try:
        data = json.loads(_extract_json(content))
    except json.JSONDecodeError as err:
        raise CitationSupportError(f"unparseable reply: {content!r}") from err

    claims: list[ClaimVerdict] = []
    for raw in data.get("claims", []):
        if not isinstance(raw, dict):
            continue
        claim = str(raw.get("claim", "")).strip()
        if not claim:
            continue
        try:
            verdict = Verdict(str(raw.get("verdict", "")).strip().lower())
        except ValueError:
            continue  # a verdict we do not recognise is not a verdict
        supported_by = [
            i for i in raw.get("supported_by", []) if isinstance(i, int) and 0 <= i < n_passages
        ]
        # A claim called supported that names no real snippet is not supported by anything we
        # can show a reader, whatever the model said.
        if verdict is not Verdict.UNSUPPORTED and not supported_by:
            verdict = Verdict.UNSUPPORTED
        claims.append(
            ClaimVerdict(
                claim=claim,
                verdict=verdict,
                supported_by=sorted(set(supported_by)),
                reason=str(raw.get("reason", "")).strip(),
            )
        )

    contradictions: list[tuple[int, int]] = []
    for pair in data.get("contradictions", []):
        if not isinstance(pair, list) or len(pair) != 2:
            continue
        a, b = pair
        if not all(isinstance(x, int) and 0 <= x < n_passages for x in (a, b)) or a == b:
            continue
        contradictions.append((min(a, b), max(a, b)))

    return SupportReport(claims=claims, contradictions=sorted(set(contradictions)))
