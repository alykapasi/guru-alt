"""What a learner can set explicitly, and what each setting tells the model (S02, V09).

A catalog in code, like the profile's dimension catalog: adding a setting needs no migration.
An explicit setting *pins* its parameter — inference keeps computing and showing its own value,
but stops steering. ``auto`` means "adapt to me", which is also what an unset key resolves to.

Only these fixed strings ever reach a prompt. A setting is chosen from a list, so nothing a
learner types can be quoted back to a model as an instruction.
"""

from dataclasses import dataclass

AUTO = "auto"


@dataclass(frozen=True)
class PreferenceSpec:
    key: str
    options: tuple[str, ...]
    default: str
    label: str


CATALOG: dict[str, PreferenceSpec] = {
    spec.key: spec
    for spec in (
        # Nothing infers guidance (V07), so it has no "adapt to me": guided is the default.
        PreferenceSpec("guidance", ("guided", "exploration"), "guided", "Guidance"),
        PreferenceSpec(
            "explanation_level",
            (AUTO, "introductory", "standard", "advanced"),
            AUTO,
            "Explanation level",
        ),
        PreferenceSpec(
            "note_format",
            (AUTO, "outline", "narrative", "mnemonic", "worked_examples"),
            AUTO,
            "Note format",
        ),
        PreferenceSpec("hints", (AUTO, "fewer", "some", "more"), AUTO, "Hints"),
        PreferenceSpec("pace", (AUTO, "brisk", "standard", "unhurried"), AUTO, "Pace"),
    )
}


def is_valid(key: str, value: str) -> bool:
    spec = CATALOG.get(key)
    return spec is not None and value in spec.options


EXPLANATION_INSTRUCTIONS = {
    "introductory": "Pitch explanations at an introductory level: define terms as they come up "
    "and assume no background in the subject.",
    "standard": "Pitch explanations at a standard level: assume the usual background for this "
    "subject, and define anything specialised.",
    "advanced": "Pitch explanations at an advanced level: be concise, skip the basics, and go "
    "into depth.",
}

PACE_INSTRUCTIONS = {
    "brisk": "Keep a brisk pace: short steps, and move on as soon as they have it.",
    "standard": "Keep a steady pace.",
    "unhurried": "Take an unhurried pace: smaller steps, and check understanding before moving on.",
}

HINT_INSTRUCTIONS = {
    "fewer": "Give fewer hints: let them try first, and hint only when asked or clearly stuck.",
    "some": "Give hints at a moderate rate.",
    "more": "Give hints readily: offer a nudge as soon as they hesitate.",
}

# The plan's inferred hint density uses low/medium/high; the learner-facing words differ.
HINT_DENSITY = {"fewer": "low", "some": "medium", "more": "high"}
