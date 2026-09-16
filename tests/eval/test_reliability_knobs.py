"""How many numbers would a reading have to settle? (S18.)

S18 has sat at *Accepted · needs data* while the thing it describes grew: every pass that closed
a loop added another threshold, each admitting in its own comment that it was a taste parameter.
The inventory is one place to count them, and these tests are what stop it becoming fiction —
an entry whose value no longer matches the code is a number somebody re-guessed without saying.
"""

import pytest

from tests.eval.reliability import knobs
from tests.eval.reliability.report import render_knobs

# --- S18: how many numbers would a reading have to settle? -------------------


def test_every_inventoried_constant_still_matches_the_code() -> None:
    """The inventory's whole value is that it is current. A drifted entry means somebody
    re-guessed an uncalibrated number without saying so, and re-guessing a number nobody has
    measured is a decision rather than an edit.
    """
    assert knobs.drifted() == []


def test_every_knob_names_the_reading_that_would_settle_it() -> None:
    """An inventory that says "needs data" against each row is the sentence S18 has carried for
    sixteen passes. Each entry has to name the specific reading, so the list doubles as the
    brief for what S59 must eventually be able to answer."""
    for knob in knobs.KNOBS:
        assert knob.settled_by.strip(), knob.id
        assert knob.governs.strip(), knob.id
        assert "needs data" not in knob.settled_by.lower(), knob.id


def test_knob_ids_are_unique_and_reach_real_places() -> None:
    ids = [k.id for k in knobs.KNOBS]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(knobs.live()), "the inventory and the live reader name the same knobs"
    for knob in knobs.KNOBS:
        assert knob.where.startswith("app."), knob.id


def test_a_drifted_knob_is_reported_rather_than_quietly_reconciled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the report does when the check above fails. Silently reconciling to whatever the
    code now holds would make the inventory a mirror of the constants instead of a record of
    what was last looked at, and the drift would never be anyone's to notice."""
    real = dict(knobs.live())
    monkeypatch.setattr(knobs, "live", lambda: {**real, "detour.min_failures": 99.0})

    out = render_knobs()

    assert "DRIFTED" in out
    assert "detour.min_failures" in out
    assert "not an edit" in out
