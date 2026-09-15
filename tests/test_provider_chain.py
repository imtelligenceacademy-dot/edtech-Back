"""The provider fallback chain.

The reason it exists is the rate limit: a low tier runs out partway through a
school day, and a teacher standing in front of a class should get a slightly
different answer rather than an error. What is asserted here is that falling
through never costs the teacher a half-written reply, and never happens for a
failure the next provider would hit identically.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.llm import LLMError, ProviderChain, get_provider, provider_chain_names


class FakeProvider:
    """A provider that fails how the test asks it to."""

    supports_vision = False

    def __init__(
        self,
        name: str,
        *,
        fails: str | None = None,
        mid_stream: bool = False,
        silent: bool = False,
    ):
        self.name = name
        self.model = f"{name}-model"
        self._fails = fails
        self._mid_stream = mid_stream
        self._silent = silent
        self.calls = 0

    def chat_stream_vision(self, system, messages, image_data_url):
        self.calls += 1
        if self._fails:
            raise LLMError(self._fails, f"{self.name} failed")
        if self._silent:
            return
        yield f"saw it, from {self.name}"

    def chat(self, system, messages):
        self.calls += 1
        if self._fails:
            raise LLMError(self._fails, f"{self.name} failed")
        return f"answer from {self.name}"

    def chat_stream(self, system, messages):
        self.calls += 1
        if self._fails and not self._mid_stream:
            raise LLMError(self._fails, f"{self.name} failed")
        if self._silent:
            # A 200 with no deltas in it. Not an error at any layer.
            return
        yield f"hello from {self.name}"
        if self._fails and self._mid_stream:
            raise LLMError(self._fails, f"{self.name} died mid-stream")
        yield " and goodbye"


def test_rate_limited_primary_falls_through_to_the_next():
    primary = FakeProvider("openai", fails="rate_limit")
    backup = FakeProvider("groq")

    chain = ProviderChain([primary, backup])

    assert chain.chat("sys", []) == "answer from groq"
    assert primary.calls == 1, "the primary is still tried first"
    assert chain.name == "groq", "the answer is attributed to who actually gave it"


def test_streaming_falls_through_before_any_text_reaches_the_teacher():
    primary = FakeProvider("openai", fails="rate_limit")
    backup = FakeProvider("groq")

    chunks = list(ProviderChain([primary, backup]).chat_stream("sys", []))

    assert "".join(chunks) == "hello from groq and goodbye"
    assert not any("openai" in c for c in chunks), "no half-answer from the failed one"


def test_a_provider_that_dies_mid_stream_is_not_restarted():
    """Once the teacher is reading words, switching would splice two different
    answers together. The failure surfaces instead."""
    primary = FakeProvider("openai", fails="unavailable", mid_stream=True)
    backup = FakeProvider("groq")

    stream = ProviderChain([primary, backup]).chat_stream("sys", [])
    assert next(stream) == "hello from openai"
    with pytest.raises(LLMError):
        list(stream)
    assert backup.calls == 0, "the backup must not re-answer over a started reply"


def test_a_bad_request_is_not_retried_anywhere_else():
    """A 400 fails identically everywhere; retrying only spends more quota."""
    primary = FakeProvider("openai", fails="bad_request")
    backup = FakeProvider("groq")

    with pytest.raises(LLMError):
        ProviderChain([primary, backup]).chat("sys", [])
    assert backup.calls == 0


def test_a_dead_key_falls_through_rather_than_breaking_the_lesson():
    primary = FakeProvider("openai", fails="auth")
    backup = FakeProvider("groq")

    assert ProviderChain([primary, backup]).chat("sys", []) == "answer from groq"


def test_the_last_failure_surfaces_when_every_provider_is_exhausted():
    chain = ProviderChain(
        [FakeProvider("openai", fails="rate_limit"), FakeProvider("groq", fails="rate_limit")]
    )

    with pytest.raises(LLMError) as exc:
        chain.chat("sys", [])
    assert exc.value.kind == "rate_limit"


def test_vision_is_the_primarys_alone():
    """The image is attached while the prompt is built, before anyone knows a
    fallback will be needed, so only the primary's capability can be promised."""
    seeing = FakeProvider("openai")
    seeing.supports_vision = True
    blind = FakeProvider("groq")

    assert ProviderChain([seeing, blind]).supports_vision is True
    assert ProviderChain([blind, seeing]).supports_vision is False


