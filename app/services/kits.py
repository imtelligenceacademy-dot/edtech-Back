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
    # What the kit actually contains. While this is empty the assistant is told
    # it does not know the contents, because the alternative is what happened
    # when it was only given the kit's name: asked to use "only parts from this
    # kit", it invented a plausible parts list and told a teacher the kit
    # "includes a pack of 220 Ω resistors". It does not.
    contents: tuple[str, ...] = ()
    # Things wrongly offered to teachers, kept so the same mistake cannot
    # return. Useful before the full contents are known.
    excludes: tuple[str, ...] = ()
    # How the modules are actually joined up. The two kits differ completely
    # here - one clips, one plugs - and an answer that names the wrong method is
    # wrong even when every component in it is right.
    wiring: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.model})"


# The two kits in use, both transcribed from their own printed product lists.
# Model numbers matter: a teacher ordering a replacement part goes by them, so a
# wrong digit is worse than saying nothing.
#
# Year 1's hexagonal modules are joined with alligator clips; Year 2's plug into
# a shield with female-to-female Dupont wires. An answer written for one kit is
# wrong for the other even when every component it names is right.
HONEYCOMB = Kit(
    name="Keyestudio micro:bit Honeycomb Ultimate Starter Kit",
    model="KS4011",
    contents=(
        "micro:bit Main Board (included in this kit)",
        "micro:bit Expansion Board",
        "1W LED Module",
        "Digital LED Module",
        "5050 RGB Module",
        "Traffic Light Module",
        "Passive Buzzer",
        "Tactile Button Module",
        "Vibration Tilt Module",
        "Capacitive Touch Module",
        "Hall Magnetic Sensor",
        "PIR Motion Module",
        "Photoresistor",
        "Sound Sensor",
        "TEMT6000 Ambient Light Sensor",
        "Rotary Potentiometer Module",
        "PS2 Joystick Module",
        "Soil Humidity Sensor",
        "TCS34725FN Colour Sensor",
        "Black USB Cable",
        "Alligator Clip Cables (x10)",
        "2 x AA Battery Holder",
    ),
    excludes=(
        "loose resistors of any value, including 220 ohm - the LED modules have "
        "theirs built in",
        "Dupont jumper wires of any kind - this kit connects with alligator clips",
        "a breadboard",
    ),
    wiring=(
        "These are hexagonal plug-and-play modules. They are joined with the "
        "kit's alligator clip cables, or through the micro:bit Expansion Board - "
        "never with Dupont jumper wires, never on a breadboard, and nothing is "
        "soldered. Each module carries any resistor it needs on board."
    ),
)
# Transcribed from the kit's own parts list, items 1-49. Two details in it are
# the reason the assistant was getting wiring wrong: the wires are female-to-
# female, and there is not a loose resistor in the box - the LED modules carry
# their own, and everything plugs into the shield at item 1.
SENSOR_45_IN_1 = Kit(
    name="Keyestudio 45-in-1 Sensor Starter Kit for BBC micro:bit",
    model="KS4010",
    contents=(
        "micro:bit Sensor Shield V2 (every module plugs into this)",
        "Soil Humidity Sensor",
        "OLED Module",
        "Water Level Sensor",
        "Joystick Module",
        "Analog Alcohol Sensor",
        "Single Relay Module",
        "Analog Gas Sensor",
        "Ultrasonic Module",
        "Vibration Sensor",
        "Traffic Light Module",
        "Infrared Obstacle Detector Sensor",
        "Fan Module",
        "Flame Sensor",
        "Analog Sound Sensor",
        "Knock Sensor Module",
        "Digital IR Receiver Module",
        "Temperature and Humidity Sensor",
        "Ambient Light Sensor",
        "Photo Interrupter Module",
        "Capacitive Touch Sensor",
        "Digital Tilt Sensor",
        "Analog Rotation Sensor",
        "Steam Sensor",
        "Passive Buzzer Module",
        "Reed Switch Module",
        "Active Buzzer Module",
        "White LED Module",
        "PIR Motion Sensor",
        "Red LED Module",
        "Digital Push Button",
        "RGB LED Module",
        # The kit's list calls it 18B20; the full designation is added so the
        # assistant connects it to what it knows about the part.
        "18B20 Temperature Sensor (DS18B20)",
        "Analog Temperature Sensor",
        "LM35 Linear Temperature Sensor",
        "Hall Magnetic Sensor",
        "Photocell Sensor",
        "Line Tracking Sensor",
        "Ultraviolet Sensor",
        "Thin-film Pressure Sensor",
        "Magic Light Cup Sensor (x2)",
        "USB Cable",
        "3W LED Module",
        "40-pin FEMALE-to-FEMALE Dupont jumper wires",
        "Crash Sensor",
        "Display Module",
        "Micro Servo",
        "IR Remote Control",
        "6 x AA Battery Holder",
    ),
    excludes=(
        "loose resistors of any value, including 220 ohm - the LED modules have "
        "theirs built in",
        "male-to-female or male-to-male jumper wires - the kit's wires are "
        "female-to-female",
        "alligator clip cables - those belong to the other kit",
        "a breadboard",
        "the micro:bit board itself, which is not part of this kit",
    ),
    wiring=(
        "These are pre-built modules with 3-pin headers, not bare components. "
        "Each plugs into the micro:bit Sensor Shield V2 using the kit's "
        "female-to-female Dupont wires - never a breadboard, never alligator "
        "clips, and nothing is soldered. Each module carries any resistor it "
        "needs on board."
    ),
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


_HEADER = """CLASSROOM HARDWARE - the teacher works with this kit, and only this kit:
{kit}

- It is a BBC micro:bit kit. Never assume an Arduino, a Raspberry Pi, or a generic breadboard-and-components set.
- Refer to the kit by name when it matters; give the model number only if the teacher asks which kit or needs to order a replacement."""

# The rule that had to be added after the assistant, told to use "only parts
# from this kit" and given no parts list, produced one: it offered a teacher a
# 220 ohm resistor and explained that "the kit includes a pack of them". Being
# asked to work within a set it cannot see is what forces the invention, so it
# is now told plainly that it cannot see it.
_UNKNOWN_CONTENTS = """- You do NOT have this kit's parts list. Never state or imply what it contains or does not contain - no "the kit includes...", no "your kit comes with...", and never a quantity.
- When a part is needed, name the part and let the teacher check: "you will need a 220 ohm resistor if your kit has one" - never assert that it does.
- This kit connects its modules with its own leads and connectors, so do not reach for a breadboard, loose resistors or generic jumper wires unless the lesson slide shows them."""

_KNOWN_CONTENTS = """- KIT CONTENTS, which is the complete list. Anything not on it is not in the kit:
{contents}
- Answer wiring and component questions using these parts only, and by their names above. If a question needs something absent from the list, say so plainly and offer the closest part that is on it.
- HOW IT CONNECTS: {wiring}"""

_EXCLUDES = """- Confirmed NOT in this kit, whatever you may otherwise assume: {excludes}. Never offer these as if the teacher has them."""


def kit_note(lesson: Lesson | None) -> str:
    """The hardware section of the prompt for this lesson, or "" when unknown."""
    kit = kit_for(lesson)
    if kit is None:
        return ""

    parts = [_HEADER.format(kit=kit.label)]
    if kit.contents:
        listed = "\n".join(f"  - {item}" for item in kit.contents)
        parts.append(_KNOWN_CONTENTS.format(contents=listed, wiring=kit.wiring))
    else:
        parts.append(_UNKNOWN_CONTENTS)
    if kit.excludes:
        parts.append(_EXCLUDES.format(excludes="; ".join(kit.excludes)))
    return "\n".join(parts)
