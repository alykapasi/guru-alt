"""Why an answer fell short, not just how far (S09).

Grading returned a score and a sentence of prose. Both are real information and neither is
actionable: "0.4, the learner confused the two forms" and "0.4, the learner never learned what
a basis is" are the same number and the same shape, and the system cannot choose different
help for them. Forgetting a symbol, applying a correct idea through a botched procedure, and
holding a wrong idea are three different failures that need three different responses — and
the fourth, a missing prerequisite, needs the plan changed rather than the explanation
reworded.

So the grader also answers a closed question. A fixed vocabulary rather than free text,
because the point is for code downstream to branch on it; prose can only be shown to a person.

Three deliberate limits, stated here because they bound what anything may do with this:

* ``confidence`` is the model's own, and a language model's self-reported confidence is not
  calibrated. Treat it as a ranking hint among diagnoses, never as a probability, and never
  as a gate on its own.
* ``evidence`` is checked against the learner's actual words. A model asked to justify a
  judgement will produce a quote whether or not one exists, so ``evidence_verbatim`` records
  whether the span was really found in the response. An unverified quote is not a reason to
  discard the diagnosis, but it is a reason not to show it to the learner as theirs.
* Nothing here establishes that the diagnosis is *right*. It is one model's reading of one
  answer, and measuring whether these labels predict anything is S59's work.

Pure: no database, no model calls, no clock.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.learning.prerequisites import normalise


class FailureKind(StrEnum):
    """What went wrong, in the terms that change what should happen next."""

    NONE = "none"
    """Nothing wrong with this component — the place a correct answer lands."""

    NOTATION = "notation"
    """The idea is there; a symbol, name, or convention is not. Cheapest to repair, and the
    one most often mistaken for a conceptual gap because the answer reads as wrong."""

    PROCEDURAL = "procedural"
    """The right method, carried out incorrectly. More practice helps; re-explaining does not."""

    CONCEPTUAL = "conceptual"
    """The idea itself is wrong or absent. Re-teaching helps; more practice entrenches it."""

    PREREQUISITE = "prerequisite"
    """The failure is upstream of what was asked. The plan is wrong, not the explanation —
    this is the one that should send the learner somewhere else (S11)."""

    INCOMPLETE = "incomplete"
    """Nothing was demonstrated either way: blank, off-topic, or stopped partway. Says the
    least about the learner, and must not be read as evidence they cannot do it."""


ACTIONABLE: frozenset[FailureKind] = frozenset(
    {FailureKind.NOTATION, FailureKind.PROCEDURAL, FailureKind.CONCEPTUAL, FailureKind.PREREQUISITE}
)
"""Kinds that name a specific failure. ``NONE`` and ``INCOMPLETE`` do not — one because there
is nothing to fix, the other because nothing was shown."""


class Diagnosis(BaseModel):
    """One component's diagnosis. Every field beyond ``kind`` is advisory — see the module
    docstring on what ``confidence`` and ``evidence`` are and are not worth."""

    kind: FailureKind = FailureKind.NONE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str = ""
    evidence_verbatim: bool = False
    prerequisite: str = ""
    """What the grader thinks is missing upstream, by name. Free text on purpose: the grader
    sees the question, not the graph, so resolving this to a KC is the planner's job (S11)."""

    @property
    def actionable(self) -> bool:
        return self.kind in ACTIONABLE


def parse(raw: object, *, response_text: str = "") -> Diagnosis:
    """Build a :class:`Diagnosis` from one model-supplied object, best-effort.

    Anything unrecognised degrades to ``NONE`` rather than raising: a grade is still a grade
    without a diagnosis, and losing the score because the extra field came back malformed
    would trade something the system depends on for something it merely benefits from.
    """
    if not isinstance(raw, dict):
        return Diagnosis()
    kind = _kind(raw.get("kind"))
    if kind is FailureKind.NONE:
        return Diagnosis()
    evidence = str(raw.get("evidence", "")).strip()
    return Diagnosis(
        kind=kind,
        confidence=_confidence(raw.get("confidence")),
        evidence=evidence,
        evidence_verbatim=quotes_verbatim(evidence, response_text),
        prerequisite=str(raw.get("prerequisite", "")).strip(),
    )


def quotes_verbatim(evidence: str, response_text: str) -> bool:
    """Whether ``evidence`` really appears in what the learner wrote.

    Whitespace-insensitive and case-folded, because a model reflowing a quote is not the thing
    worth catching — inventing one is. Reuses the curriculum's name normalisation so "the same
    text" means one thing across the codebase.
    """
    if not evidence or not response_text:
        return False
    return normalise(evidence) in normalise(response_text)


def _kind(raw: object) -> FailureKind:
    if not isinstance(raw, str):
        return FailureKind.NONE
    try:
        return FailureKind(raw.strip().lower())
    except ValueError:
        return FailureKind.NONE


def _confidence(raw: object) -> float:
    if not isinstance(raw, int | float | str):
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0
