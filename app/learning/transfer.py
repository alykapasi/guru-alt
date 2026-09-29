"""Transfer: applying a component in a setting it was never practised in (S14).

Two different item ids for one component were never "genuinely different applications" — one
template, one generator — so the old claim was removed (goal-policy design §4.3). It returns
with a difference the system can point at: a *named setting* from a fixed catalogue, recorded
on the question, compared exactly. A question with no setting is abstract — which is what
nearly every question written before this was.

Pure: no database, no model calls.
"""

from collections.abc import Iterable

ABSTRACT = "abstract"

SETTINGS: tuple[tuple[str, str], ...] = (
    (ABSTRACT, "no real-world setting; the idea on its own terms"),
    ("everyday", "household life, routines and shopping"),
    ("money", "prices, budgets, interest or trade"),
    ("physics", "motion, forces, energy or light"),
    ("biology", "living things, bodies and cells"),
    ("engineering", "building, machines and design"),
    ("computing", "software, data and networks"),
    ("sport", "games, training and competition"),
    ("health", "medicine, nutrition and fitness"),
    ("society", "people, history and government"),
    ("arts", "music, art and writing"),
    ("nature", "weather, geography and ecology"),
)
"""The order is the order checks move through: a failed check in one setting leaves it
practised, so the next check takes the next one."""

NAMES: frozenset[str] = frozenset(name for name, _ in SETTINGS)
_GLOSS = dict(SETTINGS)


def setting_of(value: str | None) -> str:
    """A question's setting, with none read as abstract."""
    return value or ABSTRACT


def next_setting(practised: Iterable[str]) -> str | None:
    """The first catalogue setting a transfer check could use, or None when all are practised."""
    seen = set(practised)
    return next((name for name, _ in SETTINGS if name != ABSTRACT and name not in seen), None)


def prompt_line(setting: str | None) -> str:
    """The sentence a generator adds for ``setting``; nothing for none."""
    if setting is None:
        return ""
    return f" Set the question in this setting: {setting} ({_GLOSS[setting]})."
