"""Turning a grade into an instruction about what to teach next (S09, S10).

The diagnosis vocabulary was defined so that *code* could branch on it — a stored label is
just a label, and prose can only be shown to a person. This module is that branch, and it lives
here rather than inside one flow because both flows that grade an answer have to make the same
move on the same evidence. Plain chat and guided practice reaching different conclusions about
one learner's one mistake is the same class of defect S16 fixed for learner context: not three
designs, one system forgetting what it knows when the learner presses a different button.

Nothing here asserts more than the grader said. A component with no per-component score does
not get the item's aggregate, and a kind of ``none`` or ``incomplete`` produces no repair —
one because there is nothing to fix, the other because nothing was demonstrated either way.
"""

import uuid
from collections.abc import Mapping

from app.learning.diagnosis import FailureKind
from app.learning.grading import GradeResult

REPAIR: dict[FailureKind, str] = {
    # One teaching move per failure kind, taken from what each kind means in
    # ``app.learning.diagnosis``. This is the join the diagnosis vocabulary was built for: it
    # was defined so code could branch on it, and until S15 nothing did — the label was stored,
    # returned to the client, and never allowed to change what the learner was told next.
    FailureKind.NOTATION: (
        "Name the convention they missed and move on — the underlying idea was there, so do "
        "not re-teach it."
    ),
    FailureKind.PROCEDURAL: (
        "Walk through the step they carried out wrongly. Do not re-explain the idea; they "
        "have it, and hearing it again will not fix the execution."
    ),
    FailureKind.CONCEPTUAL: (
        "Re-teach the idea itself from a different angle, and do not offer more practice yet "
        "— repetition on a wrong idea entrenches it."
    ),
    FailureKind.PREREQUISITE: (
        "The gap is upstream of what you asked. Say so plainly, and address that earlier idea "
        "before returning to this question."
    ),
}


def diagnosis_notes(result: GradeResult, kc_names: Mapping[uuid.UUID, str]) -> list[str]:
    """One sentence per diagnosed component, saying what went wrong and what to do about it.

    Empty where nothing was diagnosed, which is every deterministic path: an MCQ knows the
    answer was wrong and nothing about why, and a prompt that asserted a reason the evidence
    does not carry would have the tutor confidently repair a misconception nobody found.
    """
    notes: list[str] = []
    for kc_id, diagnosis in result.diagnoses.items():
        repair = REPAIR.get(diagnosis.kind)
        if repair is None:  # NONE and INCOMPLETE: nothing diagnosed to repair
            continue
        name = kc_names.get(kc_id, "that part")
        notes.append(f"On {name}, what went wrong was {diagnosis.kind.value}. {repair}")
        if diagnosis.evidence and diagnosis.evidence_verbatim:
            # Only a span actually found in the response is quoted back. An unverified quote
            # is still usable as a diagnosis, but showing a learner words they never wrote as
            # though they wrote them is its own failure (S09).
            notes.append(f'They wrote: "{diagnosis.evidence}".')
    return notes


def component_split(result: GradeResult, kc_names: Mapping[uuid.UUID, str]) -> str | None:
    """How the components did differently, or ``None`` where the grader could not tell."""
    if not result.component_scores:
        return None
    split = "; ".join(
        f"{kc_names.get(kc_id, 'one component')}: {score:.2f}"
        for kc_id, score in result.component_scores.items()
    )
    return f"Part by part — {split}."


def teaching_note(
    result: GradeResult, kc_names: Mapping[uuid.UUID, str], *, opening: str, closing: str
) -> str:
    """The whole instruction: what it scored, how the parts did, what to repair, what to do.

    ``opening`` and ``closing`` differ between flows — a conversational check must not pose
    another question this turn, while guided practice is about to offer a hint on the same one
    — and everything between them is the same evidence read the same way.
    """
    verdict = "correct" if result.correct else "not correct"
    parts = [f"{opening} It graded {result.score:.2f} ({verdict})."]
    split = component_split(result, kc_names)
    if split is not None:
        parts.append(split)
    parts.extend(diagnosis_notes(result, kc_names))
    parts.append(closing)
    return " ".join(parts)
