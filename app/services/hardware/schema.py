"""The shape of a piece of hardware.

Everything the assistant is allowed to say about a physical component is stored
in one of these. The point of a schema rather than prose in a prompt is that a
field can be *absent*: a component whose triggered polarity nobody has confirmed
says so, in those words, instead of being described by a model that has to say
something.

Two rules are baked into the types themselves.

Polarity is a property of the component, never of the code. There is no place in
this schema to write "1 means on" - the nearest thing is `Digital.high_means`,
which belongs to one specific module and is only ever rendered under that
module's name. A model reading the assembled prompt cannot pick up a global 0/1
rule from it, because none is ever written down.

Not-known is a value. `UNKNOWN` and `NOT_APPLICABLE` are different things and
both render, so "nobody has checked" never gets flattened into "does not apply",
which is how a hole in the data turns into a confident wrong answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Absent for two different reasons, and the difference matters to a teacher:
# "this sensor has no digital output" is an answer, "nobody has checked whether
# it is active-high" is a warning.
UNKNOWN = "unknown"
NOT_APPLICABLE = "not_applicable"


class Category(str, Enum):
    """What the thing is for. Drives grouping in the roster, and nothing else."""

    board = "board"
    breakout = "breakout board"
    light = "light output"
    sound = "sound output"
    motion = "motion output"
    switching = "switching output"
    display = "display"
    input_control = "manual input"
    sensor_environment = "environment sensor"
    sensor_light = "light sensor"
    sensor_motion = "motion / proximity sensor"
    sensor_magnetic = "magnetic sensor"
    sensor_sound = "sound sensor"
    sensor_touch = "touch / contact sensor"
    sensor_gas = "gas sensor"
    communication = "communication"
    passive = "non-electronic part"


class Polarity(str, Enum):
    """Which electrical level makes the component do its thing.

    `unknown` is not a gap in the schema - it is the honest state for a module
    whose datasheet nobody has read, and it renders as a refusal to guess rather
    than as a blank.
    """

    active_high = "active-high"
    active_low = "active-low"
    not_applicable = NOT_APPLICABLE
    unknown = UNKNOWN


class LedCommon(str, Enum):
    """How a multi-colour LED is wired internally - the whole reason a colour
    channel is active-low on one module and active-high on the next."""

    anode = "common-anode"
    cathode = "common-cathode"
    addressable = "addressable (serial data, not one pin per colour)"
    single = "single-colour LED"
    not_applicable = NOT_APPLICABLE
    unknown = UNKNOWN


class Signal(str, Enum):
    """How the micro:bit talks to it."""

    digital_out = "digital output"
    digital_in = "digital input"
    analog_in = "analog input"
    analog_out = "analog output"
    pwm = "PWM"
    i2c = "I2C"
    spi = "SPI"
    uart = "UART"
    one_wire = "1-Wire"
    none = "no electrical connection"


class Direction(str, Enum):
    """Which way an analog reading moves as the measured quantity increases.

    Assuming this is the single most common way an otherwise correct answer
    about a sensor turns out to be backwards.
    """

    more_is_higher = "more of the measured quantity gives a HIGHER number"
    more_is_lower = "more of the measured quantity gives a LOWER number"
    # For sensors that report a real quantity over a protocol. There is no
    # direction to get wrong, and saying "NOT VERIFIED" about one would be a
    # warning with nothing behind it.
    not_applicable = NOT_APPLICABLE
    unknown = UNKNOWN

    @property
    def short(self) -> str:
        """The roster form. The full sentence is right in a profile and too
        long forty times over in a one-line-per-module list."""
        if self is Direction.more_is_higher:
            return "more = higher reading"
        if self is Direction.more_is_lower:
            return "more = lower reading"
        if self is Direction.not_applicable:
            return ""
        return "reading direction NOT VERIFIED"


class Verified(str, Enum):
    """Where a profile's facts came from, ranked in the order they are trusted.

    A lesson PDF is teaching material, not a datasheet: when it disagrees with
    the manufacturer the manufacturer wins and the lesson gets flagged.
    `needs_verification` is a live to-do list - anything carrying it is reported
    to the teacher as unconfirmed rather than asserted.
    """

    manufacturer_doc = "manufacturer documentation for this exact module"
    microbit_doc = "official micro:bit documentation"
    imt_verified = "IM-Telligence verified on the physical hardware"
    general_knowledge = (
        "general knowledge of this part type, NOT confirmed for this exact module"
    )
    needs_verification = "NOT VERIFIED - must not be stated as fact"


@dataclass(frozen=True)
class Verification:
    status: Verified = Verified.needs_verification
    source: str = ""
    source_url: str = ""
    last_verified: str = ""  # ISO date, or "" when never
    notes: str = ""

    @property
    def trustworthy(self) -> bool:
        return self.status in (
            Verified.manufacturer_doc,
            Verified.microbit_doc,
            Verified.imt_verified,
        )


@dataclass(frozen=True)
class Electrical:
    operating_voltage: str = UNKNOWN
    recommended_voltage: str = ""
    # Pin labels as they are silkscreened on the module, because that is what
    # the teacher is looking at while they ask.
    pins: tuple[str, ...] = ()
    signal_channels: int | None = None
    needs_ground: bool = True
    current_notes: str = ""
    polarity: Polarity = Polarity.unknown
    led_common: LedCommon = LedCommon.not_applicable
    pull_resistor: str = ""
    onboard_resistor: str = ""
    # What an on-board trimmer actually adjusts - almost never the sensor's own
    # measuring range, which is what teachers are usually told it does.
    potentiometer: str = ""


@dataclass(frozen=True)
class Digital:
    """What HIGH and LOW physically do on this module, and nothing more general
    than that. Both strings are written from the component outward."""

    high_means: str = UNKNOWN
    low_means: str = UNKNOWN
    active_state: str = UNKNOWN
    inactive_state: str = UNKNOWN
    resting_state: str = ""
    triggered_state: str = ""


@dataclass(frozen=True)
class Analog:
    raw_range: str = UNKNOWN
    minimum: str = ""
    maximum: str = ""
    direction: Direction = Direction.unknown
    typical_values: str = ""
    threshold: str = ""
    calibration: str = ""
    environment: str = ""


@dataclass(frozen=True)
class Pwm:
    value_range: str = UNKNOWN
    increasing_duty: str = UNKNOWN
    frequency: str = ""
    useful_range: str = ""


@dataclass(frozen=True)
class MicrobitNotes:
    suggested_pins: str = ""
    avoid_pins: str = ""
    conflicts: str = ""
    version_notes: str = ""
    wiring: str = ""


@dataclass(frozen=True)
class CodeNotes:
    """MakeCode and MicroPython answer the same question in two languages.

    Neither carries its own account of what a value physically means - that
    lives once, in Digital/Analog/Pwm - so the two environments cannot drift
    into explaining the same circuit differently.
    """

    extension: str = ""
    setup: str = ""
    read: str = ""
    write: str = ""
    notes: str = ""


@dataclass(frozen=True)
class Teaching:
    beginner: str = ""
    mistakes: tuple[str, ...] = ()
    troubleshooting: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()
    safety: str = ""


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    kits: tuple[str, ...]
    category: Category
    summary: str
    manufacturer: str = "Keyestudio"
    model: str = ""
    # What a teacher might actually type. Matched longest-first, so "rgb led
    # module" beats "led" and the whole roster is not dragged in by one word.
    aliases: tuple[str, ...] = ()
    doc_url: str = ""
    image_url: str = ""
    signals: tuple[Signal, ...] = ()
    electrical: Electrical = field(default_factory=Electrical)
    digital: Digital | None = None
    analog: Analog | None = None
    pwm: Pwm | None = None
    microbit: MicrobitNotes = field(default_factory=MicrobitNotes)
    makecode: CodeNotes = field(default_factory=CodeNotes)
    micropython: CodeNotes = field(default_factory=CodeNotes)
    teaching: Teaching = field(default_factory=Teaching)
    verification: Verification = field(default_factory=Verification)

    def in_kit(self, kit_model: str | None) -> bool:
        return kit_model is not None and kit_model in self.kits

    @property
    def match_terms(self) -> tuple[str, ...]:
        """Everything this component answers to, longest first."""
        terms = {self.name.lower(), *(a.lower() for a in self.aliases)}
        if self.model:
            terms.add(self.model.lower())
        return tuple(sorted(terms, key=len, reverse=True))

    @property
    def unverified(self) -> bool:
        return self.verification.status in (
            Verified.needs_verification,
            Verified.general_knowledge,
        )

    @property
    def headline(self) -> str:
        """The one line this component gets in the kit roster.

        Polarity leads, because it is the fact that was being got wrong, and it
        carries its own confidence - so even a component the search missed
        contributes either a usable fact or an explicit "not verified".
        """
        bits: list[str] = []
        el = self.electrical
        if el.led_common not in (LedCommon.not_applicable, LedCommon.unknown):
            bits.append(el.led_common.value)
        if el.polarity is Polarity.unknown:
            bits.append("active level NOT VERIFIED")
        elif el.polarity is not Polarity.not_applicable:
            bits.append(el.polarity.value)
        if self.analog is not None and self.analog.direction.short:
            bits.append(self.analog.direction.short)
        if not bits:
            # Parts with no polarity and no analog reading - a shield, a servo,
            # a buzzer. What they ARE is the useful line; "no electrical
            # connection" for a sensor shield is technically true and useless.
            signals = [s.value for s in self.signals if s is not Signal.none]
            bits.append(", ".join(signals) or self.category.value)
        suffix = " [unconfirmed]" if self.unverified else ""
        return f"{self.name} - {'; '.join(bits)}{suffix}"
