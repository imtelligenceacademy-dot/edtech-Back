"""A quiz or exam across several lessons, asked for in the chat.

On the Grade 12 page, with Lesson 4 open, a teacher typed "write an exam for gr
12 about the 1st 4 lessons". The assistant was given Lesson 4's text and
nothing else, so it said so and asked her to paste the other three — lessons
the platform holds and she is assigned. Lessons 1-3 were also marked complete,
which locks them, so even naming them one at a time would not have reached
them.

These hold the fix: the lessons a request names are read and loaded, whatever
their lock status, from her own assignments only; the answer gets room for an
answer key; and a question that is not a quiz request is answered exactly as
before.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.models import Lesson, LessonAssignment, Progress, School, Slide, User
from app.models.enums import LessonStatus, Role, UserStatus
from app.routers import ai
from app.schemas.ai import AIChatRequest
from app.services import llm
from app.services.assessment_request import (
    is_assessment_request,
    parse_lesson_numbers,
    resolve_lessons_with_model,
)
from app.services.auto_assign import sync_teacher_assignments
from app.services.grades import grade_number
from app.services.lesson_access import is_lesson_available
from app.utils import new_id

SCREENSHOT = "write an exam for gr 12  about the 1st 4 lessons"


# --------------------------------------------------------------------------- #
# Reading the request
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "message,expected",
    [
        (SCREENSHOT, [1, 2, 3, 4]),
        ("make a quiz on lessons 2-5", [2, 3, 4, 5]),
        ("quiz on lessons 1 to 4", [1, 2, 3, 4]),
        ("a test on lessons 1, 3 and 4", [1, 3, 4]),
        ("write a test about the first four lessons", [1, 2, 3, 4]),
        ("exam for lesson 3", [3]),
        ("fais un examen sur les leçons 1 à 3", [1, 2, 3]),
        ("prépare une évaluation sur les 4 premières leçons", [1, 2, 3, 4]),
        ("اكتب امتحان عن الدروس ١ إلى ٤", [1, 2, 3, 4]),
        ("اختبار عن أول 3 دروس", [1, 2, 3]),
        ("give me 10 questions about lessons 1-2", [1, 2]),
    ],
)
def test_the_request_is_read_for_its_lessons(message, expected):
    assert is_assessment_request(message)
    assert parse_lesson_numbers(message) == expected


def test_the_last_few_lessons_count_back_from_the_open_one():
    assert parse_lesson_numbers("a quiz on the last 3 lessons", current_no=4) == [2, 3, 4]
    # Without an open lesson to count from there is nothing to count back from.
    assert parse_lesson_numbers("a quiz on the last 3 lessons") is None


@pytest.mark.parametrize(
    "message",
    [
        "how do I test the circuit?",
        "test the code in lesson 3",
        "why does the test fail?",
        "explain the contrôle du moteur",
        "what does print() do in lesson 4?",
    ],
)
def test_an_ordinary_question_is_not_a_quiz_request(message):
    """Ordinary questions must not pass: they keep the old prompt and cost."""
    assert not is_assessment_request(message)


class _Says:
    def __init__(self, reply):
        self.reply = reply

    def chat(self, system, messages):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_the_model_resolver_only_returns_lessons_she_has():
    index = [(1, "Hello world"), (2, "My first variable")]

    assert resolve_lessons_with_model("x", index, _Says('{"lessons": [2, 9, "x"]}')) == [2]
    assert resolve_lessons_with_model("x", index, _Says("I think lessons 1 and 2")) is None
    assert resolve_lessons_with_model("x", index, _Says(RuntimeError("down"))) is None
    assert resolve_lessons_with_model("x", index, _Says('{"lessons": []}')) is None


# --------------------------------------------------------------------------- #
# Building the prompt
# --------------------------------------------------------------------------- #

@pytest.fixture()
def school(db):
    row = School(id=new_id("sch"), name="Assessments", country="LB", city="Beirut", program_year=2)
    db.add(row)
    db.flush()
    return row


@pytest.fixture()
def teacher(db, school):
    row = User(
        id=new_id("usr"),
        name="Grade 12 teacher",
        email=f"{new_id('t')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G12"],
        language="en",
    )
    db.add(row)
    db.flush()
    return row


def _lesson(db, no, *, course="python", token="G12", language="en", body=None):
    row = Lesson(
        id=new_id("les"),
        title=f"{token} {course} lesson {no:02d}",
        grade=grade_number(token),
        subject="STEAM",
        language=language,
        year=2,
        course=course,
        lesson_no=no,
    )
    db.add(row)
    db.flush()
    db.add(
        Slide(
            id=new_id("sld"),
            lesson_id=row.id,
            index=1,
            title=f"Slide one of {course} {token} {language} {no}",
            body=body if body is not None else f"marker-{course}-{token}-{language}-L{no}",
        )
    )
    db.flush()
    return row


def _complete(db, teacher, lesson):
    """Marked complete — which locks it, and is what made 1-3 unreachable.

    Assigning a lesson already gave it a progress row, so that row is the one
    marked; a second would break the one-row-per-class rule.
    """
    row = db.scalar(
        select(Progress).where(
            Progress.teacher_id == teacher.id,
            Progress.lesson_id == lesson.id,
            Progress.section == "",
        )
    )
    if row is None:
        row = Progress(id=new_id("prg"), teacher_id=teacher.id, lesson_id=lesson.id, section="")
        db.add(row)
    row.status = LessonStatus.completed
    row.percent_complete = 100
    db.flush()


@pytest.fixture()
def grade_twelve(db, teacher):
    """The screenshot's situation: Python 1-4, the first three completed."""
    lessons = [_lesson(db, n) for n in (1, 2, 3, 4)]
    sync_teacher_assignments(db, teacher)
    db.flush()
    for lesson in lessons[:3]:
        _complete(db, teacher, lesson)
    open_lesson = lessons[3]
    # Without these the test could pass for the wrong reason: Lesson 4 must be
    # the one she can open, and 1-3 must be locked to her, or "lock status is
    # ignored" is not what is being tested.
    assert is_lesson_available(db, teacher, open_lesson.id), "Lesson 4 must be open"
    assert not is_lesson_available(db, teacher, lessons[0].id), "Lesson 1 must be locked"
    return lessons


