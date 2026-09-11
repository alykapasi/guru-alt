"""Notes policy layer: distillation, edit absorption, rendering (MASTERPLAN §4.9, Phase 8).

The substrate/projection split: a format-neutral list of KC-tagged *atoms* is the source of
truth; rendered notes are projections of it into a named format. Mirrors ``curriculum.py``'s
tolerant-parse idiom — policy functions return ``None`` on an unusable reply, never raise.

The one hard guarantee lives here, and it is enforced in code rather than prompted for: a
**distill** merge cannot drop a learner-contributed atom (the merge is rejected), cannot
rewrite or reclassify one (the stored copy is carried through verbatim), and cannot invent one
(``_carry_learner_atoms``, ``_demote_invented_learner_atom``). **Absorb** deliberately skips
all of that — the learner's own edit is the authority over their own content.
"""

import json
import uuid
from dataclasses import dataclass

import structlog

from app.llm import ChatMessage, ChatRole, LLMClient
from app.llm.types import ModelRole, Usage

log = structlog.get_logger(__name__)

# How many atoms a single merge may see and rewrite. Older atoms are carried through
# untouched, so a note can grow past this without the merge having to fit in one reply.
DISTILL_MAX_ATOMS = 60

NOTES_ROLE = ModelRole.SMART
FORMATS = ("outline", "narrative", "mnemonic", "worked_examples")
FALLBACK_FORMAT = "outline"
ATOM_KINDS = ("concept", "example", "callout", "learner")

_KIND_HEADINGS = {
    "concept": "## Concepts",
    "example": "## Worked examples",
    "callout": "## Watch out",
    "learner": "## Your notes",
}

DISTILL_SYSTEM_PROMPT = (
    "You maintain a learner's personal study notes as a list of atoms — small, self-contained "
    "markdown units. Kinds: 'concept' (an explanation of covered material), 'example' (a worked "
    "example, ideally one the learner actually worked through), 'callout' (a warning about "
    "something THIS learner got wrong, naming the actual confusion), 'learner' (content the "
    "learner wrote themselves — carry every one of these forward, keeping its id and its "
    "text exactly; you may move one, but never drop, reword, or reclassify one, and never "
    "create a new atom of this kind: it means 'the learner wrote this'). "
    "Fold the new material and outcomes into the existing atoms: restructure freely, merge "
    "duplicates, keep ids for atoms you carry forward, omit ids for genuinely new atoms.\n\n"
    "These notes belong to ONE topic, named at the top of the prompt. The study activity you "
    "are shown is gathered across the learner's whole subject, so much of it may be about "
    "sibling topics: use only what is genuinely about THIS topic, and reply "
    '{"no_change": true} if none of it is. Tag atoms only with knowledge-component ids from '
    "the catalog given. Cite the evidence each atom came from using the bracketed labels in "
    'the prompt, as "provenance": {"refs": ["m3", "o1"]} — only labels that actually appear.\n\n'
    'Reply with JSON only: {"atoms": [{"id": "...", "kind": "...", "kc_ids": [], "md": "...", '
    '"provenance": {"refs": []}}]} — or {"no_change": true}.'
)

ABSORB_SYSTEM_PROMPT = (
    "The learner edited the rendered version of their study notes. Update the underlying atom "
    "list to match their intent: content they added becomes new 'learner' atoms; content they "
    "changed updates the matching atom; content they removed is deleted (their edit is the "
    "authority, even over their own earlier notes). Keep ids for atoms you carry forward. "
    'Reply with JSON only: {"atoms": [...]} in the same atom schema you were given.'
)

RENDER_SYSTEM_PROMPT = (
    "You write a learner's personal study notes as one coherent markdown document from the "
    "atom list given. The atoms are your source material — order and group them as the format "
    "demands; do not invent facts that are not in the atoms; keep every 'callout' visible. "
    "Write an actual document, not a list of fragments."
)

