"""Notes policy layer: distillation, edit absorption, rendering (MASTERPLAN §4.9, Phase 8).

The substrate/projection split: a format-neutral list of KC-tagged *atoms* is the source of
truth; rendered notes are projections of it into a named format. Mirrors ``curriculum.py``'s
tolerant-parse idiom — policy functions return ``None`` on an unusable reply, never raise.

The one hard guarantee lives here: a **distill** merge that drops a learner-contributed atom
is rejected in code (``_learner_atoms_preserved``), not merely prompted against. **Absorb**
deliberately skips that check — the learner's own edit is the authority over their content.
"""

import json
import uuid
from dataclasses import dataclass

import structlog

from app.llm import ChatMessage, ChatRole, LLMClient
from app.llm.types import ModelRole, Usage

log = structlog.get_logger(__name__)

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
    "learner wrote themselves — carry every one of these forward, keeping its id; you may "
    "lightly edit its wording for flow but never drop one or change its meaning). "
    "Fold the new material and outcomes into the existing atoms: restructure freely, merge "
    "duplicates, keep ids for atoms you carry forward, omit ids for genuinely new atoms. "
    'Reply with JSON only: {"atoms": [{"id": "...", "kind": "...", "kc_ids": [], "md": "...", '
    '"provenance": {}}]} — or {"no_change": true} if the new activity contains nothing '
    "relevant to this topic."
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
        log.warning("note_distill.parse_failed", error=str(exc))
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


def _learner_atoms_preserved(prior: list[dict], new: list[dict]) -> bool:
    """Every learner-kind atom id in ``prior`` must appear in ``new`` (distill only)."""
    prior_ids = {a["id"] for a in prior if a.get("kind") == "learner" and a.get("id")}
    new_ids = {a.get("id") for a in new}
    return prior_ids <= new_ids


def _profile_line(reading_level: object) -> str:
    if reading_level is None:
        return ""
    return f"\n\nWrite at roughly this reading level: {reading_level}."


async def distill(
    llm: LLMClient,
    *,
    atoms: list[dict],
    transcript: str,
    outcomes: str,
    reading_level: object,
) -> tuple[DistillResult | None, Usage]:
    """One merge: current atoms + new material + outcomes -> updated atom list.

    Returns (None, usage) on parse failure or a learner-atom-invariant violation.
    """
    prompt = (
        f"Current atoms:\n{json.dumps(atoms, indent=2)}\n\n"
        f"New study activity (transcript excerpts):\n{transcript or '(none)'}\n\n"
        f"The learner's graded outcomes on this topic (build 'callout' atoms from real "
        f"mistakes here):\n{outcomes or '(none)'}"
        f"{_profile_line(reading_level)}"
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
    new_atoms = data["atoms"]
    if not _learner_atoms_preserved(atoms, new_atoms):
        log.warning("note_distill.learner_atom_dropped")
        return None, completion.usage
    return DistillResult(atoms=assign_atom_ids(new_atoms), no_change=False), completion.usage


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
    reading_level: object,
) -> tuple[str, Usage]:
    """Project the substrate into one format. Free-text markdown; no parsing to fail."""
    prompt = (
        f"{_FORMAT_INSTRUCTIONS[note_format]}{_profile_line(reading_level)}\n\n"
        f"Atoms:\n{json.dumps(atoms, indent=2)}"
    )
    completion = await llm.complete(
        NOTES_ROLE,
        [ChatMessage(role=ChatRole.USER, content=prompt)],
        system=RENDER_SYSTEM_PROMPT,
        max_tokens=4096,
    )
    return completion.content.strip(), completion.usage


def mechanical_render(atoms: list[dict]) -> str:
    """Deterministic, LLM-free markdown of a substrate — the revision source view."""
    sections: list[str] = []
    for kind in ATOM_KINDS:
        group = [a["md"] for a in atoms if a.get("kind") == kind]
        if group:
            sections.append("\n\n".join([_KIND_HEADINGS[kind], *group]))
    return "\n\n".join(sections)
