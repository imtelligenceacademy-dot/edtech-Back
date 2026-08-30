"""The robotics teaching policy: scope, safety, formatting and language."""

from __future__ import annotations

from app.models import User
from app.models.enums import Role, UserStatus
from app.routers import ai


def _teacher(language: str = "en") -> User:
    return User(
        id="u_t",
        name="T",
        email="t@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        grades=["G7"],
        language=language,
    )


def _policy_text(language: str = "en") -> str:
    return ai._policy(_teacher(language), vision_note=ai._VISION_OFF)


def test_robotics_topics_are_in_scope():
    text = _policy_text().lower()
    for topic in ("micro:bit", "arduino", "servo", "makecode", "scratch", "wiring"):
        assert topic in text, f"{topic} should be in scope"


def test_coding_help_is_permitted_when_it_supports_the_lesson():
    text = _policy_text().lower()
    assert "python" in text and "debugging" in text


def test_unrelated_subjects_are_refused():
    text = _policy_text().lower()
    for banned in ("news", "sports", "entertainment", "personal"):
        assert banned in text, f"policy should name {banned} as out of scope"
    assert ai.REFUSAL in _policy_text()


def test_wiring_answers_require_safety_structure():
    text = _policy_text().lower()
    assert "voltage compatibility" in text
    assert "pin-to-pin" in text
    assert "check before powering on" in text
    # A load must never be driven straight off a controller pin.
    assert "must not be driven directly" in text


def test_mains_voltage_and_bypassing_protection_are_refused():
    text = _policy_text().lower()
    assert "mains" in text
    assert "high-current batteries" in text
    assert "bypassing or disabling any protection" in text


def test_lesson_material_takes_priority_over_general_knowledge():
    text = _policy_text().lower()
    assert "primary source" in text
    # ...while still allowing the model to expand beyond it.
    assert "your own robotics knowledge" in text
    assert "never invent what a slide shows" in text


def test_the_lesson_is_not_the_last_word_on_electrical_facts():
    """The policy used to say "never contradict the lesson material", full stop.

    That is right about teaching and wrong about electricity, and it is how a
    slide reading "1 turns the RGB LED on" got repeated to a teacher when the
    module is common-anode. The lesson still wins on what to teach and in what
    order; the verified hardware profile wins on what a pin does.
    """
    text = _policy_text().lower()
    assert "on electrical facts it is not" in text
    assert "follow the profile" in text
    assert "the slide looks incorrect" in text


def test_plain_text_formatting_is_required():
    text = _policy_text()
    lowered = text.lower()
    assert "no markdown tables" in lowered
    assert "no markdown headings" in lowered
    assert "**" in text  # the policy explicitly names the forbidden marker


def test_language_follows_the_teacher():
    assert "English" in _policy_text("en")
    assert "French" in _policy_text("fr")
    # Bilingual teachers default to English but still mirror the question.
    assert "same language the teacher wrote" in _policy_text("both").lower()


def test_vision_notes_are_honest_about_what_was_seen():
    seen = ai._vision_note("data:image/jpeg;base64,AAA", True, 3)
    assert "slide 3" in seen.lower()
    assert "never claim to see any other slide" in seen.lower()

    failed = ai._vision_note(None, True, 3)
    assert "could not be inspected" in failed.lower()
    assert "never describe what the slide looks like" in failed.lower()

    off = ai._vision_note(None, False, None)
    assert "only have the extracted text" in off.lower()
