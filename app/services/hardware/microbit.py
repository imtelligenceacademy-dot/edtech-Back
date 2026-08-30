"""Layer 2 - the board itself.

Kit knowledge answers "what do I have"; this answers "where can I plug it in".
They fail differently. A wrong kit fact offers a teacher a part they do not own,
which is annoying. A wrong pin fact tells them to wire a sensor to P5 and the
build fails for a reason no amount of re-checking the code will reveal, because
P5 is button A.

Pins are held as data rather than prose so a question naming P3 can be answered
with P3's actual constraints instead of a paragraph the model has to search.
Everything here is from the official micro:bit hardware documentation, which is
why it is the one layer allowed to state absolute numbers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.hardware.schema import Verification, Verified

MICROBIT_DOCS = "https://tech.microbit.org/hardware/edgeconnector/"

MICROBIT_VERIFICATION = Verification(
    status=Verified.microbit_doc,
    source="BBC micro:bit developer documentation, edge connector and pinout",
    source_url=MICROBIT_DOCS,
    last_verified="2026-08-30",
)


@dataclass(frozen=True)
class Pin:
    name: str
    analog_in: bool
    pwm: bool
    # Empty when the pin is genuinely free. Anything here is a reason a working
    # circuit on this pin can still break something else on the board.
    shared_with: str = ""
    note: str = ""

    @property
    def free(self) -> bool:
        return not self.shared_with

    def describe(self) -> str:
        caps = ["digital in/out"]
        if self.analog_in:
            caps.append("analog in")
        if self.pwm:
            caps.append("PWM out")
        line = f"{self.name}: {', '.join(caps)}"
        if self.shared_with:
            line += f" - SHARED WITH {self.shared_with}"
        if self.note:
            line += f". {self.note}"
        return line


# The three large rings plus every numbered pin on the edge connector. The
# `shared_with` values are the whole reason this table exists: six of the pins
# drive the LED display and two are the buttons, so eight of the twenty look
# free in a diagram and are not.
_MATRIX = "the 5x5 LED display"
PINS: dict[str, Pin] = {
    "P0": Pin("P0", True, True, note="Large ring. General purpose, and the usual first choice."),
    "P1": Pin("P1", True, True, note="Large ring. General purpose."),
    "P2": Pin("P2", True, True, note="Large ring. General purpose."),
    "P3": Pin("P3", True, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P4": Pin("P4", True, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P5": Pin("P5", False, True, "button A", "Reads LOW while button A is held; it has a pull-up."),
    "P6": Pin("P6", False, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P7": Pin("P7", False, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P8": Pin("P8", False, True, note="Free general-purpose pin."),
    "P9": Pin("P9", False, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P10": Pin("P10", True, True, _MATRIX, "Usable only after the display is turned off in code."),
    "P11": Pin("P11", False, True, "button B", "Reads LOW while button B is held; it has a pull-up."),
    "P12": Pin("P12", False, True, note="Free general-purpose pin, reserved for accessibility on V1."),
    "P13": Pin("P13", False, True, "SPI SCK", "Free if you are not using SPI."),
    "P14": Pin("P14", False, True, "SPI MISO", "Free if you are not using SPI."),
    "P15": Pin("P15", False, True, "SPI MOSI", "Free if you are not using SPI."),
    "P16": Pin("P16", False, True, note="Free general-purpose pin."),
    "P19": Pin("P19", False, False, "I2C SCL and the on-board accelerometer/compass", "Do not use as a plain GPIO."),
    "P20": Pin("P20", False, False, "I2C SDA and the on-board accelerometer/compass", "Do not use as a plain GPIO."),
}

ANALOG_PINS = tuple(name for name, pin in PINS.items() if pin.analog_in)
FREE_PINS = tuple(name for name, pin in PINS.items() if pin.free)

_PIN_PATTERN = re.compile(r"\bP(\d{1,2})\b", re.IGNORECASE)


def pins_mentioned(text: str) -> tuple[Pin, ...]:
    """Every micro:bit pin named in a piece of text, in the order written.

    Used to answer "can I connect this to P0?" with P0's actual constraints
    rather than with a general paragraph about pins.
    """
    seen: list[Pin] = []
    for match in _PIN_PATTERN.finditer(text or ""):
        name = f"P{int(match.group(1))}"
        pin = PINS.get(name)
        if pin is not None and pin not in seen:
            seen.append(pin)
    return tuple(seen)


BOARD_FACTS = """MICRO:BIT BOARD:
- Logic level is 3.3 V. A pin driven HIGH is at about 3.3 V, LOW at about 0 V. Never feed a GPIO pin more than 3.3 V.
- The analog input range is 0 to 1023 across 0 to 3.3 V, on the analog-capable pins only.
- Analog write (PWM) takes 0 to 1023, where 0 is permanently LOW and 1023 is permanently HIGH. It is a duty cycle, not a voltage.
- Only P0, P1, P2, P3, P4 and P10 can read analog. Every numbered pin can do digital in and out.
- P19 and P20 are the I2C bus and are shared with the on-board accelerometer and compass. Do not use them as ordinary GPIO.
- The 3V ring powers modules from the board's own regulator. It is limited: keep the total draw of everything connected modest, and give motors, fans and anything with a coil their own supply with a shared ground.
- Total current available on the 3V pin is roughly 90 mA on V1 and around 190 mA on V2, shared by everything plugged in.

MICRO:BIT V1 VS V2:
- V2 adds a speaker, a microphone and a touch-sensitive logo; V1 has none of them.
- The V2 speaker matters in buzzer lessons: music blocks play through the on-board speaker even with nothing wired up, so a silent external buzzer can be missed entirely. On V1 nothing is heard unless a buzzer is connected.
- V2 supports true capacitive touch on P0, P1 and P2. V1 does not; its touch is resistive and needs the user to also hold GND.
- V2 has more memory, so a program that runs out of room on V1 may be fine on V2. The pinout is otherwise the same."""

LANGUAGES = """THE TWO PROGRAMMING ENVIRONMENTS - the language changes, the electronics do not:
- MakeCode `pins.digitalWritePin(DigitalPin.P0, 1)` and MicroPython `pin0.write_digital(1)` do exactly the same thing: drive P0 HIGH. Explain the physical result identically in both.
- MakeCode `pins.analogReadPin(AnalogPin.P0)` and MicroPython `pin0.read_analog()` both return 0-1023 from the same converter.
- MakeCode `pins.analogWritePin(AnalogPin.P0, n)` and MicroPython `pin0.write_analog(n)` both set a PWM duty cycle, 0-1023.
- MicroPython on micro:bit is not full Python: there is no pip, and libraries are the ones built into the runtime plus what a lesson provides.
- MakeCode block code and its JavaScript view are the same program. A teacher looking at blocks and a teacher looking at JavaScript are asking about the same thing.
- When asked to convert between them, convert the code and keep the electrical explanation word for word the same. If the two explanations differ, one of them is wrong."""


def board_note(question: str = "") -> str:
    """The micro:bit layer, with any pins the teacher named spelled out."""
    parts = [BOARD_FACTS, LANGUAGES]
    named = pins_mentioned(question)
    if named:
        lines = "\n".join(f"  - {pin.describe()}" for pin in named)
        parts.append(f"PINS NAMED IN THIS QUESTION:\n{lines}")
    return "\n\n".join(parts)
