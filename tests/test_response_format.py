"""Plain-text response formatting (AI_RESPONSE_FORMAT_NOTE).

Both chat UIs render replies as raw text, so any Markdown the model emits is
shown literally ("**Un micro:bit**") or collapses into unreadable pipes. Both
chat prompts must forbid it. The Word report prompt is the deliberate exception:
report_docx parses its Markdown headings.
"""

from __future__ import annotations

import pytest

from app.models import User
from app.models.enums import Role, UserStatus
from app.routers import ai


def _teacher_policy() -> str:
    user = User(
        id="u", name="T", email="t@x.com", password_hash="x",
        role=Role.teacher, status=UserStatus.active, language="en",
    )
    return ai._policy(user, vision_note=ai._VISION_OFF)


CHAT_PROMPTS = {
    "teacher": _teacher_policy,
    "school_admin": lambda: ai._ADMIN_GUARDRAILS,
}


@pytest.mark.parametrize("name", sorted(CHAT_PROMPTS))
def test_chat_prompts_forbid_markdown_tables(name):
    assert "Markdown tables" in CHAT_PROMPTS[name]()


@pytest.mark.parametrize("name", sorted(CHAT_PROMPTS))
def test_chat_prompts_forbid_markdown_headings(name):
    assert "Markdown headings" in CHAT_PROMPTS[name]()


@pytest.mark.parametrize("name", sorted(CHAT_PROMPTS))
def test_chat_prompts_forbid_bold_markers(name):
    text = CHAT_PROMPTS[name]()
    assert "**" in text and "__" in text, "must name the literal markers"


@pytest.mark.parametrize("name", sorted(CHAT_PROMPTS))
def test_chat_prompts_ask_for_plain_text_or_numbered_lists(name):
    lowered = CHAT_PROMPTS[name]().lower()
    assert "plain text" in lowered
    assert "numbered list" in lowered or "numbered steps" in lowered


def test_admin_prompt_offers_an_alternative_to_tables():
    """The data-heavy admin assistant is the most likely to reach for a table."""
    assert "instead of a table" in ai._ADMIN_GUARDRAILS


def test_admin_prompt_keeps_its_original_rules():
    """Formatting rules must not have displaced the scope/refusal behaviour."""
    assert ai.ADMIN_REFUSAL in ai._ADMIN_GUARDRAILS
    assert "never invent figures" in ai._ADMIN_GUARDRAILS
    assert "THIS SCHOOL's data only" in ai._ADMIN_GUARDRAILS


def test_word_report_prompts_still_request_markdown_headings():
    """Regression guard: report_docx turns '## ' lines into Word headings, so
    stripping Markdown here would flatten the generated report. The headings
    themselves may be renamed; the '## ' must survive."""
    for prompt in (ai._REPORT_SYSTEM, ai._PLATFORM_REPORT_SYSTEM):
        assert "## " in prompt
        assert "## What needs attention" in prompt


def test_report_prompts_forbid_restating_the_tables():
    """The narrative earns its page by deciding what matters, not by repeating
    the numbers printed underneath it."""
    for prompt in (ai._REPORT_SYSTEM, ai._PLATFORM_REPORT_SYSTEM):
        assert "Do NOT restate the tables" in prompt
        assert "Never invent or estimate a figure" in prompt


def test_report_builder_strips_bold_markers_itself():
    """The report path tolerates ** because the docx builder removes it."""
    from app.services.report_docx import _clean_inline

    assert _clean_inline("**Total** teachers") == "Total teachers"
    assert _clean_inline("__Late__ lessons") == "Late lessons"
