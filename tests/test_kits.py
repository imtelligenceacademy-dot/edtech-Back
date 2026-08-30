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
    # The parts list is stated as exhaustive, so absence means something.
    assert "which is the complete list" in note


def test_the_note_forbids_hardware_the_school_does_not_own(db):
    note = kits.kit_note(_lesson(db, year=1, grade=3))
    assert "KS4011" in note
    assert "only this kit" in note
    # The specific wrong answer this exists to prevent.
    assert "never assume an arduino" in note.lower()


def test_a_kit_with_no_parts_list_says_so_rather_than_inventing_one(db, monkeypatch):
    """Told to use "only parts from this kit" and given no list, the assistant
    produced one: it offered a 220 ohm resistor and explained that the kit
    "includes a pack of them". It does not. Being asked to work inside a set it
    cannot see is what forces the invention, so an unlisted kit says so."""
    monkeypatch.setattr(kits, "HONEYCOMB", kits.Kit(name="Unlisted", model="KS0000"))
    note = kits.kit_note(_lesson(db, year=1, grade=3))

    assert "do NOT have this kit's parts list" in note
    assert "the kit includes" in note  # quoted as a phrase it must not use
    assert "never assert that it does" in note.lower()
    # No contents section is offered while none is known.
    assert "KIT CONTENTS" not in note


def test_neither_kit_has_the_resistors_that_were_offered(db):
    """The correction that started this: a 220 ohm resistor "from the kit"."""
    for year, grade in ((1, 3), (2, 9)):
        note = kits.kit_note(_lesson(db, year=year, grade=grade))
        assert "220 ohm" in note
        assert "Never offer these" in note


def test_the_kits_connect_in_completely_different_ways(db):
    """The mistake this prevents is subtler than naming a missing part: an
    answer can list every right component and still be wrong, because Year 1
    clips its modules together and Year 2 plugs them into a shield."""
    honeycomb = kits.kit_note(_lesson(db, year=1, grade=3))
    sensor_kit = kits.kit_note(_lesson(db, year=2, grade=9))

    assert "alligator clip" in honeycomb.lower()
    assert "never with Dupont jumper wires" in honeycomb

    assert "female-to-female Dupont wires" in sensor_kit
    assert "never alligator clips" in sensor_kit

    # And neither ever reaches for a breadboard.
    assert "breadboard" in honeycomb and "breadboard" in sensor_kit


def test_parts_are_named_the_way_the_kit_names_them(db):
    """A teacher matching an answer against the printed list needs the label
    they can see, not the manufacturer's catalogue name."""
    note = kits.kit_note(_lesson(db, year=2, grade=9))
    assert "18B20 Temperature Sensor" in note


def test_only_one_kit_ships_the_microbit_itself(db):
    """A teacher whose micro:bit dies needs to know whether the kit replaces it."""
    honeycomb = kits.kit_note(_lesson(db, year=1, grade=3))
    sensor_kit = kits.kit_note(_lesson(db, year=2, grade=9))

    assert "micro:bit Main Board (included in this kit)" in honeycomb
    assert "the micro:bit board itself, which is not part of this kit" in sensor_kit


def test_a_known_parts_list_becomes_the_whole_truth(db, monkeypatch):
    """Once contents are known they replace the "you cannot know" warning, and
    are stated as complete so absence is meaningful."""
    monkeypatch.setattr(
        kits,
        "HONEYCOMB",
        kits.Kit(
            name="Test Kit",
            model="KS0000",
            contents=("micro:bit V2", "LED module", "servo"),
        ),
    )
    note = kits.kit_note(_lesson(db, year=1, grade=3))

    assert "KIT CONTENTS, which is the complete list" in note
    assert "- LED module" in note
    assert "do NOT have this kit's parts list" not in note
