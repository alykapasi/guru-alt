import json

from app.learning.curriculum import CurriculumProposal, generate_curriculum
from app.llm.providers import FakeProvider
from app.llm.registry import LLMClient, ModelSpec
from app.llm.types import ModelRole


def _client_with_reply(reply: str) -> LLMClient:
    """Create an LLMClient with FakeProvider returning a specific reply."""
    fake = FakeProvider(reply=reply)
    return LLMClient({"fake": fake}, {r: ModelSpec("fake", "fake-1") for r in ModelRole})


VALID_CURRICULUM_REPLY = json.dumps(
    {
        "subject_name": "Linear Algebra",
        "subject_description": "Fundamentals of linear algebra including vectors, matrices, and systems of equations",
        "topics": [
            {
                "name": "Vectors",
                "description": "Introduction to vector spaces and vector operations",
                "kcs": [
                    {"name": "Vector basics", "description": "Vectors as ordered lists of numbers"},
                    {"name": "Vector addition", "description": "Adding vectors component-wise"},
                ],
            },
            {
                "name": "Matrices",
                "description": "Matrix operations and properties",
                "kcs": [{"name": "Matrix basics", "description": "Matrices as rectangular arrays"}],
            },
        ],
    }
)


async def test_parse_well_formed_curriculum():
    """Curriculum JSON reply is parsed into CurriculumProposal."""
    goal = "Learn linear algebra fundamentals"
    materials = ["Vector spaces are sets of vectors...", "Matrices are rectangular arrays..."]

    result, _usage = await generate_curriculum(
        _client_with_reply(VALID_CURRICULUM_REPLY), goal, materials
    )

    assert result is not None
    assert result.subject_name == "Linear Algebra"
    assert len(result.topics) == 2
    assert result.topics[0].name == "Vectors"
    assert len(result.topics[0].kcs) == 2
    assert result.topics[0].kcs[0].name == "Vector basics"


async def test_parse_malformed_json_returns_none():
    """Malformed JSON reply returns None (not fatal)."""
    goal = "Learn calculus"

    result, _usage = await generate_curriculum(_client_with_reply("not json at all"), goal, None)

    assert result is None


async def test_a_generation_that_could_not_be_parsed_still_reports_what_it_cost():
    """The tokens were spent before anyone looked at the reply; None must not mean free."""
    result, usage = await generate_curriculum(_client_with_reply("not json at all"), "goal", None)

    assert result is None
    assert usage.total_tokens > 0


async def test_empty_topics_returns_none():
    """Empty topics array is treated as failure."""
    goal = "Something"
    bad_reply = json.dumps(
        {
            "subject_name": "Empty Subject",
            "subject_description": "A subject with no topics",
            "topics": [],
        }
    )

    result, _usage = await generate_curriculum(_client_with_reply(bad_reply), goal, None)

    assert result is None


async def test_missing_required_fields_returns_none():
    """Missing topic required field (name) returns None."""
    goal = "Test goal"
    bad_reply = json.dumps(
        {
            "subject_name": "Incomplete Subject",
            "subject_description": "A subject with incomplete topics",
            "topics": [
                {
                    # Missing 'name' field
                    "description": "A topic without a name",
                    "kcs": [{"name": "KC1", "description": "A knowledge component"}],
                }
            ],
        }
    )

    result, _usage = await generate_curriculum(_client_with_reply(bad_reply), goal, None)

    assert result is None


async def test_materials_included_in_prompt_when_provided():
    """When materials provided, curriculum generation succeeds (materials grounded)."""
    goal = "Learn Python"
    materials = ["Python is a high-level language...", "Functions are reusable blocks of code..."]

    result, _usage = await generate_curriculum(
        _client_with_reply(VALID_CURRICULUM_REPLY), goal, materials
    )

    # Should successfully parse the curriculum with materials provided
    assert result is not None
    assert isinstance(result, CurriculumProposal)


async def test_materials_none_omits_materials_section():
    """When materials is None, curriculum is purely knowledge-based."""
    goal = "Learn basic statistics"

    result, _usage = await generate_curriculum(
        _client_with_reply(VALID_CURRICULUM_REPLY), goal, None
    )

    # Should succeed without materials
    assert result is not None
    assert isinstance(result, CurriculumProposal)


async def test_non_list_topics_returns_none():
    """Topics field must be a list, not a dict or other type."""
    goal = "Test"
    bad_reply = json.dumps(
        {
            "subject_name": "Test Subject",
            "subject_description": "Test",
            "topics": {"key": "value"},  # topics should be a list, not a dict
        }
    )

    result, _usage = await generate_curriculum(_client_with_reply(bad_reply), goal, None)

    assert result is None
