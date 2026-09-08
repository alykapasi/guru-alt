"""Notes policy layer: parsing, the learner-atom invariant, distill/absorb/render."""

import json
from collections.abc import Sequence
from typing import Any

from app.learning import note_distill
from app.llm.providers.fake import FakeProvider
from app.llm.registry import LLMClient, ModelSpec, fake_llm_client
from app.llm.types import ChatMessage, ChatResponse, ModelRole, ToolDef


class _TruncatingProvider(FakeProvider):
    """A provider whose completions report having hit the output cap."""

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        response = await super().complete(
            model=model, messages=messages, system=system, max_tokens=max_tokens, tools=tools
        )
        return response.model_copy(update={"truncated": True})


def _truncating_client(reply: str) -> LLMClient:
    return LLMClient(
        {"fake": _TruncatingProvider(reply=reply)},
        {r: ModelSpec("fake", "fake-1") for r in ModelRole},
    )


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


TOPIC = note_distill.TopicContext(
    name="Integration by parts",
    subject_name="Calculus",
    kcs=(("11111111-1111-1111-1111-111111111111", "Choosing u and dv"),),
)


class TestDistill:
    async def test_distill_returns_atoms(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT]))
        result, _usage = await note_distill.distill(
            llm,
            topic=TOPIC,
            atoms=[],
            transcript="tutor: dot products…",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and not result.no_change
        assert result.atoms is not None and result.atoms[0]["md"] == CONCEPT["md"]

    async def test_distill_no_change(self) -> None:
        llm = fake_llm_client('{"no_change": true}')
        result, _ = await note_distill.distill(
            llm, topic=TOPIC, atoms=[CONCEPT], transcript="t", outcomes="", reading_level=None
        )
        assert result is not None and result.no_change and result.atoms is None

    async def test_distill_parse_failure_returns_none(self) -> None:
        llm = fake_llm_client("garbage")
        result, _ = await note_distill.distill(
            llm, topic=TOPIC, atoms=[], transcript="t", outcomes="", reading_level=None
        )
        assert result is None

    async def test_learner_atom_invariant_rejects_dropping(self) -> None:
        """THE key regression test: a distill that loses a learner atom is rejected."""
        llm = fake_llm_client(_atoms_reply([CONCEPT]))  # reply omits LEARNER_ATOM
        result, _ = await note_distill.distill(
            llm,
            topic=TOPIC,
            atoms=[CONCEPT, LEARNER_ATOM],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is None

    async def test_learner_atom_carried_is_accepted(self) -> None:
        llm = fake_llm_client(_atoms_reply([CONCEPT, LEARNER_ATOM]))
        result, _ = await note_distill.distill(
            llm,
            topic=TOPIC,
            atoms=[CONCEPT, LEARNER_ATOM],
            transcript="t",
            outcomes="",
            reading_level=None,
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
        assert md is not None and md.startswith("# Dot products")

    async def test_an_empty_render_is_refused_rather_than_returned(self) -> None:
        """Free-text markdown has no parse step to fail, so nothing else catches this."""
        md, _usage = await note_distill.render(
            fake_llm_client("   \n  "),
            atoms=[CONCEPT],
            note_format="narrative",
            reading_level=None,
        )
        assert md is None

    async def test_a_severed_render_is_refused(self) -> None:
        """A model that hit its output cap returns half a note and no error."""
        llm = _truncating_client("# Dot products\n\nThey measure")
        md, _usage = await note_distill.render(
            llm, atoms=[CONCEPT], note_format="narrative", reading_level=None
        )
        assert md is None


class TestMechanicalRender:
    def test_groups_by_kind(self) -> None:
        md = note_distill.mechanical_render([CONCEPT, LEARNER_ATOM])
        assert "## Concepts" in md and "## Your notes" in md
        assert md.index("## Concepts") < md.index("## Your notes")
        assert CONCEPT["md"] in md and LEARNER_ATOM["md"] in md

    def test_empty_substrate(self) -> None:
        assert note_distill.mechanical_render([]) == ""


class TestTopicScopingAndProvenance:
    """S39: the merge is told which topic it is for, and every stored reference is real."""

    def test_the_prompt_names_the_topic_and_its_kc_catalog(self) -> None:
        rendered = TOPIC.as_prompt()
        assert "Integration by parts" in rendered and "Calculus" in rendered
        assert "11111111-1111-1111-1111-111111111111: Choosing u and dv" in rendered

    async def test_kc_tags_outside_the_topic_are_dropped(self) -> None:
        """A model-invented id is not a reference to anything."""
        atom = {
            **CONCEPT,
            "kc_ids": ["11111111-1111-1111-1111-111111111111", "not-a-kc-of-this-topic"],
        }
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([atom])),
            topic=TOPIC,
            atoms=[],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        assert result.atoms[0]["kc_ids"] == ["11111111-1111-1111-1111-111111111111"]

    async def test_cited_evidence_resolves_to_the_row_behind_the_label(self) -> None:
        atom = {**CONCEPT, "provenance": {"refs": ["m1", "o1"]}}
        refs = {
            "m1": {"kind": "message", "id": "msg-1"},
            "o1": {"kind": "attempt", "id": "att-1", "kc_id": None},
        }
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([atom])),
            topic=TOPIC,
            atoms=[],
            transcript="[m1] user: hi",
            outcomes="[o1] KC 'x': score=0.0",
            refs=refs,
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        assert result.atoms[0]["provenance"] == {"evidence": [refs["m1"], refs["o1"]]}

    async def test_a_reference_to_evidence_that_was_never_supplied_is_discarded(self) -> None:
        atom = {**CONCEPT, "provenance": {"refs": ["m1", "m99"], "note": "invented"}}
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([atom])),
            topic=TOPIC,
            atoms=[],
            transcript="[m1] user: hi",
            outcomes="",
            refs={"m1": {"kind": "message", "id": "msg-1"}},
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        # The unknown label is gone, and so is the free-text field the model added itself.
        assert result.atoms[0]["provenance"] == {"evidence": [{"kind": "message", "id": "msg-1"}]}

    async def test_a_carried_forward_atom_keeps_the_lineage_it_already_had(self) -> None:
        prior = {**CONCEPT, "provenance": {"evidence": [{"kind": "message", "id": "old"}]}}
        returned = {**CONCEPT, "provenance": {"refs": ["m1"]}}
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([returned])),
            topic=TOPIC,
            atoms=[prior],
            transcript="[m1] user: hi",
            outcomes="",
            refs={"m1": {"kind": "message", "id": "new"}},
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        assert result.atoms[0]["provenance"]["evidence"] == [
            {"kind": "message", "id": "old"},
            {"kind": "message", "id": "new"},
        ]