def _ask(db, teacher, open_lesson, message):
    return ai._build_prompt(db, teacher, AIChatRequest(message=message, lessonId=open_lesson.id))


def test_the_screenshot_request_is_built_from_all_four_lessons(db, teacher, grade_twelve):
    """The reported bug. Lessons 1-3 are completed and locked, Lesson 4 is open,
    and the exam is built from all four."""
    bundle = _ask(db, teacher, grade_twelve[3], SCREENSHOT)

    assert bundle.refusal is None
    for no in (1, 2, 3, 4):
        assert f"marker-python-G12-en-L{no}" in bundle.system
        assert f"=== Lesson {no}: " in bundle.system
    assert bundle.source_ref == "Lessons 1–4"
    assert "ANSWER KEY (teacher only)" in bundle.system
    # Room for the answer key, and no slide image spent on an exam.
    assert bundle.long_answer is True
    assert bundle.image_data_url is None


def test_an_ordinary_question_is_answered_as_before(db, teacher, grade_twelve):
    open_lesson = grade_twelve[3]
    bundle = _ask(db, teacher, open_lesson, "How do I explain variables to the class?")

    assert bundle.long_answer is False
    assert bundle.source_ref == open_lesson.title
    assert "marker-python-G12-en-L4" in bundle.system
    assert "marker-python-G12-en-L1" not in bundle.system
    assert "ANSWER KEY" not in bundle.system
    assert "LESSONS THIS TEACHER HAS" not in bundle.system


