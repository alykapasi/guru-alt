"""The stand-in that answers in the shape it was asked for (S58).

The tests that carry the weight here are the first two. Everything this provider does rests on
recognising a caller by a phrase copied out of its system prompt, and that is a coupling
nothing else in the codebase would notice breaking: reword ``_SHORT_SYSTEM_PROMPT`` and the
marker stops matching, the provider falls through to prose, item generation fails its parse,
and the only signal is a browser job going red twenty minutes later with a timeout on a
question that never appeared. So the real prompts are imported and matched here, in a unit
test that runs in milliseconds.

The rest pin the *shapes*: each reply is fed through the parser that will actually receive it
in production, because a reply this file asserts on and a reply the product can use are two
different claims, and only the second one is worth anything.
"""

import json

import pytest

from app.learning.conversation_evidence import _SYSTEM_PROMPT as _INTENT_SYSTEM_PROMPT
from app.learning.conversation_evidence import TurnIntent, parse_intent
from app.learning.curriculum import CURRICULUM_SYSTEM_PROMPT, generate_curriculum
from app.learning.item_generation import (
    _FILL_BLANK_SYSTEM_PROMPT,
    _FLASHCARD_SYSTEM_PROMPT,
    _SHORT_SYSTEM_PROMPT,
)
from app.learning.item_generation import _SYSTEM_PROMPT as _MCQ_SYSTEM_PROMPT
from app.learning.lesson_plan import _OBJECTIVE_SYSTEM_PROMPT
from app.learning.rubric_grading import _COMPONENT_SYSTEM_PROMPT
from app.learning.rubric_grading import _SYSTEM_PROMPT as _GRADE_SYSTEM_PROMPT
from app.llm.providers.shaped import SHAPES, ShapedProvider, _verdict
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ChatMessage, ChatRole, ModelRole

# Every system prompt this provider claims to recognise, beside the shape that must claim it.
REAL_PROMPTS = [
    ("curriculum", CURRICULUM_SYSTEM_PROMPT),
    ("objectives", _OBJECTIVE_SYSTEM_PROMPT),
    ("mcq item", _MCQ_SYSTEM_PROMPT),
    ("fill-blank item", _FILL_BLANK_SYSTEM_PROMPT),
    ("short item", _SHORT_SYSTEM_PROMPT),
    ("flashcard item", _FLASHCARD_SYSTEM_PROMPT),
    ("grade", _GRADE_SYSTEM_PROMPT),
    ("per-component grade", _COMPONENT_SYSTEM_PROMPT),
    ("intent", _INTENT_SYSTEM_PROMPT),
]


def _client() -> LLMClient:
    provider = ShapedProvider()
    return LLMClient(
        {"shaped": provider},
        {role: ModelSpec(provider="shaped", model="shaped-1") for role in ModelRole},
    )


async def _reply(system: str, prompt: str) -> str:
    completion = await _client().complete(
        ModelRole.SMART, [ChatMessage(role=ChatRole.USER, content=prompt)], system=system
    )
    return completion.content


# --- the coupling, checked against the real prompts -------------------------------------------


@pytest.mark.parametrize(("expected", "prompt"), REAL_PROMPTS, ids=[n for n, _ in REAL_PROMPTS])
def test_each_real_system_prompt_is_claimed_by_exactly_one_shape(
    expected: str, prompt: str
) -> None:
    """Exactly one, in both directions.

    Zero means a prompt was reworded past its marker and that caller now silently gets prose.
    Two means a marker is broad enough to answer somebody else's request in the wrong shape —
    the two graders share an opening sentence, so that is a live risk rather than a theoretical
    one, and which of the pair wins would come down to the order of a tuple.
    """
    matched = [shape.name for shape in SHAPES if shape.marker in prompt]

    assert matched == [expected]


def _every_system_prompt_in_the_app() -> dict[str, str]:
    """Every module-level ``*SYSTEM_PROMPT`` string in ``app``, by qualified name.

    Swept rather than listed: a hand-written list is exactly the thing that goes stale, and the
    risk this guards against arrives with a prompt nobody thought to add to it.
    """
    import importlib
    import pkgutil

    import app

    found: dict[str, str] = {}
    for info in pkgutil.walk_packages(app.__path__, prefix="app."):
        module = importlib.import_module(info.name)
        for attribute in dir(module):
            if attribute.endswith("SYSTEM_PROMPT"):
                value = getattr(module, attribute)
                if isinstance(value, str):
                    found[f"{info.name}.{attribute}"] = value
    return found


def test_no_conversational_prompt_is_answered_with_json() -> None:
    """The dangerous direction, and the one the other tests cannot see.

    A marker broad enough to also match the tutor's, the refinement gate's or the workflow's
    system prompt would replace a teaching reply with a JSON blob — and the browser journeys
    would go on passing, because they assert on the *default* prose, which is what those
    callers get today. This sweeps every system prompt in the codebase, including the ones
    written after this file, and requires that only the eight known callers match anything.
    """
    known = {prompt for _, prompt in REAL_PROMPTS}
    wrongly_claimed = {
        name: [shape.name for shape in SHAPES if shape.marker in prompt]
        for name, prompt in _every_system_prompt_in_the_app().items()
        if prompt not in known and any(shape.marker in prompt for shape in SHAPES)
    }

    assert wrongly_claimed == {}