class TestLearnerContentIsTheLearners:
    """S40: 'learner' means the learner wrote it, and a merge cannot make that untrue."""

    async def test_a_merge_cannot_reword_a_learner_atom_while_keeping_its_id(self) -> None:
        """Checking the id survived was never the guarantee — this is."""
        rewritten = {**LEARNER_ATOM, "md": "A mnemonic I never wrote."}
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([CONCEPT, rewritten])),
            topic=TOPIC,
            atoms=[CONCEPT, LEARNER_ATOM],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        carried = next(a for a in result.atoms if a["id"] == "a-L1")
        assert carried["md"] == LEARNER_ATOM["md"]

    async def test_a_merge_cannot_reclassify_a_learner_atom(self) -> None:
        reclassified = {**LEARNER_ATOM, "kind": "concept"}
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([reclassified])),
            topic=TOPIC,
            atoms=[LEARNER_ATOM],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        assert result.atoms[0]["kind"] == "learner"
        assert result.atoms[0]["md"] == LEARNER_ATOM["md"]

    async def test_a_merge_cannot_invent_a_learner_atom(self) -> None:
        """Content in 'Your notes' the learner never wrote is a lie about them."""
        invented = {"kind": "learner", "kc_ids": [], "md": "I find this easy!", "provenance": {}}
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([invented])),
            topic=TOPIC,
            atoms=[],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        # Kept as content, but no longer attributed to the learner.
        assert result.atoms[0]["kind"] == "concept"
        assert result.atoms[0]["md"] == "I find this easy!"

    async def test_a_merge_may_still_reorder_learner_atoms(self) -> None:
        """The invariant is about content, not position."""
        result, _ = await note_distill.distill(
            fake_llm_client(_atoms_reply([LEARNER_ATOM, CONCEPT])),
            topic=TOPIC,
            atoms=[CONCEPT, LEARNER_ATOM],
            transcript="t",
            outcomes="",
            reading_level=None,
        )
        assert result is not None and result.atoms is not None
        assert [a["id"] for a in result.atoms] == ["a-L1", "a-c1"]

    async def test_absorb_still_lets_the_learner_change_their_own_words(self) -> None:
        """Their own edit is the authority — the immutability rule is for the *merge*."""
        edited = {**LEARNER_ATOM, "md": "My better mnemonic."}
        atoms, _ = await note_distill.absorb(
            fake_llm_client(_atoms_reply([edited])),
            atoms=[LEARNER_ATOM],
            previous_render="whatever",
            edited_md="My better mnemonic.",
        )
        assert atoms is not None and atoms[0]["md"] == "My better mnemonic."


class TestSubstrateWindow:
    """A long note is not resent whole and asked for whole back."""

    @staticmethod
    def _atoms(n: int) -> list[dict]:
        return [
            {"id": f"a{i}", "kind": "concept", "kc_ids": [], "md": f"fact {i}", "provenance": {}}
            for i in range(n)
        ]

    async def test_atoms_beyond_the_window_are_carried_through_untouched(self) -> None:
        prior = self._atoms(10)
        reply = json.dumps(
            {
                "atoms": [
                    {"id": "a8", "kind": "concept", "kc_ids": [], "md": "fact 8 revised"},
                    {"id": "a9", "kind": "concept", "kc_ids": [], "md": "fact 9"},
                ]
            }
        )
        result, _usage = await note_distill.distill(
            fake_llm_client(reply),
            topic=note_distill.TopicContext(name="t", subject_name="s", description="", kcs=()),
            atoms=prior,
            transcript="",
            outcomes="",
            reading_level=None,
            max_atoms=2,
        )

        assert result is not None and result.atoms is not None
        # The eight settled atoms survive a merge that never saw them...
        assert [a["md"] for a in result.atoms[:8]] == [f"fact {i}" for i in range(8)]
        # ...and the two that were offered come back as the model returned them.
        assert [a["md"] for a in result.atoms[8:]] == ["fact 8 revised", "fact 9"]

    async def test_a_short_note_is_offered_whole(self) -> None:
        prior = self._atoms(2)
        reply = json.dumps({"atoms": [{"id": "a0", "kind": "concept", "kc_ids": [], "md": "one"}]})
        result, _usage = await note_distill.distill(
            fake_llm_client(reply),
            topic=note_distill.TopicContext(name="t", subject_name="s", description="", kcs=()),
            atoms=prior,
            transcript="",
            outcomes="",
            reading_level=None,
            max_atoms=60,
        )
        assert result is not None and result.atoms is not None
        assert [a["md"] for a in result.atoms] == ["one"]  # a real merge, nothing carried
