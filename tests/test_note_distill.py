"""Notes policy layer: parsing, the learner-atom invariant, distill/absorb/render."""

import json
from typing import Any

from app.learning import note_distill
from app.llm.registry import fake_llm_client

CONCEPT: dict[str, Any] = {
    "id": "a-c1",
    "kind": "concept",
    "kc_ids": [],
    "md": "Dot products measure alignment.",
    "provenance": {},
}
LEARNER_ATOM: dict[str, Any] = {
    "id": "a-L1",
    "kind": "learner",
    "kc_ids": [],
    "md": "My mnemonic: SOH-CAH-TOA.",
    "provenance": {},
}


def _atoms_reply(atoms: list[dict]) -> str:
    return json.dumps({"atoms": atoms})


class TestParsing:
    def test_valid_payload(self) -> None:
        data = note_distill.parse_atoms_payload(_atoms_reply([CONCEPT]))
        assert data is not None and data["atoms"][0]["md"] == CONCEPT["md"]

    def test_fenced_payload(self) -> None:
        fenced = f"```json\n{_atoms_reply([CONCEPT])}\n```"
        assert note_distill.parse_atoms_payload(fenced) is not None

    def test_no_change_payload(self) -> None:
        data = note_distill.parse_atoms_payload('{"no_change": true}')
        assert data == {"no_change": True}

    def test_garbage_returns_none(self) -> None:
        assert note_distill.parse_atoms_payload("not json at all") is None

    def test_bad_kind_returns_none(self) -> None:
        bad = _atoms_reply([{"kind": "poem", "md": "x"}])
        assert note_distill.parse_atoms_payload(bad) is None

    def test_missing_md_returns_none(self) -> None:
        assert note_distill.parse_atoms_payload(_atoms_reply([{"kind": "concept"}])) is None


class TestAssignIds:
    def test_missing_ids_filled_existing_kept(self) -> None:
        atoms = note_distill.assign_atom_ids(
            [dict(CONCEPT), {"kind": "example", "kc_ids": [], "md": "x"}]
        )
        assert atoms[0]["id"] == "a-c1"
        assert atoms[1]["id"].startswith("a-") and len(atoms[1]["id"]) > 2


class TestDistill:
    async def test_distill_returns_atoms(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT]))
        result, _usage = await note_distill.distill(
            llm, atoms=[], transcript="tutor: dot products…", outcomes="", reading_level=None
        )
        assert result is not None and not result.no_change
        assert result.atoms is not None and result.atoms[0]["md"] == CONCEPT["md"]

    async def test_distill_no_change(self) -> None:
        llm = fake_llm_client('{"no_change": true}')
        result, _ = await note_distill.distill(
            llm, atoms=[CONCEPT], transcript="t", outcomes="", reading_level=None
        )
        assert result is not None and result.no_change and result.atoms is None

    async def test_distill_parse_failure_returns_none(self) -> None:
        llm = fake_llm_client("garbage")
        result, _ = await note_distill.distill(
            llm, atoms=[], transcript="t", outcomes="", reading_level=None
        )
        assert result is None

    async def test_learner_atom_invariant_rejects_dropping(self) -> None:
        """THE key regression test: a distill that loses a learner atom is rejected."""
        llm = fake_llm_client(_atoms_reply([CONCEPT]))  # reply omits LEARNER_ATOM
        result, _ = await note_distill.distill(
            llm, atoms=[CONCEPT, LEARNER_ATOM], transcript="t", outcomes="", reading_level=None
        )
        assert result is None

    async def test_learner_atom_carried_is_accepted(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT, LEARNER_ATOM]))
        result, _ = await note_distill.distill(
            llm, atoms=[CONCEPT, LEARNER_ATOM], transcript="t", outcomes="", reading_level=None
        )
        assert result is not None and result.atoms is not None
        assert any(a["id"] == "a-L1" for a in result.atoms)


class TestAbsorb:
    async def test_absorb_returns_atoms_and_may_drop_learner_atoms(self) -> None:
        """Absorb trusts the learner: their edit may delete even their own earlier atoms."""
        llm = fake_llm_client(_atoms_reply([CONCEPT]))
        atoms, _ = await note_distill.absorb(
            llm, atoms=[CONCEPT, LEARNER_ATOM], previous_render="…", edited_md="…"
        )
        assert atoms is not None and len(atoms) == 1

    async def test_absorb_failure_returns_none(self) -> None:
        llm = fake_llm_client("nope")
        atoms, _ = await note_distill.absorb(llm, atoms=[], previous_render="", edited_md="x")
        assert atoms is None


class TestRender:
    async def test_render_returns_markdown(self) -> None:
        llm = fake_llm_client("# Dot products\n\nThey measure alignment.")
        md, _usage = await note_distill.render(
            llm, atoms=[CONCEPT], note_format="narrative", reading_level=None
        )
        assert md.startswith("# Dot products")


class TestMechanicalRender:
    def test_groups_by_kind(self) -> None:
        md = note_distill.mechanical_render([CONCEPT, LEARNER_ATOM])
        assert "## Concepts" in md and "## Your notes" in md
        assert md.index("## Concepts") < md.index("## Your notes")
        assert CONCEPT["md"] in md and LEARNER_ATOM["md"] in md

    def test_empty_substrate(self) -> None:
        assert note_distill.mechanical_render([]) == ""
