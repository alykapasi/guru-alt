"""Which conversational turns are evidence, and which are only conversation (S15).

Mastery was reachable from exactly one place: ``answer_item``, called by the guided-practice
workflow. A learner who explained an idea well in chat had demonstrated nothing the system
recorded, and the practice item the tutor turn already resolved was handed to the client as a
widget and then forgotten — a new one was minted next turn, the conversation's phase still said
``chatting``, and nothing connected the learner's next message to the question they had just
been asked.

The fix is not "grade the conversation". Treating every message as evidence is worse than
treating none as evidence, because it is wrong in a direction that looks like data: a learner
thinking aloud, asking for a hint, or saying "hold on, why does that work?" would each be
scored as a failed attempt and fed to the tracer as proof they cannot do it.

So evidence needs a *deliberate* mechanism, and it has three parts:

* A check is **posed once and stays open** — recorded on the conversation — rather than a fresh
  item being resolved and discarded every turn. There is exactly one question in play, both
  sides know which, and it survives a reload.
* A message arriving against an open check passes an **intent gate** before it is graded. Only
  a genuine attempt becomes an observation; everything else leaves the check standing.
* An attempt made after the tutor has explained more is **not independent**, and says so. Every
  reply given while the question still stands is counted as a scaffold on the conversation
  (``Conversation.active_item_scaffolds``) and discounted exactly as a guided-practice hint is
  (:mod:`app.learning.assistance`).

The gate's default is the load-bearing choice. Every unparseable reply, every empty message and
every model failure resolves to :attr:`TurnIntent.DEFERRAL` — keep the check, record nothing.
Recording no evidence is always recoverable: the question is still open and the learner can
still answer it. Recording invented evidence is not, because it reaches the tracer, moves the
ability estimate, reschedules the FSRS card, and revises the lesson plan before anyone notices.
The two errors are not symmetric, so the default is not either.

Pure apart from the one classification call: no database, no clock.
"""

import json
from enum import StrEnum

from app.llm import ChatMessage, ChatRole, LLMClient, ModelRole, Usage

CHECK_ROLE = ModelRole.FAST
"""Deciding whether a sentence is an attempt is a cheap classification — the FAST tier. The
grading it gates is a SMART call, so the gate also saves that call on every message that turns
out not to be an answer."""


class TurnIntent(StrEnum):
    """What the learner's message does about the check that is open."""

    ATTEMPT = "attempt"
    """A genuine try at the question, however partial or wrong. The only intent that becomes
    evidence — and partial and wrong are exactly what the rubric grader is for."""

    DEFERRAL = "deferral"
    """Engaging with the question without answering it: asking what it means, asking for a
    hint, thinking aloud, or answering a different question first. The check stays open, and
    the tutor's reply becomes a scaffold against the eventual attempt."""

    WITHDRAWAL = "withdrawal"
    """Leaving the question: changing the subject, or saying outright they would rather not.
    The check is dropped with no evidence recorded. A learner must be able to decline a
    question without that refusal itself becoming a mark against them."""


_SYSTEM_PROMPT = (
    "A tutor asked a learner a specific practice question. Classify what the learner's reply "
    "does about that question — not whether it is correct, which is graded separately. "
    'Respond with ONLY a JSON object {"intent": "attempt"|"deferral"|"withdrawal"} and '
    "nothing else. "
    '"attempt" = they are trying to answer it, even partially, even if they are unsure or '
    "plainly wrong. "
    '"deferral" = they are engaging but not answering: asking what it means, asking for a '
    "hint, or asking something else first. "
    '"withdrawal" = they are leaving it: changing the subject, or declining to answer. '
    "When the reply could be read either way, prefer the weaker claim: deferral over attempt."
)


async def classify_intent(
    client: LLMClient, *, question: str, message: str, max_tokens: int = 64
) -> tuple[TurnIntent, Usage]:
    """What ``message`` does about ``question``.

    Never raises: a provider failure is a :attr:`TurnIntent.DEFERRAL`, like every other thing
    that goes wrong here. A check the system could not classify is a check still waiting for an
    answer, which is the true state of it.
    """
    if not message.strip():
        return TurnIntent.DEFERRAL, Usage()
    try:
        completion = await client.complete(
            CHECK_ROLE,
            [ChatMessage(role=ChatRole.USER, content=_build_prompt(question, message))],
            system=_SYSTEM_PROMPT,
            max_tokens=max_tokens,
        )
    except Exception:
        return TurnIntent.DEFERRAL, Usage()
    return parse_intent(completion.content), completion.usage


def _build_prompt(question: str, message: str) -> str:
    return f"Question the tutor asked:\n{question}\n\nLearner's reply:\n{message}"


def parse_intent(content: str) -> TurnIntent:
    """Read an intent out of a model reply. Anything unreadable is a
    :attr:`TurnIntent.DEFERRAL` — see the module docstring on why the default is not neutral."""
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return TurnIntent.DEFERRAL
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return TurnIntent.DEFERRAL
    if not isinstance(payload, dict):
        return TurnIntent.DEFERRAL
    raw = payload.get("intent")
    if not isinstance(raw, str):
        return TurnIntent.DEFERRAL
    try:
        return TurnIntent(raw.strip().lower())
    except ValueError:
        return TurnIntent.DEFERRAL
