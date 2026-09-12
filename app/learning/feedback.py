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


RECURRENCE_MIN = 2
"""Prior occurrences of the same failure kind before it is treated as a pattern (S09).

Two earlier ones, so the third time is when the teaching changes. One repeat is a coincidence
worth nothing; by the third the explanation on offer has demonstrably not worked, and repeating
it a fourth time is the system being stubborn rather than the learner being slow.

Uncalibrated, like every other threshold here (S18) — but note what it is *not* gated on. The
per-diagnosis ``confidence`` is the model's own and is uncalibrated in a way no amount of
tuning fixes, so nothing may branch on it. A count of independent observations is a different
kind of evidence, and it is the one this branches on.
"""


def _ordinal(n: int) -> str:
    """``3`` -> ``"3rd"``. Teens are the exception every naive version gets wrong."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _recurrence_note(name: str, kind: FailureKind, times_before: int) -> str:
    """What to say once a failure kind stops looking like a slip."""
    return (
        f"This is the {_ordinal(times_before + 1)} time {name} has gone wrong the same way "
        f"({kind.value}), across separate attempts. Treat it as a settled wrong idea rather "
        "than a slip: the explanation already given has not worked, so change the "
        "representation entirely or go back to what this rests on. Do not repeat it."
    )


def diagnosis_notes(
    result: GradeResult,
    kc_names: Mapping[uuid.UUID, str],
    *,
    prior_kinds: Mapping[uuid.UUID, Mapping[FailureKind, int]] | None = None,
) -> list[str]:
    """One sentence per diagnosed component, saying what went wrong and what to do about it.

    Empty where nothing was diagnosed, which is every deterministic path: an MCQ knows the
    answer was wrong and nothing about why, and a prompt that asserted a reason the evidence
    does not carry would have the tutor confidently repair a misconception nobody found.

    ``prior_kinds`` is how often each kind has already come up on each component
    (``mastery.prior_failure_kinds``). Without it every mistake is described as if it were the
    first, which is the specific failure this argument fixes: one persistent wrong belief and
    five unrelated slips produced identical instructions, and they need opposite ones.
    """
    notes: list[str] = []
    seen = prior_kinds or {}
    for kc_id, diagnosis in result.diagnoses.items():
        repair = REPAIR.get(diagnosis.kind)
        if repair is None:  # NONE and INCOMPLETE: nothing diagnosed to repair
            continue
        name = kc_names.get(kc_id, "that part")
        times_before = seen.get(kc_id, {}).get(diagnosis.kind, 0)
        if times_before >= RECURRENCE_MIN:
            # The recurrence supersedes the one-off repair rather than being appended to it:
            # "re-explain the idea" is exactly the advice that has already failed twice.
            notes.append(_recurrence_note(name, diagnosis.kind, times_before))
        else:
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
    result: GradeResult,
    kc_names: Mapping[uuid.UUID, str],
    *,
    opening: str,
    closing: str,
    prior_kinds: Mapping[uuid.UUID, Mapping[FailureKind, int]] | None = None,
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
    parts.extend(diagnosis_notes(result, kc_names, prior_kinds=prior_kinds))
    parts.append(closing)
    return " ".join(parts)
