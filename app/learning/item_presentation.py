"""Split an item's ``answer_key`` into the part a learner may see (TECHNICAL_DESIGN §7.6).

``Item.answer_key`` mixes *presentation* data with the *correctness* key: an MCQ stores both
its ``choices`` and the ``correct`` index in one JSONB blob. Because ``ItemRead`` withholds
that whole object, a generated MCQ used to reach the learner with a stem and no options at
all — unanswerable. This module is the single place that decides, per item type, which parts
of an answer key are public.

It is a **whitelist**: anything not explicitly named is withheld, so adding a field to an
answer key can never leak it by omission.
"""

from app.models.assessment import ItemType


def public_presentation(item_type: ItemType, answer_key: dict | None) -> dict | None:
    """Return the learner-visible part of ``answer_key``, or ``None`` if nothing is public.

    MCQ choices are public (you cannot answer without them); the ``correct`` index is not.
    Cloze/fill-blank keys *are* the answers, and a flashcard's ``back`` is its reveal — a
    deliberate step the learner asks for, not something to ship with the question.
    """
    if not answer_key:
        return None
    if item_type is ItemType.MCQ:
        choices = answer_key.get("choices")
        if isinstance(choices, list) and choices:
            return {"choices": [str(choice) for choice in choices]}
    return None
