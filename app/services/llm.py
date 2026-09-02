"""Provider-agnostic LLM layer for the teacher assistant.

Every provider implements the same `chat(system, messages) -> str` interface,
so switching between Grok, GPT-4o, Claude, or the no-key mock is a one-line
config change (``AI_PROVIDER`` in the environment). API keys live only here on
the server — never in the frontend.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Protocol, TypedDict

import httpx

from app.config import settings

logger = logging.getLogger("app.llm")


class ChatMessage(TypedDict):
    role: str  # "user" | "assistant"
    content: str


class LLMError(RuntimeError):
    """A provider call failed. `kind` lets callers give the user an accurate
    message without leaking provider internals or credentials."""

    def __init__(self, kind: str, message: str = "") -> None:
        super().__init__(message or kind)
        self.kind = kind  # auth | rate_limit | quota | timeout | unavailable


def _raise_for_status(status_code: int) -> None:
    """Map a provider HTTP status onto our own error kinds."""
    if status_code in (401, 403):
        raise LLMError("auth", "provider rejected the API key")
    if status_code == 429:
        raise LLMError("rate_limit", "provider rate limit or quota reached")
    if status_code >= 500:
        raise LLMError("unavailable", f"provider returned {status_code}")
    if status_code >= 400:
        raise LLMError("unavailable", f"provider returned {status_code}")


class LLMProvider(Protocol):
    name: str
    model: str | None
    # True only when the provider can accept an image alongside the question.
    supports_vision: bool

    def chat(self, system: str, messages: list[ChatMessage]) -> str: ...

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]: ...


# --------------------------------------------------------------------------- #
# Mock — works with no API key, for local development.
# --------------------------------------------------------------------------- #
class MockProvider:
    name = "mock"
    model = None
    supports_vision = False

    def chat(self, system: str, messages: list[ChatMessage]) -> str:
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        grounded = "based on this lesson's material, " if "LESSON MATERIAL" in system else ""
        return (
            f"(mock assistant) Here's how I'd approach \"{last.strip()[:120]}\" {grounded}"
            "— break it into a short definition, one real-world example, and a 5-minute "
            "classroom activity. Add a real provider API key in backend/.env for live answers."
        )

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]:
        for word in self.chat(system, messages).split(" "):
            yield word + " "


# --------------------------------------------------------------------------- #
# OpenAI-compatible Chat Completions (covers both xAI Grok and OpenAI GPT-4o).
# --------------------------------------------------------------------------- #
class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        supports_vision: bool = False,
    ):
        self.name = name
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        # Only providers whose Responses API we speak (OpenAI) get the image path.
        self.supports_vision = supports_vision

    # ---- Vision (OpenAI Responses API) --------------------------------- #
    def _vision_payload(
        self, system: str, messages: list[ChatMessage], image_data_url: str
    ) -> dict:
        """Build a Responses-API payload with the slide image attached to the
        latest user turn. Kept separate so it can be asserted in tests without
        making a paid API call."""
        history = messages[:-1] if messages else []
        last_user = messages[-1]["content"] if messages else ""
        return {
            "model": self.model,
            "stream": True,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system}],
                },
                *[
                    {
                        "role": m["role"],
                        "content": [
                            {
                                "type": (
                                    "output_text"
                                    if m["role"] == "assistant"
                                    else "input_text"
                                ),
                                "text": m["content"],
                            }
                        ],
                    }
                    for m in history
                ],
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": last_user},
                        {
                            "type": "input_image",
                            "image_url": image_data_url,
                            "detail": settings.ai_image_detail,
                        },
                    ],
                },
            ],
        }

    def chat_stream_vision(
        self, system: str, messages: list[ChatMessage], image_data_url: str
    ) -> Iterator[str]:
        """Stream an answer that also considers the rendered slide image."""
        payload = self._vision_payload(system, messages, image_data_url)
        try:
            with httpx.stream(
                "POST",
                f"{self._base_url}/responses",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=settings.ai_timeout_seconds,
            ) as resp:
                _raise_for_status(resp.status_code)
                for line in resp.iter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    kind = obj.get("type")
                    if kind == "response.output_text.delta":
                        delta = obj.get("delta")
                        if delta:
                            yield delta
                    elif kind == "error" or kind == "response.failed":
                        raise LLMError("unavailable", "provider reported a failure")
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMError("timeout", "provider timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError("unavailable", "provider connection failed") from exc

    def _payload(self, system: str, messages: list[ChatMessage], *, stream: bool) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0.4,
            "stream": stream,
        }

    def chat(self, system: str, messages: list[ChatMessage]) -> str:
        resp = httpx.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=self._payload(system, messages, stream=False),
            timeout=settings.ai_timeout_seconds,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]:
        with httpx.stream(
            "POST",
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=self._payload(system, messages, stream=True),
            timeout=settings.ai_timeout_seconds,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content")
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                if delta:
                    yield delta


# --------------------------------------------------------------------------- #
# Anthropic Messages API.
# --------------------------------------------------------------------------- #
class AnthropicProvider:
    name = "anthropic"
    supports_vision = False

    def __init__(self, *, api_key: str, model: str):
        self.model = model
        self._api_key = api_key

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def chat(self, system: str, messages: list[ChatMessage]) -> str:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers=self._headers(),
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": system,
                "messages": messages,
            },
            timeout=settings.ai_timeout_seconds,
        )
        resp.raise_for_status()
        return "".join(block.get("text", "") for block in resp.json()["content"]).strip()

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]:
        with httpx.stream(
            "POST",
            "https://api.anthropic.com/v1/messages",
            headers=self._headers(),
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": system,
                "messages": messages,
                "stream": True,
            },
            timeout=settings.ai_timeout_seconds,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:].strip())
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "content_block_delta":
                    text = obj.get("delta", {}).get("text")
                    if text:
                        yield text


# Failures the next provider might survive. A 400 is excluded deliberately: a
# malformed request fails identically everywhere, so retrying it just spends
# another provider's quota to get the same answer. `auth` is included because a
# dead or revoked key must not take the assistant down mid-lesson — it is logged
# loudly instead, since it is a misconfiguration rather than weather.
RETRYABLE_KINDS = {"rate_limit", "quota", "timeout", "unavailable", "auth"}


class ProviderChain:
    """Providers in order, each tried when the one before it fails.

    The point is the rate limit. A free or low tier runs out partway through a
    school day, and a teacher standing in front of a class should get a slightly
    different answer rather than no answer.

    Streaming commits late: a provider owns the reply only once it has produced
    its first chunk. Failing before that is invisible and falls through; failing
    after it cannot be undone, because the teacher is already reading the words.
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        if not providers:
            raise ValueError("a chain needs at least one provider")
        self._providers = providers
        # Reported to the teacher as the source of the answer, so it tracks who
        # actually replied rather than who was asked first.
        self.last_used: LLMProvider = providers[0]

    @property
    def name(self) -> str:
        return self.last_used.name

    @property
    def model(self) -> str | None:
        return self.last_used.model

    @property
    def supports_vision(self) -> bool:
        # The primary decides, because the image is attached while the prompt is
        # built — before anyone knows a fallback will be needed.
        return getattr(self._providers[0], "supports_vision", False)

    def chat(self, system: str, messages: list[ChatMessage]) -> str:
        last: LLMError | None = None
        for provider in self._providers:
            try:
                answer = provider.chat(system, messages)
                self.last_used = provider
                return answer
            except LLMError as exc:
                if exc.kind not in RETRYABLE_KINDS:
                    raise
                last = exc
                _log_fallthrough(provider, exc)
        raise last if last else LLMError("unavailable", "no provider answered")

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]:
        last: LLMError | None = None
        for provider in self._providers:
            try:
                stream = provider.chat_stream(system, messages)
                first = next(stream, None)
            except LLMError as exc:
                if exc.kind not in RETRYABLE_KINDS:
                    raise
                last = exc
                _log_fallthrough(provider, exc)
                continue

            # Past this point the teacher is reading it, so it is ours.
            self.last_used = provider
            if first is not None:
                yield first
            yield from stream
            return
        raise last if last else LLMError("unavailable", "no provider answered")

    def chat_stream_vision(
        self, system: str, messages: list[ChatMessage], image_data_url: str
    ) -> Iterator[str]:
        """Only the primary sees images; nothing else in the chain can. When it
        fails the caller retries through `chat_stream`, which loses the picture
        but keeps the answer."""
        primary = self._providers[0]
        stream = primary.chat_stream_vision(system, messages, image_data_url)
        first = next(stream, None)
        self.last_used = primary
        if first is not None:
            yield first
        yield from stream


