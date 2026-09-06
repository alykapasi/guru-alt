"""The expanded golden kc_tagging set is large + well-formed (Phase 9c)."""

from tests.eval.harness import load_kc_tagging_cases


def test_kc_tagging_set_is_large_and_well_formed() -> None:
    cases = load_kc_tagging_cases()
    assert len(cases) >= 24
    ids = [c.id for c in cases]
    assert len(ids) == len(set(ids))  # unique ids
    for c in cases:
        assert c.text and c.candidates
        assert all(1 <= i <= len(c.candidates) for i in c.expect)  # indices in range
