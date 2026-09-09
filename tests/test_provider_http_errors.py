"""A provider's HTTP failures have to arrive as LLMError, or the chain is blind.

`ProviderChain` falls through on `LLMError` and nothing else. Anything other
exception escapes it, takes the whole request down, and is reported to the
teacher as the generic "unavailable" — with no fallback attempted, however many
providers are configured.

The existing chain tests mock providers that already raise `LLMError`, so they
assert the chain's logic and can never catch a provider that raises the wrong
type. These start one layer lower, at the real provider against a stubbed
transport, which is where that mistake actually lives.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import llm
from app.services.llm import (
    AnthropicProvider,
    LLMError,
    OpenAICompatProvider,
    ProviderChain,
)


class _FakeResponse:
    """Just enough of an httpx response for the provider to read."""

    def __init__(self, status_code: int, lines: list[str] | None = None):
        self.status_code = status_code
        self._lines = lines or []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None
            )

    def iter_lines(self):
        yield from self._lines

    def json(self):
        return {"choices": [{"message": {"content": "hi"}}], "content": [{"text": "hi"}]}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def failing_transport(monkeypatch):
    """Every outbound call returns the given status."""

    def _install(status_code: int):
        monkeypatch.setattr(
            llm.httpx, "stream", lambda *a, **k: _FakeResponse(status_code)
        )
        monkeypatch.setattr(
            llm.httpx, "post", lambda *a, **k: _FakeResponse(status_code)
        )

    return _install


def _openai() -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_key="k",
        model="a-model",
        supports_vision=True,
    )


# --------------------------------------------------------------------------- #
# The text path — a teacher with no lesson open, and every school-admin question
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_streaming_turns_an_http_status_into_an_llm_error(failing_transport, status):
    failing_transport(status)

    with pytest.raises(LLMError):
        list(_openai().chat_stream("sys", [{"role": "user", "content": "hi"}]))


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_non_streaming_turns_an_http_status_into_an_llm_error(failing_transport, status):
    failing_transport(status)

    with pytest.raises(LLMError):
        _openai().chat("sys", [{"role": "user", "content": "hi"}])


def test_anthropic_streaming_does_the_same(failing_transport):
    failing_transport(500)

    with pytest.raises(LLMError):
        list(
            AnthropicProvider(api_key="k", model="m").chat_stream(
                "sys", [{"role": "user", "content": "hi"}]
            )
        )


def test_anthropic_non_streaming_does_the_same(failing_transport):
    failing_transport(500)

    with pytest.raises(LLMError):
        AnthropicProvider(api_key="k", model="m").chat("sys", [])


# --------------------------------------------------------------------------- #
# What it costs when they don't
# --------------------------------------------------------------------------- #
class _Working:
    """A provider that answers, standing in for the configured fallback."""

    name = "groq"
    model = "groq-model"
    supports_vision = False

    def chat(self, system, messages):
        return "answer from groq"

    def chat_stream(self, system, messages):
        yield "answer from groq"


def test_a_failing_primary_actually_falls_through_when_streaming(failing_transport):
    """The whole point of the chain. A real OpenAI 500 has to reach Grok."""
    failing_transport(500)
    chain = ProviderChain([_openai(), _Working()])

    assert "".join(chain.chat_stream("sys", [])) == "answer from groq"
    assert chain.name == "groq"


def test_a_failing_primary_actually_falls_through_when_not_streaming(failing_transport):
    failing_transport(500)
    chain = ProviderChain([_openai(), _Working()])

    assert chain.chat("sys", []) == "answer from groq"


def test_a_transport_failure_falls_through_too(monkeypatch):
    """Not just bad statuses — a dropped connection has to fall through as well."""

    def _boom(*a, **k):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(llm.httpx, "stream", _boom)
    monkeypatch.setattr(llm.httpx, "post", _boom)

    chain = ProviderChain([_openai(), _Working()])
    assert "".join(chain.chat_stream("sys", [])) == "answer from groq"


def test_a_timeout_is_reported_as_a_timeout(monkeypatch):
    """So the teacher is told it took too long, rather than the catch-all."""

    def _slow(*a, **k):
        raise httpx.ReadTimeout("too slow")

    monkeypatch.setattr(llm.httpx, "stream", _slow)

    with pytest.raises(LLMError) as raised:
        list(_openai().chat_stream("sys", []))
    assert raised.value.kind == "timeout"
