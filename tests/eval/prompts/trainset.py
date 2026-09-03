"""Golden kc_tagging cases -> DSPy examples with a deterministic train/dev split (Phase 9c)."""

from __future__ import annotations

import random

import dspy

from tests.eval.harness import KCTaggingCase, load_kc_tagging_cases


def to_example(case: KCTaggingCase) -> dspy.Example:
    catalog = "\n".join(f"{i}. {name}" for i, name in enumerate(case.candidates, start=1))
    return dspy.Example(
        passage=case.text, candidates=catalog, expect=list(case.expect)
    ).with_inputs("passage", "candidates")


def load_kc_tagging_examples(
    *, dev_fraction: float = 0.33, seed: int = 0
) -> tuple[list[dspy.Example], list[dspy.Example]]:
    """Deterministic train/dev split over the golden set (sorted by id, shuffled by seed)."""
    cases = sorted(load_kc_tagging_cases(), key=lambda c: c.id)
    examples = [to_example(c) for c in cases]
    random.Random(seed).shuffle(examples)
    n_dev = max(1, round(len(examples) * dev_fraction))
    return examples[n_dev:], examples[:n_dev]
