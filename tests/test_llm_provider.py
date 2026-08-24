"""Provider layer: multimodal payload shape and error mapping.

These inspect the generated request without making any paid API call.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.llm import (
    LLMError,
    MockProvider,
    OpenAICompatProvider,
    _raise_for_status,
    get_provider,
)


def _openai() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key="sk-not-a-real-key",
        model="test-model",
        supports_vision=True,
    )


MESSAGES = [
    {"role": "user", "content": "first question"},
    {"role": "assistant", "content": "first answer"},
    {"role": "user", "content": "which pin is the servo on?"},
]
IMAGE = "data:image/jpeg;base64,AAAA"


def test_payload_targets_the_configured_model_and_streams():
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    assert payload["model"] == "test-model"
    assert payload["stream"] is True


def test_payload_puts_the_policy_in_a_system_turn():
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    first = payload["input"][0]
    assert first["role"] == "system"
    assert first["content"][0]["text"] == "POLICY"


def test_image_is_attached_only_to_the_latest_user_turn():
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    last = payload["input"][-1]
    assert last["role"] == "user"
    types = [part["type"] for part in last["content"]]
    assert types == ["input_text", "input_image"]
    assert last["content"][1]["image_url"] == IMAGE

    # No earlier turn carries an image - we never resend the whole deck.
    for turn in payload["input"][:-1]:
        assert all(part["type"] != "input_image" for part in turn["content"])


def test_only_one_image_is_ever_sent():
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    images = [
        part
        for turn in payload["input"]
        for part in turn["content"]
        if part["type"] == "input_image"
    ]
    assert len(images) == 1


def test_assistant_history_uses_output_text_parts():
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    assistant = [t for t in payload["input"] if t["role"] == "assistant"][0]
    assert assistant["content"][0]["type"] == "output_text"


def test_image_detail_comes_from_settings(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ai_image_detail", "low")
    payload = _openai()._vision_payload("POLICY", MESSAGES, IMAGE)
    assert payload["input"][-1]["content"][1]["detail"] == "low"


@pytest.mark.parametrize(
    "status_code,kind",
    [(401, "auth"), (403, "auth"), (429, "rate_limit"), (500, "unavailable"), (503, "unavailable")],
)
def test_provider_status_codes_map_to_error_kinds(status_code, kind):
    with pytest.raises(LLMError) as exc:
        _raise_for_status(status_code)
    assert exc.value.kind == kind


def test_success_status_does_not_raise():
    _raise_for_status(200)


def test_errors_never_leak_the_api_key():
    with pytest.raises(LLMError) as exc:
        _raise_for_status(401)
    assert "sk-" not in str(exc.value)


def test_mock_and_text_providers_do_not_claim_vision():
    assert MockProvider().supports_vision is False


def test_default_provider_is_the_mock_in_tests():
    """conftest forces AI_PROVIDER=mock so tests never hit a paid API."""
    assert get_provider().name == "mock"
