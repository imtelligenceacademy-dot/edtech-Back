"""Which hardware kit is actually on the teacher's desk.

Without this the assistant answers wiring questions from general micro:bit
knowledge and cheerfully suggests components the school does not own. The kit is
decided by the curriculum track, so it is knowable from the lesson alone.

Year 1 is the Honeycomb kit at every grade. Year 2 starts on the same kit and
switches to the 45-in-1 sensor kit at the grade where the course turns to
Python — the same boundary that decides whether a slide needs reading as an
image, because it is the same change in the curriculum.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.models import Lesson


@dataclass(frozen=True)
class Kit:
    name: str
    model: str

    @property
    def label(self) -> str:
        return f"{self.name} ({self.model})"


# The two kits in use. Model numbers matter: a teacher ordering a replacement
# part goes by them, so a wrong digit here is worse than saying nothing.
HONEYCOMB = Kit(
    name="Keyestudio micro:bit Honeycomb Ultimate Starter Kit",
    model="KS4011",
)
SENSOR_45_IN_1 = Kit(
    name="Keyestudio 45-in-1 Sensor Starter Kit for BBC micro:bit",
    model="KS4010",
)


def kit_for(lesson: Lesson | None) -> Kit | None:
    """The kit this lesson is taught with, or None when it cannot be known."""
    if lesson is None:
        return None
    year = lesson.year or 2
    if year == 1:
        return HONEYCOMB
    grade = lesson.grade or 0
    return HONEYCOMB if grade < settings.year2_advanced_from_grade else SENSOR_45_IN_1


# Stated as a constraint rather than a fact, because the failure it prevents is
# the assistant confidently describing a component the school has never owned.
KIT_NOTE = """CLASSROOM HARDWARE - the teacher has this kit, and only this kit:
{kit}

- Every wiring, component and pin answer must use parts from this kit. It is a BBC micro:bit kit: never assume an Arduino, a Raspberry Pi, or a generic breadboard set.
- If a question needs something the kit does not contain, say so plainly and offer the closest part that is in it, rather than describing hardware the teacher cannot put their hands on.
- Refer to the kit by name when it matters; give the model number only if the teacher asks which kit or needs to order a replacement."""


def kit_note(lesson: Lesson | None) -> str:
    kit = kit_for(lesson)
    return KIT_NOTE.format(kit=kit.label) if kit else ""
