"""Vision-capable LLM layer: content-parts translation + the VISION role.

These exercise the provider *translators* directly (no network): each turns a multimodal
ChatMessage into its backend's native image shape, while a plain-string message stays a
plain string (the text-path regression).
"""

import base64

from app.core.config import Settings
from app.llm import (
    ChatMessage,
    ChatRole,
    ImagePart,
    ModelRole,
    TextPart,
    build_llm_client,
    text_of,
)
from app.llm.providers.anthropic import AnthropicProvider
from app.llm.providers.fake import FakeProvider
from app.llm.providers.openai_compat import OpenAICompatProvider
from app.llm.registry import fake_llm_client

_IMG = b"\x89PNG\r\n\x1a\n fake bytes"


def _image_message() -> ChatMessage:
    return ChatMessage(
        role=ChatRole.USER,
        content=[
            TextPart(text="Transcribe this."),
            ImagePart(media_type="image/png", data=_IMG),
        ],
    )


# --- text_of ----------------------------------------------------------------


def test_text_of_str_and_parts() -> None:
    assert text_of("plain") == "plain"
    parts = [TextPart(text="a"), ImagePart(media_type="image/png", data=_IMG), TextPart(text="b")]
    assert text_of(parts) == "a b"  # images are skipped


# --- OpenAI-compatible translation ------------------------------------------


def test_openai_payload_translates_image() -> None:
    payload = OpenAICompatProvider._payload([_image_message()], system=None)
    parts = payload[0]["content"]
    assert parts[0] == {"type": "text", "text": "Transcribe this."}
    expected_url = f"data:image/png;base64,{base64.b64encode(_IMG).decode()}"
    assert parts[1] == {"type": "image_url", "image_url": {"url": expected_url}}


def test_openai_payload_keeps_string_content() -> None:
    payload = OpenAICompatProvider._payload(
        [ChatMessage(role=ChatRole.USER, content="hello")], system="sys"
    )
    assert payload == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},  # str stays str, not a parts list
    ]


# --- Anthropic translation --------------------------------------------------


def test_anthropic_split_translates_image() -> None:
    _system, convo = AnthropicProvider._split([_image_message()], system=None)
    blocks = convo[0]["content"]
    assert blocks[0] == {"type": "text", "text": "Transcribe this."}
    assert blocks[1] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(_IMG).decode(),
        },
    }


def test_anthropic_split_keeps_string_and_hoists_system() -> None:
    msgs = [
        ChatMessage(role=ChatRole.SYSTEM, content="be brief"),
        ChatMessage(role=ChatRole.USER, content="hi"),
    ]
    system, convo = AnthropicProvider._split(msgs, system="top")
    assert system == "top\n\nbe brief"
    assert convo == [{"role": "user", "content": "hi"}]  # str stays str


# --- role wiring + fake provider --------------------------------------------


def test_registry_includes_vision_role() -> None:
    client = build_llm_client(Settings())
    assert client.spec(ModelRole.VISION).model == "llama3.2-vision"
    assert fake_llm_client().spec(ModelRole.VISION).provider == "fake"


async def test_fake_provider_accepts_image_message() -> None:
    resp = await FakeProvider(reply="ocr text").complete(
        model="fake-1", messages=[_image_message()]
    )
    assert resp.content == "ocr text"
    assert resp.usage.input_tokens == 2  # "Transcribe this." -> 2 words; image skipped
