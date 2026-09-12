"""Letting the tutor say "this question counts" (S15).

A conversational check was posed only from the lesson plan's active step. That covers the case
the plan anticipated and misses the one that makes conversation worth having: the tutor notices
something, asks about it, the learner answers — and none of it reaches the tracer, because
nothing in the exchange was ever an *item*. The evidence a tutor gathers by teaching was the
evidence the system could not see.

The obstacle was never detection, it was attribution. A question in prose has no component
attached and no stated standard, so an answer to it cannot be scored against anything or
credited to anything. Guessing either from the text would be inventing evidence, which is
worse than missing it.

So the tutor declares. It writes its explanation as prose and then emits one marker naming the
component and the question, and that marker — not the prose — is what becomes the item. The
learner is shown the question as a widget, exactly as a plan-driven check already is, so the
thing they are asked and the thing that is graded cannot drift apart: there is only one of
them. The marker is stripped before the reply is stored, because it is addressed to the system
and a learner reading their own transcript should not find machinery in it.

Pure: no database, no model calls. Resolving the named component against the graph is the
caller's job, and dropping one that resolves to nothing is deliberate — the tutor sees the
conversation, not the graph, so it can name something true and irrelevant.
"""

import re
from dataclasses import dataclass

# Deliberately noisy and unlikely to be produced by accident, and deliberately *not* JSON: the
# reply is streamed to the learner token by token, so a partially-emitted marker is going to be
# on somebody's screen for a moment either way, and half a sentence in brackets reads better
# than half an object.
MARKER = re.compile(
    r"\[\[\s*CHECK\s*:\s*(?P<component>[^:\]]{1,120}?)\s*::\s*(?P<question>[^\]]{1,600}?)\s*\]\]",
    re.IGNORECASE | re.DOTALL,
)

INSTRUCTION = (
    "If your reply asks the learner a question you want *graded* — a real comprehension check, "
    "not a rhetorical or conversational one — do not write the question in your prose. Lead up "
    "to it, then emit exactly one marker of the form "
    "[[CHECK: <knowledge component name> :: <the question>]] as the last line. The learner is "
    "shown the question from the marker, so writing it twice shows it twice. Name a knowledge "
    "component from the material above; if none fits, ask nothing and emit no marker. At most "
    "one marker per reply, and none at all when you are explaining rather than checking."
)


@dataclass(frozen=True)
class DeclaredCheck:
    """A question the tutor asked for, and the component it says the question is about."""

    component: str
    question: str


def extract(reply: str) -> tuple[str, DeclaredCheck | None]:
    """``(reply without the marker, the declaration)``.

    The *first* marker wins and every marker is stripped. A reply carrying two declarations has
    disobeyed the instruction, and the failure to prefer is silent either way — taking the first
    and removing the rest at least leaves one question on screen rather than two, and one of
    them graded rather than neither.
    """
    match = MARKER.search(reply)
    cleaned = MARKER.sub("", reply).strip()
    if match is None:
        return reply, None
    component = match.group("component").strip()
    question = match.group("question").strip()
    if not component or not question:
        return cleaned, None
    return cleaned, DeclaredCheck(component=component, question=question)