# --- How the chain is assembled from config --------------------------------- #


def test_the_chain_is_the_primary_then_its_fallbacks(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "ai_fallback_providers", "groq,grok,anthropic")

    assert provider_chain_names() == ["openai", "groq", "grok", "anthropic"]


def test_a_provider_named_twice_is_only_tried_once(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "groq")
    monkeypatch.setattr(settings, "ai_fallback_providers", "groq, openai ,groq")

    assert provider_chain_names() == ["groq", "openai"]


def test_providers_without_a_key_are_left_out_of_the_chain(monkeypatch):
    """An unconfigured fallback is not a fallback; skipping it here beats
    discovering it at request time, mid-lesson."""
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "ai_fallback_providers", "groq,anthropic")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(settings, "groq_api_key", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    provider = get_provider()

    # Only one usable provider, so no chain wrapper is built around it.
    assert provider.name == "openai"


def test_with_no_keys_at_all_the_mock_answers(monkeypatch):
    for key in ("openai_api_key", "groq_api_key", "xai_api_key", "anthropic_api_key"):
        monkeypatch.setattr(settings, key, "")
    monkeypatch.setattr(settings, "ai_provider", "openai")

    assert get_provider().name == "mock"


def test_a_reply_with_no_text_moves_to_the_next_provider(monkeypatch):
    """A turn can carry no text at all — a content filter, a refusal, a tool
    call — and `content` comes back null.

    Calling .strip() on it raised AttributeError, which is not the LLMError the
    chain catches, so the whole request died with healthy providers untried:
    the exact failure this layer exists to prevent, arriving through the one
    door it did not watch.
    """
    import httpx
    from app.services.llm import OpenAICompatProvider

    provider = OpenAICompatProvider(
        name="primary", api_key="k", base_url="https://example.invalid", model="m",
    )

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": None, "refusal": "no"}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(LLMError) as excinfo:
        provider.chat("sys", [{"role": "user", "content": "hi"}])
    assert excinfo.value.kind == "unavailable"

    # And in a chain, that is something the next provider gets to answer.
    chain = ProviderChain([provider, FakeProvider("groq")])
    assert chain.chat("sys", [{"role": "user", "content": "hi"}]) == "answer from groq"


def test_an_empty_reply_is_a_failure_and_the_chain_moves_on():
    """A 200 carrying no content is not an answer.

    A content filter trips, or a refusal arrives as an empty body, and the
    provider returns success with nothing in it. Credited as a reply, that
    committed the chain: the teacher read a blank message while three healthy
    providers sat untried, and because the usage was refunded the only symptom
    was the assistant saying nothing at all.
    """
    silent = FakeProvider("openai", silent=True)
    backup = FakeProvider("groq")

    chunks = list(ProviderChain([silent, backup]).chat_stream("sys", []))

    assert "".join(chunks) == "hello from groq and goodbye"
    assert backup.calls == 1, "the backup has to be tried"


def test_an_empty_reply_leaves_the_answer_attributed_to_whoever_gave_it():
    silent = FakeProvider("openai", silent=True)
    backup = FakeProvider("groq")

    chain = ProviderChain([silent, backup])
    list(chain.chat_stream("sys", []))

    assert chain.name == "groq", "the silent one did not answer and must not be credited"


def test_an_empty_vision_reply_raises_so_the_caller_can_drop_the_image():
    """The vision path has one provider and no fallback inside the chain.

    Its caller retries through the text path, which loses the picture and keeps
    the answer — but only on an error. Returning empty was indistinguishable
    from having answered, so nothing retried.
    """
    silent = FakeProvider("openai", silent=True)

    with pytest.raises(LLMError):
        list(ProviderChain([silent]).chat_stream_vision("sys", [], "data:image/png;base64,AA"))
