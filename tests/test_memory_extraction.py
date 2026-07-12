"""Memory extraction: the pure ``extract_memories`` classifier, exercised offline.

The FAST model reads a conversation transcript and extracts durable facts/preferences/summaries
about the learner. A ``FakeProvider`` returns a canned JSON reply so mapping, capping, and
robustness against bad output are all deterministic.
"""

import json

from app.llm import Usage
from app.llm.registry import fake_llm_client
from app.llm.types import ChatMessage, ChatRole
from app.memory.extraction import ExtractedMemory, extract_memories
from app.models.memory import MemoryKind


def _messages() -> list[ChatMessage]:
    return [
        ChatMessage(role=ChatRole.USER, content="I'm studying for the MCAT, mornings only."),
        ChatMessage(role=ChatRole.ASSISTANT, content="Got it, let's focus on organic chemistry."),
    ]


async def test_extract_memories_parses_mixed_kinds() -> None:
    reply = json.dumps(
        {
            "memories": [
                {"kind": "fact", "content": "Studying for the MCAT."},
                {"kind": "preference", "content": "Prefers morning study sessions."},
                {"kind": "summary", "content": "Covered organic chemistry basics."},
            ]
        }
    )

    extracted, usage = await extract_memories(fake_llm_client(reply), _messages())

    assert extracted == [
        ExtractedMemory(kind=MemoryKind.FACT, content="Studying for the MCAT."),
        ExtractedMemory(kind=MemoryKind.PREFERENCE, content="Prefers morning study sessions."),
        ExtractedMemory(kind=MemoryKind.SUMMARY, content="Covered organic chemistry basics."),
    ]
    assert usage.input_tokens > 0


async def test_extract_memories_no_messages_makes_no_model_call() -> None:
    # A sentinel reply that would blow up if parsed proves the model is never consulted.
    extracted, usage = await extract_memories(fake_llm_client("BOOM not json"), [])
    assert extracted == []
    assert usage == Usage()


async def test_extract_memories_skips_unparseable_reply() -> None:
    extracted, usage = await extract_memories(fake_llm_client("not json at all"), _messages())
    assert extracted == []
    assert usage.output_tokens > 0  # the call still happened and cost tokens


async def test_extract_memories_drops_unknown_kind_keeps_the_rest() -> None:
    reply = json.dumps(
        {
            "memories": [
                {"kind": "bogus", "content": "should be dropped"},
                {"kind": "fact", "content": "Studying for the MCAT."},
            ]
        }
    )

    extracted, _usage = await extract_memories(fake_llm_client(reply), _messages())

    assert extracted == [ExtractedMemory(kind=MemoryKind.FACT, content="Studying for the MCAT.")]


async def test_extract_memories_drops_empty_content() -> None:
    reply = json.dumps({"memories": [{"kind": "fact", "content": "   "}]})
    extracted, _usage = await extract_memories(fake_llm_client(reply), _messages())
    assert extracted == []


async def test_extract_memories_caps_output_at_the_limit() -> None:
    reply = json.dumps({"memories": [{"kind": "fact", "content": f"fact {i}"} for i in range(10)]})

    extracted, _usage = await extract_memories(fake_llm_client(reply), _messages())

    assert len(extracted) == 5
    assert [e.content for e in extracted] == [f"fact {i}" for i in range(5)]
