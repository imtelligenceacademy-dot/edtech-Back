"""What happens to the slide when the seeing provider drops out.

Only the primary can be sent an image, and it is attached while the prompt is
built — before anyone knows a rate limit is coming. Without this, a fallthrough
answered a question about a slide the model had never been told anything about,
from a prompt that still claimed an image was attached. Both halves of that are
fixed here: the claim, and the missing content.
"""

from __future__ import annotations

import pytest

from app.routers import ai
from app.routers.ai import PromptBundle, _stream_answer, _without_image
from app.services.llm import LLMError


class SeeingProvider:
    """Fails on the vision call, works on the text one — the real rate-limit shape."""

    name = "openai"
    model = "gpt-5.6-luna"
    supports_vision = True

    def __init__(self, *, vision_fails: str | None = "rate_limit", emit_first: bool = False):
        self._vision_fails = vision_fails
        self._emit_first = emit_first
        self.text_system: str | None = None

    def chat_stream_vision(self, system, messages, image_data_url):
        if self._emit_first:
            yield "half an answer"
        if self._vision_fails:
            raise LLMError(self._vision_fails, "vision unavailable")
        yield "saw the slide"

    def chat_stream(self, system, messages):
        self.text_system = system
        yield "answered from text"


def _bundle(provider, **kw):
    return PromptBundle(
        system="SYSTEM WITH IMAGE ATTACHED",
        messages=[{"role": "user", "content": "Explain the code on slide 8"}],
        source_ref="Lesson - slide 8",
        image_data_url="data:image/png;base64,AAAA",
        grounded=True,
        **kw,
    )


def test_a_rate_limited_vision_call_reruns_the_prompt_through_the_reader(monkeypatch):
    provider = SeeingProvider()
    monkeypatch.setattr(ai, "get_provider", lambda: provider)
    bundle = _bundle(provider, text_fallback=lambda: "SYSTEM WITH SLIDE READING")

    out = "".join(_stream_answer(bundle))

    assert out == "answered from text"
    assert provider.text_system == "SYSTEM WITH SLIDE READING", (
        "the retry must use the rebuilt prompt, not the one claiming an image"
    )


def test_the_teacher_never_sees_two_answers_spliced_together(monkeypatch):
    """Once words are on screen the reply is committed. Restarting would join
    the start of one answer to the middle of another."""
    provider = SeeingProvider(emit_first=True)
    monkeypatch.setattr(ai, "get_provider", lambda: provider)
    bundle = _bundle(provider, text_fallback=lambda: "SYSTEM WITH SLIDE READING")

    stream = _stream_answer(bundle)
    assert next(stream) == "half an answer"
    with pytest.raises(LLMError):
        list(stream)
    assert provider.text_system is None, "no second answer may be started"


def test_a_working_vision_call_is_left_alone(monkeypatch):
    provider = SeeingProvider(vision_fails=None)
    monkeypatch.setattr(ai, "get_provider", lambda: provider)

    out = "".join(_stream_answer(_bundle(provider, text_fallback=lambda: "UNUSED")))

    assert out == "saw the slide"
    assert provider.text_system is None


def test_a_failed_rebuild_still_answers(monkeypatch):
    """A prompt that overstates what the model can see beats no answer at all."""
    provider = SeeingProvider()
    monkeypatch.setattr(ai, "get_provider", lambda: provider)

    def boom():
        raise RuntimeError("reader exploded")

    out = "".join(_stream_answer(_bundle(provider, text_fallback=boom)))

    assert out == "answered from text"
    assert provider.text_system == "SYSTEM WITH IMAGE ATTACHED"


def test_without_image_is_a_no_op_when_there_was_never_an_image():
    bundle = PromptBundle(system="PLAIN", messages=[], source_ref=None)

    assert _without_image(bundle) == "PLAIN"


# --- The answer-length contract --------------------------------------------- #


def test_the_policy_asks_for_a_short_answer_first():
    text = ai._FORMAT.lower()

    assert "answer first" in text
    assert "120 words" in text, "the limit should be a number, not 'be concise'"
    assert "great question" in text, "the preamble it must not write is named"


def test_safety_steps_and_code_are_exempt_from_the_length_limit():
    """A word count that trims a wiring warning makes the assistant unsafe, not
    shorter. The exemption is the load-bearing half of the rule."""
    text = ai._FORMAT.lower()

    assert "never shorten" in text
    for protected in ("wiring procedure", "safety warning", "check before powering on", "code"):
        assert protected in text, f"{protected} must be exempt from the limit"
