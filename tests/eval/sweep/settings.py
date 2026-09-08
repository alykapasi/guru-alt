"""What a sweep is actually able to vary, and what each setting does when it gets there.

A sweep's whole product is a comparison. `gen_config` and `toggles` were free-form dicts
that were logged as run parameters and then, for everything but one toggle, ignored — so a
sweep could report that it compared `temperature: 0.2` against `temperature: 0.9` while
running the identical configuration twice and attributing the noise between them to a
setting that never reached a model. A false result is worse than no result.

Every knob a cell may set is declared here, with the suite it reaches and how. A cell naming
anything else is refused when the config is loaded — before any paid call — rather than
silently producing a comparison that did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

# Suites whose behaviour no declared setting reaches yet. Listed so the error message can say
# so plainly instead of implying the setting was misspelled.
UNTUNED_SUITES = ("rubric", "retrieval")


@dataclass(frozen=True)
class Setting:
    """One sweepable knob: which suite it changes, and what values it accepts."""

    name: str
    suite: str
    describe: str
    validate: Any  # value -> None, raising ValueError on a value the call path cannot take


def _confidence(value: object) -> None:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"must be a number, got {value!r}")
    if not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"must be between 0.0 and 1.0, got {value!r}")


GEN_SETTINGS: dict[str, Setting] = {
    "kc_tag_min_confidence": Setting(
        name="kc_tag_min_confidence",
        suite="kc_tagging",
        describe="confidence a predicted tag must reach to be kept",
        validate=_confidence,
    )
}

TOGGLE_SETTINGS: dict[str, Setting] = {
    "strict_kc_tagging": Setting(
        name="strict_kc_tagging",
        suite="kc_tagging",
        describe="shorthand for kc_tag_min_confidence=0.7",
        validate=lambda value: None,  # expand() already constrains toggles to booleans
    )
}


class UnsupportedSetting(ValueError):
    """A cell names a setting the runner cannot apply, so the comparison would be fictional."""


def _reject(kind: str, name: str, known: dict[str, Setting]) -> NoReturn:
    supported = ", ".join(sorted(known)) or "(none)"
    raise UnsupportedSetting(
        f"{kind} {name!r} is not applied by any suite, so a sweep varying it would compare "
        f"identical configurations. Supported {kind}s: {supported}. "
        f"Nothing tunes the {' or '.join(UNTUNED_SUITES)} suites yet."
    )


def validate_settings(gen_config: dict, toggles: dict) -> None:
    """Raise :class:`UnsupportedSetting` unless every named setting actually reaches a call."""
    for key, value in gen_config.items():
        setting = GEN_SETTINGS.get(key)
        if setting is None:
            _reject("gen_config key", key, GEN_SETTINGS)
        try:
            setting.validate(value)
        except ValueError as exc:
            raise UnsupportedSetting(f"gen_config {key!r}: {exc}") from exc
    for key in toggles:
        if key not in TOGGLE_SETTINGS:
            _reject("toggle", key, TOGGLE_SETTINGS)


def kc_tag_min_confidence(gen_config: dict, toggles: dict) -> float:
    """The confidence gate a kc_tagging run will actually use.

    An explicit `kc_tag_min_confidence` wins over the `strict_kc_tagging` shorthand, so a sweep
    that sets both gets the number it wrote rather than a silent override.
    """
    explicit = gen_config.get("kc_tag_min_confidence")
    if explicit is not None:
        return float(explicit)
    return 0.7 if toggles.get("strict_kc_tagging") else 0.5


def resolved(suites: tuple[str, ...], gen_config: dict, toggles: dict) -> dict[str, str]:
    """The settings a run will really apply, for logging — the resolution, not the request."""
    if "kc_tagging" not in suites:
        return {}
    return {"applied_kc_tag_min_confidence": str(kc_tag_min_confidence(gen_config, toggles))}