def test_the_sweep_can_actually_see_the_prompts_it_claims_to_check() -> None:
    """A sweep that silently found nothing would make the test above pass forever."""
    swept = set(_every_system_prompt_in_the_app().values())

    assert {prompt for _, prompt in REAL_PROMPTS} <= swept
    assert len(swept) > len(REAL_PROMPTS)


def test_no_shape_is_answering_a_prompt_that_no_longer_exists() -> None:
    """A marker matching nothing is dead code that looks like coverage: the shape is listed, a
    reader assumes that caller is served, and the caller is getting prose."""
    unmatched = [
        shape.name
        for shape in SHAPES
        if not any(shape.marker in prompt for _, prompt in REAL_PROMPTS)
    ]

    assert unmatched == []


async def test_an_unrecognised_prompt_falls_through_to_prose() -> None:
    """The tutoring case, and the correct one: a tutor's reply is prose. This is also what a
    caller whose prompt has drifted gets — its own parser fails, rather than this file
    inventing a plausible-looking answer for a request it did not understand."""
    assert await _reply("You are Guru, a patient tutor.", "Explain derivatives.") == (
        "Hello from the fake tutor."
    )


async def test_a_call_with_no_system_prompt_is_prose_too() -> None:
    assert await _reply("", "hello") == "Hello from the fake tutor."


# --- the shapes, checked through the parsers that receive them --------------------------------


async def test_the_curriculum_is_named_after_the_goal_and_carries_prerequisites() -> None:
    """Through ``generate_curriculum`` rather than ``json.loads``: the journey's claim is that
    the wizard produces a usable curriculum, and only the real parser can support it."""
    proposal, usage = await generate_curriculum(_client(), "Master linear algebra", None)

    assert proposal is not None
    assert proposal.subject_name == "Master linear algebra"
    kcs = [kc for topic in proposal.topics for kc in topic.kcs]
    assert [kc.name for kc in kcs] == [
        "Core vocabulary",
        "First principles",
        "Worked examples",
        "Common mistakes",
    ]
    # Resolved to keys by the parser, which is the half that matters: a prerequisite naming a
    # KC the parser did not keep would be dropped silently and the plan would come out flat.
    assert [len(kc.requires) for kc in kcs] == [0, 1, 1, 1]
    assert usage.output_tokens > 0


async def test_a_prerequisite_crosses_the_topic_boundary() -> None:
    """The third KC requires the second, which is in the other topic. A chain that never left
    its own topic would let a per-topic resolution bug through unnoticed."""
    proposal, _ = await generate_curriculum(_client(), "Master linear algebra", None)

    assert proposal is not None
    first_topic_keys = {kc.key for kc in proposal.topics[0].kcs}
    crossing = proposal.topics[1].kcs[0]
    assert set(crossing.requires) <= first_topic_keys
    assert crossing.requires


async def test_objectives_target_the_last_candidate_so_prerequisites_are_pulled_in() -> None:
    reply = await _reply(
        _OBJECTIVE_SYSTEM_PROMPT,
        "Candidate KCs:\n1. Core vocabulary\n2. First principles\n3. Worked examples\n\n"
        "Learner's goal:\nGet to worked examples",
    )

    assert json.loads(reply) == {"kcs": [3]}


async def test_objectives_with_no_catalogue_select_nothing_rather_than_guessing() -> None:
    assert json.loads(await _reply(_OBJECTIVE_SYSTEM_PROMPT, "Learner's goal:\nanything")) == {
        "kcs": []
    }


@pytest.mark.parametrize(
    ("prompt", "required"),
    [
        (_MCQ_SYSTEM_PROMPT, ("stem", "choices", "correct")),
        (_FILL_BLANK_SYSTEM_PROMPT, ("stem", "answer")),
        (_SHORT_SYSTEM_PROMPT, ("stem", "criteria")),
        (_FLASHCARD_SYSTEM_PROMPT, ("stem", "answer")),
    ],
)
async def test_each_item_shape_carries_the_fields_its_parser_requires(
    prompt: str, required: tuple[str, ...]
) -> None:
    data = json.loads(await _reply(prompt, "Knowledge component: Eigenvalues\nWhat they are."))

    assert set(required) <= set(data)
    assert "Eigenvalues" in data["stem"], "the question has to be about what was asked"


async def test_the_multiple_choice_answer_key_is_in_range() -> None:
    """``_parse_mcq`` rejects an index outside the choices, and a rejected item is no item —
    the practice page would sit on a spinner with nothing to show."""
    data = json.loads(await _reply(_MCQ_SYSTEM_PROMPT, "Knowledge component: Eigenvalues"))

    assert len(data["choices"]) == 4
    assert 0 <= data["correct"] < len(data["choices"])


