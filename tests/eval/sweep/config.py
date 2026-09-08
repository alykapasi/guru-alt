"""Sweep config: a declarative YAML matrix expanded into fully-resolved cells."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.llm.registry import ModelSpec
from app.llm.types import ModelRole
from tests.eval.sweep.settings import validate_settings


class ModelCandidate(BaseModel):
    """One candidate model for a role axis."""

    provider: str
    model: str

    def to_spec(self) -> ModelSpec:
        return ModelSpec(provider=self.provider, model=self.model)


class SweepConfig(BaseModel):
    """A parsed sweep file. ``axes`` maps a role to its candidate models."""

    name: str
    suites: list[str]
    axes: dict[ModelRole, list[ModelCandidate]] = Field(default_factory=dict)
    gen_config: list[dict] = Field(default_factory=lambda: [{}])
    toggles: dict[str, list[bool]] = Field(default_factory=dict)


@dataclass(frozen=True)
class Cell:
    """One fully-resolved experiment configuration (one point in the sweep matrix)."""

    id: str
    role_overrides: dict[ModelRole, ModelSpec]
    gen_config: dict
    toggles: dict[str, bool]
    suites: tuple[str, ...]


def load_sweep_config(path: str | Path) -> SweepConfig:
    data = yaml.safe_load(Path(path).read_text())
    return SweepConfig.model_validate(data)


def expand(config: SweepConfig) -> list[Cell]:
    """Cartesian product of every axis (roles x gen_config x toggles) -> one Cell per combination.

    Raises :class:`~tests.eval.sweep.settings.UnsupportedSetting` if the config names a knob no
    suite applies. This fires before the first paid call, because the failure mode it prevents —
    a sweep that reports comparing settings it never varied — is invisible once the run finishes.
    """
    for gen in config.gen_config or [{}]:
        validate_settings(gen, config.toggles)
    roles = list(config.axes)
    role_choices = [config.axes[r] for r in roles]
    toggle_names = list(config.toggles)
    toggle_choices = [config.toggles[t] for t in toggle_names]

    role_combos = itertools.product(*role_choices) if role_choices else [()]
    toggle_combos = itertools.product(*toggle_choices) if toggle_choices else [()]

    cells: list[Cell] = []
    for i, (role_pick, gen, toggle_pick) in enumerate(
        itertools.product(role_combos, config.gen_config or [{}], toggle_combos)
    ):
        cells.append(
            Cell(
                id=f"{config.name}-{i:03d}",
                role_overrides={roles[j]: role_pick[j].to_spec() for j in range(len(roles))},
                gen_config=gen,
                toggles={toggle_names[j]: toggle_pick[j] for j in range(len(toggle_names))},
                suites=tuple(config.suites),
            )
        )
    return cells