def _assign_by_hand(db, teacher, lesson):
    """What the Access Control override does: an assignment outside her rule."""
    db.add(
        LessonAssignment(
            id=new_id("asg"), lesson_id=lesson.id, teacher_id=teacher.id, source="manual"
        )
    )
    db.flush()


def test_nothing_comes_from_another_grade_language_or_course(db, teacher, grade_twelve):
    """Her own assignments, in the open lesson's grade, language and course.

    Each of these is genuinely assigned to her — an admin can hand a teacher a
    lesson from another grade or language — so it is the grade, language and
    course rules that keep them out, not the absence of an assignment.
    """
    _assign_by_hand(db, teacher, _lesson(db, 1, token="G11"))
    _assign_by_hand(db, teacher, _lesson(db, 1, language="fr"))
    _lesson(db, 1, course="microbit")
    sync_teacher_assignments(db, teacher)
    db.flush()

    bundle = _ask(db, teacher, grade_twelve[3], SCREENSHOT)

    assert "marker-python-G11" not in bundle.system
    assert "marker-python-G12-fr" not in bundle.system
    assert "marker-microbit" not in bundle.system


def test_a_course_named_in_the_request_wins(db, teacher, grade_twelve):
    """"The micro:bit lessons 1-2", asked from a Python page, means those."""
    _lesson(db, 1, course="microbit")
    _lesson(db, 2, course="microbit")
    sync_teacher_assignments(db, teacher)
    db.flush()

    bundle = _ask(db, teacher, grade_twelve[3], "a quiz on the micro:bit lessons 1-2")

    assert "marker-microbit-G12-en-L1" in bundle.system
    assert "marker-microbit-G12-en-L2" in bundle.system
    assert "marker-python" not in bundle.system


def test_a_lesson_not_assigned_to_her_is_never_used(db, teacher, grade_twelve, school):
    """Assigned to her is the rule. A lesson in her grade and course that is
    assigned to another teacher is not hers to examine, whatever is asked."""
    colleague = User(
        id=new_id("usr"),
        name="Colleague",
        email=f"{new_id('t')}@example.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G12"],
        language="en",
    )
    db.add(colleague)
    db.flush()
    # Created after her assignments were synced, then synced for the colleague
    # only: assigned, but to somebody else.
    lesson_five = _lesson(db, 5)
    sync_teacher_assignments(db, colleague)
    db.flush()
    assert db.scalar(
        select(LessonAssignment.id).where(
            LessonAssignment.lesson_id == lesson_five.id,
            LessonAssignment.teacher_id == colleague.id,
        )
    ), "precondition: lesson 5 is assigned, to the colleague"

    bundle = _ask(db, teacher, grade_twelve[3], "a quiz on lessons 1-5")

    assert "marker-python-G12-en-L5" not in bundle.system
    assert bundle.source_ref == "Lessons 1–4"


def test_more_lessons_than_fit_are_capped_and_she_is_told(db, teacher, grade_twelve, monkeypatch):
    monkeypatch.setattr(settings, "ai_assessment_max_lessons", 2)

    bundle = _ask(db, teacher, grade_twelve[3], "a quiz on lessons 1-4")

    assert bundle.source_ref == "Lessons 1–2"
    assert "marker-python-G12-en-L3" not in bundle.system
    assert "covers the first 2" in bundle.system


def test_the_lessons_share_one_budget(db, teacher, monkeypatch):
    """Naming four lessons must not make the prompt four times the size."""
    lessons = [_lesson(db, n, body="x" * 1000) for n in (1, 2, 3, 4)]
    sync_teacher_assignments(db, teacher)
    db.flush()
    monkeypatch.setattr(settings, "ai_assessment_context_chars", 400)

    bundle = _ask(db, teacher, lessons[0], "a quiz on lessons 1-4")

    assert bundle.source_ref == "Lessons 1–4"
    assert bundle.system.count("x" * 50) <= 8, "each lesson should be cut to its share"
    assert bundle.system.count("x") < 800


