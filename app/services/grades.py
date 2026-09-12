"""The grade vocabulary, and the one place integers and tokens are converted.

A grade is written two ways in this product. Teachers hold tokens — "KG1",
"G7" — because that is how a school says it. Lessons, progress and every
report group by ``Lesson.grade``, an integer, because that is how the
curriculum is ordered and how it has always been stored.

Kindergarten has no number, so it is given one below zero::

    KG1 -> -3    KG2 -> -2    KG3 -> -1    Grade 1 -> 1 ... Grade 12 -> 12

Negative rather than 0, 13, or a separate column, for three reasons: sorting a
track by grade already puts kindergarten ahead of Grade 1 with no comparator to
remember; every existing row keeps the value it has, so there is no migration
of curriculum that is already being taught; and there stays exactly one field
holding a lesson's grade, so no read site has to work out which of two columns
to believe.

The cost is that -3 means nothing to someone reading the table directly. That
is paid for here: nothing outside this module writes ``f"G{grade}"`` or reads
the sign of a grade, so the encoding is checkable in one file rather than
inferred from fifty.
"""

from __future__ import annotations

# Kindergarten, in curriculum order. The integers are an implementation detail
# of this module; everywhere else uses the tokens.
KG_TOKEN_TO_INT: dict[str, int] = {"KG1": -3, "KG2": -2, "KG3": -1}
KG_INT_TO_TOKEN: dict[int, str] = {v: k for k, v in KG_TOKEN_TO_INT.items()}

SCHOOL_GRADE_RANGE = range(1, 13)

# Every grade a teacher may be assigned, in the order a school lists them.
ALL_GRADE_TOKENS: tuple[str, ...] = (
    *KG_TOKEN_TO_INT,
    *(f"G{i}" for i in SCHOOL_GRADE_RANGE),
)


def is_kindergarten(grade: int | str) -> bool:
    """True for KG1/KG2/KG3, in either spelling."""
    if isinstance(grade, str):
        return grade in KG_TOKEN_TO_INT
    return grade in KG_INT_TO_TOKEN


def grade_token(grade: int | str) -> str:
    """The token form of a grade — "KG2", "G7" — matching ``User.grades``."""
    if isinstance(grade, str):
        return grade
    return KG_INT_TO_TOKEN.get(grade) or f"G{grade}"


def grade_number(token: str) -> int | None:
    """The integer a token is stored as, or None if it is not a grade.

    Used by the upload parser and by anything reading a grade out of a URL, so
    a hand-typed or stale value fails here rather than becoming a lesson nobody
    can be assigned.
    """
    text = (token or "").strip().upper()
    if text in KG_TOKEN_TO_INT:
        return KG_TOKEN_TO_INT[text]
    if text.startswith("G"):
        text = text[1:]
    if text.isdigit() and int(text) in SCHOOL_GRADE_RANGE:
        return int(text)
    return None


def grade_label(grade: int | str) -> str:
    """The grade as a school says it out loud: "KG2", "Grade 7".

    Kindergarten is already spoken as its token — nobody says "Grade KG2" — so
    it is returned unchanged rather than given a prefix that would read wrong
    in a report heading.
    """
    token = grade_token(grade)
    return token if is_kindergarten(token) else f"Grade {token.lstrip('G')}"