_FORMAT_INSTRUCTIONS = {
    "outline": "Format: dense structured outline — nested bullets, bold key terms, minimal prose.",
    "narrative": "Format: flowing narrative prose with short headed sections.",
    "mnemonic": "Format: memory-hook heavy — lead each section with a mnemonic, acronym, or vivid anchor.",
    "worked_examples": "Format: example-led — each section opens with a worked example, then the principle.",
}


@dataclass(frozen=True)
class DistillResult:
    """``atoms`` is None iff ``no_change``."""

    atoms: list[dict] | None
    no_change: bool


@dataclass(frozen=True)
class TopicContext:
    """Which topic a note is for, and the only KCs an atom may legitimately be tagged with.

    Without this the merge prompt said "this topic" without ever naming one, and the transcript
    it was given spans the whole subject — so a conversation about one topic could populate
    every sibling topic's note, most visibly when those notes started empty and the atom list
    carried no hint of what the topic was.
    """

    name: str
    subject_name: str
    description: str | None = None
    kcs: tuple[tuple[str, str], ...] = ()
    """(kc_id, kc name) for every KC in the topic — the catalog atoms may cite."""

    @property
    def allowed_kc_ids(self) -> set[str]:
        return {kc_id for kc_id, _ in self.kcs}

    def as_prompt(self) -> str:
        described = f" — {self.description}" if self.description else ""
        catalog = (
            "\n".join(f"- {kc_id}: {name}" for kc_id, name in self.kcs) or "- (none defined yet)"
        )
        return (
            f"Topic: {self.name!r} in the subject {self.subject_name!r}{described}\n"
            f"Knowledge components in this topic — use only these ids in kc_ids:\n{catalog}"
        )


def _validated_kc_ids(atom: dict, allowed: set[str]) -> list[str]:
    """Keep only tags that name a KC of this topic. A model-invented id is not a reference."""
    kept = [k for k in atom["kc_ids"] if k in allowed]
    if len(kept) != len(atom["kc_ids"]):
        log.warning(
            "note_distill.unknown_kc_ids", dropped=len(atom["kc_ids"]) - len(kept), atom=atom["id"]
        )
    return kept


def _resolved_provenance(atom: dict, prior: dict | None, refs: dict[str, dict]) -> dict:
    """Lineage is server-owned: only labels for evidence actually supplied become references.

    The model cites transient labels (``m3``, ``o1``); those resolve to the durable message and
    attempt ids behind them, so a stored reference still means something after the prompt that
    produced it is gone. An atom carried forward keeps the lineage it already had, and anything
    the model wrote into ``provenance`` other than ``refs`` is discarded rather than trusted.
    """
    evidence: list[dict] = list((prior or {}).get("provenance", {}).get("evidence", []))
    seen = {(e.get("kind"), e.get("id")) for e in evidence}
    cited = atom.get("provenance", {}).get("refs")
    for ref in cited if isinstance(cited, list) else []:
        source = refs.get(str(ref))
        if source is None:
            log.warning("note_distill.unknown_provenance_ref", ref=str(ref)[:40])
            continue
        key = (source["kind"], source["id"])
        if key not in seen:
            seen.add(key)
            evidence.append(source)
    return {"evidence": evidence}


def _extract_json(content: str) -> str:
    """First '{' to last '}' — tolerates markdown fences. Raises ValueError if absent."""
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("no JSON object in reply")
    return content[start : end + 1]


