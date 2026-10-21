"""Working out that a chat message asks for a quiz or exam, and over which lessons.

A teacher on the Grade 12 page typed "write an exam for gr 12 about the 1st 4
lessons". The assistant is given one lesson's text — the open one — so it
answered that it only had Lesson 4 and asked her to paste the other three. The
platform holds all of them; nothing was reading which ones she meant.

Two steps, cheapest first:

- ``is_assessment_request`` decides whether the message is asking for an
  assessment at all. Ordinary questions must not pass it — they are answered
  exactly as before, at exactly the old cost — so the ambiguous words are only
  counted when used as the noun: "write a test for…", not "test the circuit".
- ``parse_lesson_numbers`` reads the lessons out of the common phrasings, in
  English, French and Arabic, without asking a model. When it cannot,
  ``resolve_lessons_with_model`` asks one, briefly, with the list of lessons
  she actually has to choose from.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

# Eastern Arabic and Persian digits, read as the digits they are.
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

_NUMBER_WORDS = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    # French ("une" deliberately absent: it is the article far more often)
    "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
}

# Above this a number attached to "lesson" is not a lesson number anybody has.
_MAX_LESSON_NO = 60

_LESSON = r"(?:lessons?|lesson|le[cç]ons?|دروس|الدروس|درس|الدرس)"
_RANGE_JOIN = r"(?:-|–|—|to|through|thru|until|à|a|au|jusqu'?à|إلى|الى)"
_LIST_JOIN = r"(?:,|and|&|et|و)"

_ASSESSMENT_WORDS = re.compile(
    r"""
    \b(?:quiz(?:zes)?|exams?|examination|assessments?|worksheets?|mcqs?|
         examens?|[ée]valuations?|interrogations?|qcm)\b
    | \b(?:a|an|short|quick|final|unit|mini|class|written)\s+tests?\b
    | \b(?:un|une|petit|court)\s+(?:test|contr[oô]le)s?\b
    | \btests?\s+(?:on|about|for|covering|of|sur)\b
    | \b\d+\s+(?:multiple[- ]choice\s+)?questions?\b
    | \bquestions?\s+(?:on|about|for|covering)\s+(?:the\s+)?(?:first|last|\d|lessons?)
    | (?:اختبار|امتحان|مسابقة|تقييم)
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _normalise(text: str) -> str:
    text = text.translate(_DIGITS).lower()
    text = re.sub(r"(\d+)(?:st|nd|rd|th|er|ère|e|ème)\b", r"\1", text)
    text = re.sub(
        r"\b(" + "|".join(_NUMBER_WORDS) + r")\b",
        lambda m: str(_NUMBER_WORDS[m.group(1)]),
        text,
    )
    return text


def is_assessment_request(text: str) -> bool:
    """Whether the message asks for a quiz, test or exam."""
    return bool(_ASSESSMENT_WORDS.search(_normalise(text or "")))


def _span(first: int, last: int) -> list[int] | None:
    if first < 1 or last < first or last > _MAX_LESSON_NO:
        return None
    return list(range(first, last + 1))


def parse_lesson_numbers(text: str, *, current_no: int | None = None) -> list[int] | None:
    """The lesson numbers a message names, or None when it names none.

    ``current_no`` is the open lesson's number, which "the last 3 lessons"
    counts back from.
    """
    t = _normalise(text or "")

    # "lessons 1-4", "lessons 1 to 4", "leçons 1 à 4", "الدروس 1 إلى 4"
    m = re.search(rf"{_LESSON}\s*(\d+)\s*{_RANGE_JOIN}\s*(?:{_LESSON}\s*)?(\d+)", t)
    if m:
        return _span(int(m.group(1)), int(m.group(2)))

    # "first 4 lessons", "1st 4 lessons" ("1st" is "1" by now), "the 4 first lessons"
    m = re.search(rf"\b(?:first|1)\s+(\d+)\s+{_LESSON}", t) or re.search(
        rf"\b(\d+)\s+first\s+{_LESSON}", t
    )
    if m:
        return _span(1, int(m.group(1)))

    # "les 4 premières leçons", "les premières 4 leçons", "أول 4 دروس"
    m = (
        re.search(rf"\b(\d+)\s+premi[eè]res?\s+{_LESSON}", t)
        or re.search(rf"premi[eè]res?\s+(\d+)\s+{_LESSON}", t)
        or re.search(rf"أول\s*(\d+)\s*{_LESSON}", t)
    )
    if m:
        return _span(1, int(m.group(1)))

    # "last 3 lessons", "les 3 dernières leçons", "آخر 3 دروس"
    m = (
        re.search(rf"\blast\s+(\d+)\s+{_LESSON}", t)
        or re.search(rf"\b(\d+)\s+derni[eè]res?\s+{_LESSON}", t)
        or re.search(rf"آخر\s*(\d+)\s*{_LESSON}", t)
    )
    if m:
        if current_no is None:
            return None
        count = int(m.group(1))
        return _span(max(1, current_no - count + 1), current_no)

    # "lessons 1, 3 and 4", "leçons 2 et 5"
    m = re.search(rf"{_LESSON}\s*(\d+(?:\s*{_LIST_JOIN}\s*\d+)+)", t)
    if m:
        found = sorted({int(n) for n in re.findall(r"\d+", m.group(1))})
        found = [n for n in found if 1 <= n <= _MAX_LESSON_NO]
        return found or None

    # "lesson 3" — every one mentioned, so "lesson 2 and lesson 5" is both
    found = sorted({int(n) for n in re.findall(rf"{_LESSON}\s*(\d+)\b", t)})
    found = [n for n in found if 1 <= n <= _MAX_LESSON_NO]
    return found or None


_RESOLVER_SYSTEM = (
    "You read a teacher's request and say which lessons it is about. You are "
    "given the request and the numbered list of lessons the teacher has. Reply "
    'with JSON only, in the form {"lessons": [1, 2, 3]}, listing the lesson '
    "numbers the request covers. If the request does not identify any lessons, "
    'reply {"lessons": []}. Never include a number that is not in the list.'
)


def resolve_lessons_with_model(
    text: str, index: Sequence[tuple[int, str]], provider
) -> list[int] | None:
    """Ask a model which of ``index`` (number, title) the request means.

    For the phrasings the parser does not know — "the lessons on variables",
    other word orders, other languages. Fails soft: any error, or anything that
    is not a JSON list of numbers from the index, is None, and the caller falls
    back to the open lesson.
    """
    if not index:
        return None
    listing = "\n".join(f"Lesson {no}: {title}" for no, title in index)
    try:
        reply = provider.chat(
            _RESOLVER_SYSTEM,
            [{"role": "user", "content": f"LESSONS:\n{listing}\n\nREQUEST:\n{text}"}],
        )
        match = re.search(r"\{.*\}", reply, re.DOTALL)
        numbers = json.loads(match.group(0))["lessons"] if match else None
    except Exception:
        return None
    if not isinstance(numbers, list):
        return None
    known = {no for no, _ in index}
    picked = sorted({n for n in numbers if isinstance(n, int) and n in known})
    return picked or None