def _log_fallthrough(provider: LLMProvider, exc: LLMError) -> None:
    logger.warning(
        "AI provider %s failed (%s) - trying the next in the chain", provider.name, exc.kind
    )
    if exc.kind == "auth":
        logger.error(
            "AI provider %s rejected its API key. The chain carried on, but this "
            "is a configuration problem and will not fix itself.",
            provider.name,
        )


def _build(name: str) -> LLMProvider | None:
    """One provider by name, or None when its key is not configured."""
    if name == "groq" and settings.groq_api_key:
        return OpenAICompatProvider(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key,
            model=settings.groq_model,
        )
    if name == "grok" and settings.xai_api_key:
        return OpenAICompatProvider(
            name="grok",
            base_url="https://api.x.ai/v1",
            api_key=settings.xai_api_key,
            model=settings.grok_model,
        )
    if name == "openai" and settings.openai_api_key:
        return OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            supports_vision=True,
        )
    if name == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    if name == "mock":
        return MockProvider()
    return None


def provider_chain_names() -> list[str]:
    """The primary followed by its fallbacks, de-duplicated, order preserved."""
    names = [settings.ai_provider]
    names += [n.strip() for n in settings.ai_fallback_providers.split(",") if n.strip()]
    seen: set[str] = set()
    ordered = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def get_provider() -> LLMProvider:
    """The active provider chain: the configured primary, then its fallbacks.

    Providers without a key are skipped rather than allowed to fail at request
    time — an unconfigured fallback is not a fallback. If none of them is usable
    the mock answers, which is what keeps local development working with no keys
    at all.
    """
    providers = [p for p in (_build(n) for n in provider_chain_names()) if p is not None]
    if not providers:
        return MockProvider()
    if len(providers) == 1:
        return providers[0]
    return ProviderChain(providers)
