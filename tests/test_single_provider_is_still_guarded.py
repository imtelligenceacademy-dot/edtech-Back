"""One configured key is the ordinary production shape, not a special case.

`get_provider()` returned a bare provider whenever exactly one had a key, and
both streaming empty-reply guards live on ProviderChain — so the deployments
with no fallback to soften a silent failure were also the ones with no guard
against it. A 200 carrying no chunks became an empty answer bubble with no error
frame and no retry, and on the vision path the slide transcription a text-only
call would have answered from was discarded with it.

The existing vision-fallback tests build ProviderChain([...]) by hand, which is
why this went unseen: they assert the chain's behaviour, and the chain was the
one thing a single-key deployment never got.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.config import settings
from app.services import llm as llm_module
from app.services.llm import ChatMessage, LLMError, ProviderChain, get_provider


class _Silent:
    """A provider that answers 200 and says nothing — a content filter, a
    refusal, or a failure that arrived as a non-error type."""

    name = "silent"
    model = "silent-1"
    supports_vision = True

    def chat(self, system: str, messages: list[ChatMessage]) -> str:
        return ""

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]:
        return iter(())

    def chat_stream_vision(
        self, system: str, messages: list[ChatMessage], image: str
    ) -> Iterator[str]:
        return iter(())


@pytest.fixture()
def only_one_key(monkeypatch):
    """Exactly one usable provider, as a single-key deployment has."""
    monkeypatch.setattr(llm_module, "_build", lambda name: _Silent() if name == "openai" else None)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "ai_fallback_providers", "groq,grok,anthropic")


def test_one_configured_provider_still_comes_back_as_a_chain(only_one_key):
    provider = get_provider()

    assert isinstance(provider, ProviderChain)
    # And it still looks like the provider it wraps, or the answer would be
    # attributed to the wrong thing on screen.
    assert provider.name == "silent"
    assert provider.model == "silent-1"
    assert provider.supports_vision is True


def test_an_empty_stream_from_the_only_provider_is_an_error_not_a_blank_answer(only_one_key):
    provider = get_provider()

    with pytest.raises(LLMError) as err:
        list(provider.chat_stream("system", [ChatMessage(role="user", content="hi")]))

    assert err.value.kind == "unavailable"


def test_an_empty_vision_stream_from_the_only_provider_is_an_error_too(only_one_key):
    """The path where it costs most: the teacher asked about the slide."""
    provider = get_provider()

    with pytest.raises(LLMError) as err:
        list(
            provider.chat_stream_vision(
                "system", [ChatMessage(role="user", content="what is on this slide?")], "data:image/png;base64,AAAA"
            )
        )

    assert err.value.kind == "unavailable"


def test_with_no_keys_at_all_the_mock_still_answers(monkeypatch):
    """Local development with no keys has to keep working."""
    monkeypatch.setattr(llm_module, "_build", lambda name: None)

    provider = get_provider()

    assert provider.name == "mock"
    answer = provider.chat("system", [ChatMessage(role="user", content="hi")])
    assert answer