def test_the_model_reads_what_the_parser_cannot(db, teacher, grade_twelve, monkeypatch):
    asked = {}

    def resolver(text, index, provider):
        asked["index"] = index
        return [2, 3]

    monkeypatch.setattr(ai, "resolve_lessons_with_model", resolver)

    bundle = _ask(db, teacher, grade_twelve[3], "a quiz on the lessons about variables")

    assert bundle.source_ref == "Lessons 2–3"
    # It chose from her lessons, numbered.
    assert [no for no, _ in asked["index"]] == [1, 2, 3, 4]


def test_unread_lessons_fall_back_to_the_open_one_with_her_list(db, teacher, grade_twelve, monkeypatch):
    """Nothing resolved: answered from the open lesson as before, but with her
    lessons listed, so the reply asks which she meant instead of asking her to
    paste lessons the platform already has."""
    monkeypatch.setattr(ai, "resolve_lessons_with_model", lambda *a: None)
    open_lesson = grade_twelve[3]

    bundle = _ask(db, teacher, open_lesson, "a quiz on the lessons about variables")

    assert bundle.long_answer is False
    assert bundle.source_ref == open_lesson.title
    assert "LESSONS THIS TEACHER HAS IN THIS COURSE" in bundle.system
    for no in (1, 2, 3, 4):
        assert f"Lesson {no}: G12 python lesson {no:02d}" in bundle.system
    assert "Never ask her to paste or upload" in bundle.system


# --------------------------------------------------------------------------- #
# Room for the answer
# --------------------------------------------------------------------------- #

class _Recording:
    """A provider that records how it was called."""

    name = "recording"
    model = None
    supports_vision = False

    def __init__(self):
        self.kwargs = None

    def chat_stream(self, system, messages, **kwargs):
        self.kwargs = kwargs
        yield "ok"


def test_the_exam_asks_the_provider_for_a_long_answer(db, teacher, grade_twelve, monkeypatch):
    provider = _Recording()
    monkeypatch.setattr(ai, "get_provider", lambda: provider)

    bundle = _ask(db, teacher, grade_twelve[3], SCREENSHOT)
    assert list(ai._stream_answer(bundle)) == ["ok"]

    assert provider.kwargs == {"long_answer": True}


def test_an_ordinary_answer_calls_the_provider_as_it_always_has(db, teacher, grade_twelve, monkeypatch):
    """Every existing provider, and every test double of one, takes two
    arguments. An ordinary question must still call them with exactly that."""
    provider = _Recording()
    monkeypatch.setattr(ai, "get_provider", lambda: provider)
    # No slide image for this one, so the plain streaming path is taken.
    monkeypatch.setattr(ai, "_slide_image", lambda *a, **k: (None, False))

    bundle = _ask(db, teacher, grade_twelve[3], "How do I explain variables?")
    list(ai._stream_answer(bundle))

    assert provider.kwargs == {}


class _AnthropicReply:
    status_code = 200

    def json(self):
        return {"content": [{"type": "text", "text": "an exam"}]}


@pytest.mark.parametrize("long_answer,max_tokens", [(False, 1024), (True, 6000)])
def test_anthropic_is_given_room_for_an_answer_key(monkeypatch, long_answer, max_tokens):
    """It hard-coded 1024 tokens, which cut an exam off before its answer key
    whenever Anthropic was the provider that answered."""
    sent = {}

    def post(url, **kwargs):
        sent.update(kwargs)
        return _AnthropicReply()

    monkeypatch.setattr(llm.httpx, "post", post)
    provider = llm.AnthropicProvider(api_key="key", model="model")

    provider.chat("system", [{"role": "user", "content": "hi"}], long_answer=long_answer)

    assert sent["json"]["max_tokens"] == max_tokens
    assert sent["timeout"] == (
        settings.ai_assessment_timeout_seconds if long_answer else settings.ai_timeout_seconds
    )
