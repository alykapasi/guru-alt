"""A stand-in that answers in the shape it was asked for (S58).

``FakeProvider`` answers every request with the same sentence. For a tutoring turn that is
exactly right — a tutor's reply *is* prose — and for everything else the product asks a model
to do it is useless. Curriculum design, item writing, objective selection and grading all
parse the reply as JSON against a documented shape, so against the plain fake every one of
them takes its parse-failure path: the wizard reports "failed to generate curriculum",
practice never produces a question, and no browser journey can get past its first step.

This provider reads the system prompt the caller sent, recognises which of those jobs it is,
and answers in that job's shape — derived from the request wherever that is cheap, so a
journey can assert that what the learner typed reached the model and shaped what came back.
Anything it does not recognise falls through to prose, unchanged from ``FakeProvider``: a
conversational turn is prose, and a JSON caller whose prompt has drifted out of recognition
fails its own parse loudly rather than being served something invented here.

**What keeps it honest.** Every shape is matched by a phrase copied out of the real system
prompt, and ``tests/test_shaped_provider.py`` imports those prompt constants and asserts each
is matched by exactly one shape. Rewording a prompt past its marker fails a unit test in
seconds instead of a browser job in minutes, and a marker broad enough to catch two prompts
fails the same test. First match wins at runtime; that test is what makes the order irrelevant.

**What it is not.** Every reply here is the *well-formed* case. Nothing about a real model's
tendency to omit a field, wrap JSON in apology, or return three choices when asked for four is
represented, so a journey passing against this provider says the product handles a good reply
— never that it handles a bad one. Malformed replies belong to each parser's unit tests and to
``tests/test_fault_injection.py``. The scores below are likewise arbitrary and deterministic:
they exist so a journey can drive both sides of a pass/fail branch, and they model nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from app.llm.providers.fake import FakeProvider, FakeTurn
from app.llm.types import ChatChunk, ChatMessage, ChatResponse, ToolDef, text_of

# A response with at least this many words counts as an attempt and is scored as a pass. Crude
# and deliberately so: this is a switch a journey can throw from the composer, not a grader.
_ATTEMPT_WORDS = 5

_PASSING_SCORE = 0.9
_FAILING_SCORE = 0.2


@dataclass(frozen=True)
class Shape:
    """One job this provider can answer, and how it answers it.

    ``marker`` is a phrase copied verbatim out of the caller's system prompt — distinctive
    enough to identify that one caller, and no more. ``build`` receives the last user message
    and returns the reply text.
    """

    name: str
    marker: str
    build: Callable[[str], str]


def _labelled(prompt: str, label: str) -> str:
    """The rest of the line introduced by ``label``, or "" if it is not there."""
    for line in prompt.splitlines():
        if line.startswith(label):
            return line[len(label) :].strip()
    return ""


def _numbered(prompt: str) -> list[int]:
    """The numbers of a ``1. item`` catalogue — how both numbered prompts list their options."""
    return [int(n) for n in re.findall(r"^\s*(\d+)\.\s", prompt, re.M)]


def _first_words(text: str, count: int) -> str:
    return " ".join(text.split()[:count])


# --- curriculum -------------------------------------------------------------------------------

# A four-component chain rather than four unrelated names, because the prerequisite edges are
# the part of curriculum generation worth driving in a browser (S22): they are persisted at
# commit, and they decide the order the lesson plan comes out in. The third requires the
# second, which lives in the other topic, so the edge crosses a topic boundary as a real one
# would.
_CURRICULUM_KCS = (
    ("Core vocabulary", "The terms the rest of the subject is written in.", ()),
    ("First principles", "The rules everything else follows from.", ("Core vocabulary",)),
    ("Worked examples", "The principles applied to concrete problems.", ("First principles",)),
    ("Common mistakes", "Where those examples usually go wrong.", ("Worked examples",)),
)


def _curriculum(prompt: str) -> str:
    goal = _labelled(prompt, "Goal:") or "An unnamed subject"

    def kc(entry: tuple[str, str, tuple[str, ...]]) -> dict[str, object]:
        name, description, requires = entry
        return {"name": name, "description": description, "requires": list(requires)}

    return json.dumps(
        {
            # Named after the goal so a journey can assert that the sentence the learner typed
            # reached the model and came back as the subject they now own.
            "subject_name": goal[:120],
            "subject_description": f"Everything needed to: {goal}"[:500],
            "topics": [
                {
                    "name": "Foundations",
                    "description": "What has to be in place before anything else.",
                    "kcs": [kc(entry) for entry in _CURRICULUM_KCS[:2]],
                },
                {
                    "name": "Putting it to work",
                    "description": "Applying the foundations, and where that goes wrong.",
                    "kcs": [kc(entry) for entry in _CURRICULUM_KCS[2:]],
                },
            ],
        }
    )


# --- lesson-plan objectives -------------------------------------------------------------------


def _objectives(prompt: str) -> str:
    """Target the *last* candidate, so its prerequisites have to be pulled in behind it.

    Picking the first would select a starting point with nothing before it, and a plan of one
    step never exercises the ordering the graph exists for.
    """
    numbers = _numbered(prompt)
    return json.dumps({"kcs": [max(numbers)] if numbers else []})


# --- item generation --------------------------------------------------------------------------


def _kc_of(prompt: str) -> str:
    return _labelled(prompt, "Knowledge component:") or "this component"


def _mcq(prompt: str) -> str:
    kc = _kc_of(prompt)
    return json.dumps(
        {
            "stem": f"Which of these best describes {kc}?",
            "choices": [
                f"The accepted account of {kc}.",
                f"A common misreading of {kc}.",
                f"A related idea often confused with {kc}.",
                "None of these.",
            ],
            "correct": 0,
        }
    )


def _fill_blank(prompt: str) -> str:
    return json.dumps(
        {"stem": f"The central idea of {_kc_of(prompt)} is ___.", "answer": "the accepted account"}
    )


def _short(prompt: str) -> str:
    kc = _kc_of(prompt)
    return json.dumps(
        {
            "stem": f"In your own words, explain {kc} and give one example.",
            "criteria": [f"States what {kc} is.", "Gives a concrete example."],
        }
    )


def _flashcard(prompt: str) -> str:
    kc = _kc_of(prompt)
    return json.dumps({"stem": f"What is {kc}?", "answer": f"The accepted account of {kc}."})


# --- grading ----------------------------------------------------------------------------------


def _answer_of(prompt: str) -> str:
    """The learner's response, out of the nonce fence the grading prompt puts it in (S31).

    Imported at call time, not at module scope. Providers sit *below* the agent layer, and
    ``app.agent`` imports ``app.llm`` to build its graphs — so a top-level import here closes a
    cycle and the whole package stops loading. Reaching for the real helper late is worth more
    than the alternative of copying the fence format into this file, where it would drift out
    of step with ``as_untrusted`` silently, and the evidence quoted below would stop being the
    learner's words without anything failing.
    """
    from app.agent.untrusted import untrusted_body

    return untrusted_body(prompt).strip()


def _verdict(answer: str) -> tuple[float, dict[str, object], str]:
    """A score, a diagnosis and a rationale for one response. Length is the whole rule.

    The failing kind is ``conceptual`` rather than the ``incomplete`` a short answer would
    honestly earn, and that is a deliberate choice about what this double is for.
    ``turn_common.build_check_result`` drops the diagnosis for ``incomplete`` on purpose —
    "nothing was shown" names no specific failure — so a stand-in that always returned it would
    produce a grade the product renders with no diagnosis at all, and no journey could ever
    reach the S09 path that exists to say *why* an answer failed.
    """
    if len(answer.split()) >= _ATTEMPT_WORDS:
        return _PASSING_SCORE, {"kind": "none"}, "Covers the idea and gives an example."
    return (
        _FAILING_SCORE,
        {
            "kind": "conceptual",
            "confidence": 0.9,
            # The learner's own words, copied exactly, because that is what the prompt demands
            # and what `diagnosis.quotes_verbatim` checks — a paraphrase here would be stored
            # with `evidence_verbatim` false and would be the wrong thing to render.
            "evidence": _first_words(answer, 6),
            "prerequisite": "",
        },
        "The central idea is not there yet.",
    )


def _grade(prompt: str) -> str:
    score, diagnosis, rationale = _verdict(_answer_of(prompt))
    return json.dumps({"score": score, "rationale": rationale, "diagnosis": diagnosis})


def _grade_components(prompt: str) -> str:
    score, diagnosis, rationale = _verdict(_answer_of(prompt))
    listed = _numbered(prompt.split("Knowledge components assessed:", 1)[-1]) or [1]
    return json.dumps(
        {
            "components": [{"n": n, "score": score, "diagnosis": diagnosis} for n in listed],
            "score": score,
            "rationale": rationale,
        }
    )


# Markers are chosen to identify one caller each. The two graders share an opening sentence, so
# neither is matched on it: they are told apart by the clause that differs.
SHAPES: tuple[Shape, ...] = (
    Shape("curriculum", "expert curriculum designer", _curriculum),
    Shape("objectives", "Select only the KCs that are the direct target", _objectives),
    Shape("mcq item", "one short multiple-choice question", _mcq),
    Shape("fill-blank item", "one fill-in-the-blank question", _fill_blank),
    Shape("short item", "one short-answer question", _short),
    Shape("flashcard item", "one flashcard question", _flashcard),
    Shape("per-component grade", "several numbered knowledge components", _grade_components),
    Shape("grade", "against the question and rubric", _grade),
)


class ShapedProvider(FakeProvider):
    """A :class:`FakeProvider` that answers JSON callers in their own shape. See module docs.

    Embeddings, usage accounting and the word-by-word stream are all inherited unchanged: what
    differs is only *what text* comes back, so a journey driving this provider still exercises
    the same streaming path, the same token counting and the same vectors as the plain fake.
    """

    name = "shaped"

    def reply_for(self, system: str | None, messages: Sequence[ChatMessage]) -> str:
        """The reply this provider gives — prose when no shape recognises the system prompt."""
        if not system:
            return self._reply
        prompt = text_of(messages[-1].content) if messages else ""
        for shape in SHAPES:
            if shape.marker in system:
                return shape.build(prompt)
        return self._reply

    def _turn_for(self, system: str | None, messages: Sequence[ChatMessage]) -> FakeTurn:
        return FakeTurn(text=self.reply_for(system, messages))

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> ChatResponse:
        turn = self._turn_for(system, messages)
        return ChatResponse(content=turn.text, usage=self._usage(messages, turn), model=model)

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system: str | None = None,
        max_tokens: int = 1024,
        tools: Sequence[ToolDef] | None = None,
    ) -> AsyncIterator[ChatChunk]:
        turn = self._turn_for(system, messages)
        for i, word in enumerate(turn.text.split()):
            yield ChatChunk(text=word if i == 0 else f" {word}")
        yield ChatChunk(usage=self._usage(messages, turn))
