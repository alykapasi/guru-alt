"""Deterministic train/dev split + exact-set-match metric (Phase 9c)."""

import types

from tests.eval.prompts.metric import kc_set_match
from tests.eval.prompts.trainset import load_kc_tagging_examples


def test_split_is_deterministic_and_disjoint() -> None:
    train_a, dev_a = load_kc_tagging_examples(seed=0)
    train_b, dev_b = load_kc_tagging_examples(seed=0)
    assert [e.passage for e in train_a] == [e.passage for e in train_b]  # deterministic
    assert [e.passage for e in dev_a] == [e.passage for e in dev_b]
    train_ids = {e.passage for e in train_a}
    dev_ids = {e.passage for e in dev_a}
    assert train_ids.isdisjoint(dev_ids)  # no leakage
    assert len(train_a) > 0 and len(dev_a) > 0


def test_metric_is_exact_set_match() -> None:
    example = types.SimpleNamespace(expect=[1, 3])
    good = types.SimpleNamespace(tags=[types.SimpleNamespace(kc=3), types.SimpleNamespace(kc=1)])
    bad = types.SimpleNamespace(tags=[types.SimpleNamespace(kc=1)])
    assert kc_set_match(example, good) is True  # order-insensitive
    assert kc_set_match(example, bad) is False
