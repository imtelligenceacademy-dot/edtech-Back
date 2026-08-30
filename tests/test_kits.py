"""Which hardware kit the teacher actually has in front of them.

Without this the assistant answers wiring questions from general micro:bit
knowledge, and names components the school has never owned.
"""

from __future__ import annotations

import pytest

from app.models import Lesson
from app.services import kits
from app.utils import new_id


def _lesson(db, *, year: int, grade: int) -> Lesson:
    lesson = Lesson(
        id=new_id("les"),
        title=f"Y{year} G{grade}",
        grade=grade,
        subject="STEAM",
        language="en",
        year=year,
    )
    db.add(lesson)
    db.commit()
    return lesson


@pytest.mark.parametrize(
    "year,grade,expected",
    [
        # Year 1 is the Honeycomb kit at every grade, top to bottom.
        (1, 1, kits.HONEYCOMB),
        (1, 6, kits.HONEYCOMB),
        (1, 7, kits.HONEYCOMB),
        (1, 12, kits.HONEYCOMB),
        # Year 2 starts on the same kit...
        (2, 1, kits.HONEYCOMB),
        (2, 6, kits.HONEYCOMB),
        # ...and switches where the course turns to Python.
        (2, 7, kits.SENSOR_45_IN_1),
        (2, 12, kits.SENSOR_45_IN_1),
    ],
)
def test_the_kit_follows_the_curriculum_track(db, year, grade, expected):
    assert kits.kit_for(_lesson(db, year=year, grade=grade)) is expected


def test_the_same_grade_can_mean_different_kits(db):
    """Grade 9 is Honeycomb in Year 1 and the sensor kit in Year 2 — the year
    decides, not the grade."""
    assert kits.kit_for(_lesson(db, year=1, grade=9)) is kits.HONEYCOMB
    assert kits.kit_for(_lesson(db, year=2, grade=9)) is kits.SENSOR_45_IN_1


def test_no_lesson_means_no_claim_about_hardware(db):
    """An ICT Fair project has no year or grade, so nothing is asserted."""
    assert kits.kit_for(None) is None
    assert kits.kit_note(None) == ""


def test_the_note_names_the_kit_and_its_model(db):
    note = kits.kit_note(_lesson(db, year=2, grade=8))
    assert "45-in-1" in note
    assert "KS4010" in note
    assert "Honeycomb" not in note  # never both


def test_the_note_forbids_hardware_the_school_does_not_own(db):
    note = kits.kit_note(_lesson(db, year=1, grade=3))
    assert "KS4011" in note
    assert "only this kit" in note
    # The specific wrong answer this exists to prevent.
    assert "never assume an arduino" in note.lower()