def parse_atoms_payload(reply: str) -> dict | None:
    """Parse a distill/absorb reply into ``{"no_change": True}`` or ``{"atoms": [...]}``.

    Shape-validates every atom (kind, non-empty md, list kc_ids). Returns None on anything
    unusable — the caller keeps the old substrate.
    """
    try:
        data = json.loads(_extract_json(reply))
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("note_distill.parse_failed", error=type(exc).__name__)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("no_change") is True:
        return {"no_change": True}
    atoms = data.get("atoms")
    if not isinstance(atoms, list):
        log.warning("note_distill.parse_failed", reason="atoms_not_list")
        return None
    cleaned: list[dict] = []
    for atom in atoms:
        if not isinstance(atom, dict):
            return None
        if atom.get("kind") not in ATOM_KINDS:
            log.warning("note_distill.parse_failed", reason="bad_kind", kind=atom.get("kind"))
            return None
        md = atom.get("md")
        if not isinstance(md, str) or not md.strip():
            log.warning("note_distill.parse_failed", reason="missing_md")
            return None
        kc_ids = atom.get("kc_ids")
        cleaned.append(
            {
                "id": atom.get("id") if isinstance(atom.get("id"), str) else None,
                "kind": atom["kind"],
                "kc_ids": [str(k) for k in kc_ids] if isinstance(kc_ids, list) else [],
                "md": md.strip(),
                "provenance": atom.get("provenance")
                if isinstance(atom.get("provenance"), dict)
                else {},
            }
        )
    return {"atoms": cleaned}


def assign_atom_ids(atoms: list[dict]) -> list[dict]:
    """Fill missing/None ids with fresh short ids; existing ids are kept untouched."""
    for atom in atoms:
        if not atom.get("id"):
            atom["id"] = f"a-{uuid.uuid4().hex[:6]}"
    return atoms


def _learner_atoms(atoms: list[dict]) -> dict[str, dict]:
    return {a["id"]: a for a in atoms if a.get("kind") == "learner" and a.get("id")}


def _demote_invented_learner_atom(atom: dict, prior_learner: dict[str, dict]) -> None:
    """A merge may not put words in the learner's mouth.

    'learner' means "the learner wrote this", and only an actual edit (``absorb``) can produce
    one. A merge that invents one is keeping the content but not the attribution, so it is
    re-kinded rather than dropped.
    """
    if atom["kind"] == "learner" and atom["id"] not in prior_learner:
        log.warning("note_distill.invented_learner_atom", atom=atom["id"])
        atom["kind"] = "concept"


def _carry_learner_atoms(prior: list[dict], new: list[dict]) -> list[dict] | None:
    """Copy the learner's own atoms through the merge verbatim. ``None`` if one was dropped.

    Checking that the *id* survived was never the guarantee the docstring claimed: an atom
    keeping its id while its text changed passed, so the model could rewrite what the learner
    said and the note would still be presented as theirs. The merge may reorder learner atoms
    and nothing else about them — text, kind, tags and lineage all come from the stored copy.
    """
    prior_learner = _learner_atoms(prior)
    carried: list[dict] = []
    seen: set[str] = set()
    for atom in new:
        original = prior_learner.get(atom["id"])
        if original is None:
            carried.append(atom)
            continue
        seen.add(original["id"])
        if atom.get("md") != original.get("md") or atom.get("kind") != original.get("kind"):
            log.warning("note_distill.learner_atom_rewritten", atom=original["id"])
        carried.append(dict(original))
    if set(prior_learner) - seen:
        return None
    return carried