async def test_the_fill_in_the_blank_stem_has_exactly_one_blank() -> None:
    """The prompt demands ``___`` exactly once and the parser enforces it."""
    data = json.loads(await _reply(_FILL_BLANK_SYSTEM_PROMPT, "Knowledge component: Eigenvalues"))

    assert data["stem"].count("___") == 1


# --- the intent gate --------------------------------------------------------------------------


@pytest.mark.parametrize("message", ["no", "An eigenvalue is the scaling factor."])
async def test_every_reply_to_an_open_question_is_an_attempt(message: str) -> None:
    """Short or long, so a journey's brief answer reaches the grader and fails there, rather
    than being read as a deferral and pausing practice before anything is graded (S52)."""
    reply = await _reply(
        _INTENT_SYSTEM_PROMPT,
        f"Question the tutor asked:\nWhat is an eigenvalue?\n\nLearner's reply:\n{message}",
    )

    assert parse_intent(reply) is TurnIntent.ATTEMPT


# --- grading ----------------------------------------------------------------------------------


def _grading_prompt(answer: str, *, components: str = "") -> str:
    """The shape ``rubric_grading._build_prompt`` produces, fence and all."""
    from app.agent.untrusted import as_untrusted

    parts = ["Question:\nWhat is an eigenvalue?", 'Rubric criteria (JSON):\n{"criteria": []}']
    if components:
        parts.append(f"Knowledge components assessed:\n{components}")
    parts.append(f"Learner's response:\n{as_untrusted('LEARNER RESPONSE', answer)}")
    return "\n\n".join(parts)


async def test_an_answer_that_looks_like_an_attempt_passes() -> None:
    data = json.loads(
        await _reply(
            _GRADE_SYSTEM_PROMPT,
            _grading_prompt("An eigenvalue is the scaling factor of an eigenvector."),
        )
    )

    assert data["score"] >= 0.6
    assert data["diagnosis"]["kind"] == "none"


async def test_a_one_word_answer_fails_and_the_evidence_is_the_learner_s_own_words() -> None:
    """``diagnosis.quotes_verbatim`` checks the quote against the response, and a paraphrase
    would be stored with ``evidence_verbatim`` false. A stand-in that invented its evidence
    would let a journey assert on a rendered quote that the real path would mark unverified."""
    from app.learning.diagnosis import quotes_verbatim

    answer = "dunno"
    data = json.loads(await _reply(_GRADE_SYSTEM_PROMPT, _grading_prompt(answer)))

    assert data["score"] < 0.6
    assert quotes_verbatim(data["diagnosis"]["evidence"], answer)


def test_the_failing_diagnosis_is_one_the_product_actually_renders() -> None:
    """Found by driving the practice loop rather than by reading the code: the first version
    returned ``incomplete``, the grade arrived with ``failure_kind: null``, and the journey had
    nothing to assert. ``build_check_result`` drops the diagnosis for the two kinds that name
    no specific failure, so a stand-in returning one of those exercises the S09 path in name
    only."""
    from app.learning.diagnosis import ACTIONABLE, FailureKind

    _, diagnosis, _ = _verdict("no")

    assert FailureKind(diagnosis["kind"]) in ACTIONABLE


async def test_the_answer_is_read_from_inside_the_fence_not_around_it() -> None:
    """The grading prompt wraps the learner's words in a nonce fence (S31). Quoting the
    instruction text that precedes it would produce evidence the learner never wrote."""
    data = json.loads(await _reply(_GRADE_SYSTEM_PROMPT, _grading_prompt("no")))

    assert data["diagnosis"]["evidence"] == "no"


async def test_every_listed_component_is_graded_exactly_once() -> None:
    """The prompt requires it, and ``_parse_components`` keys by number — a missing component
    silently falls back to the aggregate score, which is the thing S10 exists to stop."""
    data = json.loads(
        await _reply(
            _COMPONENT_SYSTEM_PROMPT,
            _grading_prompt(
                "A long enough answer to count as an attempt here.",
                components="1. Definition\n2. Example",
            ),
        )
    )

    assert [c["n"] for c in data["components"]] == [1, 2]
    assert "score" in data and "rationale" in data


# --- the stream ---------------------------------------------------------------------------------


async def test_the_shaped_reply_is_streamed_in_pieces_and_reassembles() -> None:
    """Shaping changes *what* comes back, not how. A JSON body delivered whole in one chunk
    would mean the streaming path is not the one the journeys drive."""
    chunks = [
        chunk
        async for chunk in _client().stream(
            ModelRole.SMART,
            [ChatMessage(role=ChatRole.USER, content="Knowledge component: Eigenvalues")],
            system=_SHORT_SYSTEM_PROMPT,
        )
    ]

    text = "".join(chunk.text for chunk in chunks if chunk.text)
    assert len(chunks) > 2
    assert json.loads(text)["stem"]
    assert chunks[-1].usage is not None


async def test_embeddings_are_the_plain_fake_s_so_a_journey_indexes_real_vectors() -> None:
    result = await _client().embed(ModelRole.EMBED, ["one", "two"])

    assert len(result.vectors) == 2
    assert result.vectors[0] != result.vectors[1]
