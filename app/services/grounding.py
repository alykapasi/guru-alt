"""What the tutor is told about the learner's passages (S28) — one policy for every turn.

Four situations, one sentence set each: passages or none, sources-only or not. Before this the
chat, practice and agent paths shared only the citation instruction, so an empty retrieval said
nothing at all and the model's fallback to general knowledge was never announced.
"""

from collections.abc import Sequence

from app.agent.untrusted import as_untrusted
from app.rag.retrieval import RetrievalHit

_CITE = (
    "When your answer draws on one of the numbered passages below, cite it inline immediately "
    'after the sentence that uses it, like this: "...as shown here [1]." Only cite a passage '
    "you actually used — never invent a number that isn't listed."
)
_CONFLICT = (
    "If the passages disagree with one another, say so and cite each side rather than "
    "silently choosing one."
)
_BEYOND = (
    "If you add anything the passages do not say, tell the learner in a plain sentence that "
    "this part comes from general knowledge rather than their materials."
)
_ONLY = (
    "This subject is set to sources-only: answer only from these passages. If they do not "
    "cover part of the question, name that part, say their materials do not cover it, and do "
    "not answer it from general knowledge."
)
_NOTHING = (
    "The learner's materials for this subject had nothing relevant to this message. Say so "
    "plainly, then answer from general knowledge."
)
_NOTHING_ONLY = (
    "The learner's materials for this subject had nothing relevant to this message, and the "
    "subject is set to sources-only. Tell them their materials do not cover this, and suggest "
    "adding a source or turning off sources-only for the subject. Do not answer from general "
    "knowledge."
)


def instruction(*, sources_only: bool, has_passages: bool) -> str:
    """The rule for one turn. Callers with no library scope at all never ask."""
    if not has_passages:
        return _NOTHING_ONLY if sources_only else _NOTHING
    return " ".join((_CITE, _CONFLICT, _ONLY if sources_only else _BEYOND))


def format_grounding(hits: Sequence[RetrievalHit], *, sources_only: bool) -> str:
    """The grounding section of a system prompt: the rule, then the passages fenced as data.

    Never empty for a scoped turn — "nothing matched" is something the tutor must be told, or
    it answers from general knowledge without saying so.
    """
    rule = instruction(sources_only=sources_only, has_passages=bool(hits))
    if not hits:
        return rule
    passages = "\n".join(f"[{i}] {hit.text}" for i, hit in enumerate(hits, start=1))
    # Fenced as data (S31): a passage is whatever someone uploaded, and an uploaded document
    # can contain a sentence addressed to the model.
    return f"{rule}\n\n{as_untrusted('RETRIEVED PASSAGES', passages)}"


def policy_note(*, sources_only: bool) -> str:
    """The same rule for the agent, whose passages arrive in ``search_materials`` results."""
    found = _ONLY if sources_only else _BEYOND
    missing = (
        "If a search finds nothing relevant, tell the learner their materials do not cover "
        "this and do not answer from general knowledge."
        if sources_only
        else "If a search finds nothing relevant, say so, then answer from general knowledge."
    )
    # Sources-only has to require the search: this flow's model may otherwise decide a search
    # would not help and answer from general knowledge, which is the one thing the setting
    # forbids. Chat and practice cannot skip it, because they retrieve before the model runs.
    must_search = (
        ("Before answering, search the learner's materials with search_materials.",)
        if sources_only
        else ()
    )
    return " ".join(
        (
            *must_search,
            "Passages come from the search_materials tool.",
            found,
            _CONFLICT,
            missing,
        )
    )
