"""Provider-agnostic LLM layer for the teacher assistant.

Every provider implements the same `chat(system, messages) -> str` interface,
so switching between Grok, GPT-4o, Claude, or the no-key mock is a one-line
config change (``AI_PROVIDER`` in the environment). API keys live only here on
the server — never in the frontend.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Protocol, TypedDict

import httpx

from app.config import settings


class ChatMessage(TypedDict):
    role: str  # "user" | "assistant"
    content: str


class LLMProvider(Protocol):
    name: str
    model: str | None

    def chat(self, system: str, messages: list[ChatMessage]) -> str: ...

    def chat_stream(self, system: str, messages: list[ChatMessage]) -> Iterator[str]: ...


# --------------------------------------------------------------------------- #
# Mock — works with no API key, for local development.
# --------------------------------------------------------------------------- #
class MockProvider:
    name = "mock"
    model = None

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
    def __init__(self, *, name: str, base_url: str, api_key: str, model: str):
        self.name = name
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

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


def get_provider() -> LLMProvider:
    """Resolve the active provider, falling back to mock if its key is missing."""
    provider = settings.ai_provider
    if provider == "groq" and settings.groq_api_key:
        return OpenAICompatProvider(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            api_key=settings.groq_api_key,
            model=settings.groq_model,
        )
    if provider == "grok" and settings.xai_api_key:
        return OpenAICompatProvider(
            name="grok",
            base_url="https://api.x.ai/v1",
            api_key=settings.xai_api_key,
            model=settings.grok_model,
        )
    if provider == "openai" and settings.openai_api_key:
        return OpenAICompatProvider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            model=settings.openai_model,
        )
    if provider == "anthropic" and settings.anthropic_api_key:
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=settings.anthropic_model)
    return MockProvider()