async def distill(
    llm: LLMClient,
    *,
    topic: TopicContext,
    atoms: list[dict],
    transcript: str,
    outcomes: str,
    refs: dict[str, dict] | None = None,
    max_atoms: int = DISTILL_MAX_ATOMS,
) -> tuple[DistillResult | None, Usage]:
    """One merge: current atoms + new material + outcomes -> updated atom list.

    ``refs`` maps the bracketed labels used in ``transcript``/``outcomes`` to the durable rows
    behind them; every reference the model cites is resolved through it, and every KC tag is
    checked against ``topic``. Returns (None, usage) on parse failure or a learner-atom
    violation.

    ``max_atoms`` bounds how much of the substrate is put in front of the model; anything
    older is preserved verbatim and rejoined afterwards. The cost is that a merge can no
    longer revise a settled atom — which is the price of it not being able to lose one.
    """
    refs = refs or {}
    # Only the tail of a long note is offered for rewriting; everything before it is carried
    # through untouched. A note used to be resent whole and asked for whole back, under a fixed
    # 4096-token output cap — so past a certain size the reply could not contain the note, and
    # what came back was a shorter note that had quietly lost the difference.
    settled, atoms = atoms[:-max_atoms] if len(atoms) > max_atoms else [], atoms[-max_atoms:]
    if settled:
        log.info("note_distill.substrate_windowed", carried=len(settled), offered=len(atoms))
    prompt = (
        f"{topic.as_prompt()}\n\n"
        f"Current atoms:\n{json.dumps(atoms, indent=2)}\n\n"
        f"New study activity, from conversations across the whole subject — only some of it "
        f"may concern this topic:\n{transcript or '(none)'}\n\n"
        f"The learner's graded outcomes on this topic (build 'callout' atoms from real "
        f"mistakes here):\n{outcomes or '(none)'}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=DISTILL_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    data = parse_atoms_payload(completion.content)
    if data is None:
        return None, completion.usage
    if data.get("no_change"):
        return DistillResult(atoms=None, no_change=True), completion.usage
    new_atoms = assign_atom_ids(data["atoms"])
    prior_by_id = {a["id"]: a for a in atoms if a.get("id")}
    prior_learner = _learner_atoms(atoms)
    allowed = topic.allowed_kc_ids
    for atom in new_atoms:
        atom["kc_ids"] = _validated_kc_ids(atom, allowed)
        atom["provenance"] = _resolved_provenance(atom, prior_by_id.get(atom["id"]), refs)
        _demote_invented_learner_atom(atom, prior_learner)
    carried = _carry_learner_atoms(atoms, new_atoms)
    if carried is None:
        log.warning("note_distill.learner_atom_dropped")
        return None, completion.usage
    return DistillResult(atoms=settled + carried, no_change=False), completion.usage


async def absorb(
    llm: LLMClient,
    *,
    atoms: list[dict],
    previous_render: str,
    edited_md: str,
) -> tuple[list[dict] | None, Usage]:
    """Fold a learner's edit of the rendered note back into the substrate."""
    prompt = (
        f"Current atoms:\n{json.dumps(atoms, indent=2)}\n\n"
        f"The rendered note they started from:\n{previous_render}\n\n"
        f"Their edited version:\n{edited_md}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=ABSORB_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    data = parse_atoms_payload(completion.content)
    if data is None or data.get("no_change"):
        return None, completion.usage
    return assign_atom_ids(data["atoms"]), completion.usage


async def render(
    llm: LLMClient,
    *,
    atoms: list[dict],
    note_format: str,
) -> tuple[str | None, Usage]:
    """Project the substrate into one format, or ``None`` if the projection cannot be trusted.

    Free-text markdown, so there is no parse step to fail — which is exactly why an empty or
    severed reply used to be cached as the learner's note. Two things disqualify a render:
    nothing came back, or the model hit its output cap mid-note. In both cases the caller
    falls back to :func:`mechanical_render`, which is derived from the same substrate and
    always complete.
    """
    prompt = f"{_FORMAT_INSTRUCTIONS[note_format]}\n\nAtoms:\n{json.dumps(atoms, indent=2)}"
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=RENDER_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    content = completion.content.strip()
    if not content:
        log.warning("note_distill.render_empty", note_format=note_format)
        return None, completion.usage
    if completion.truncated:
        log.warning("note_distill.render_truncated", note_format=note_format)
        return None, completion.usage
    return content, completion.usage


def mechanical_render(atoms: list[dict]) -> str:
    """Deterministic, LLM-free markdown of a substrate — the revision source view."""
    sections: list[str] = []
    for kind in ATOM_KINDS:
        group = [a["md"] for a in atoms if a.get("kind") == kind]
        if group:
            sections.append("\n\n".join([_KIND_HEADINGS[kind], *group]))
    return "\n\n".join(sections)
