"""Turning a component profile into the text the model actually reads.

Kept apart from the schema so the data stays data. The ordering here is the
argument: identity first, then what HIGH and LOW physically do on this exact
board, then wiring, then code, then teaching, and confidence last so it is the
thing most recently read before the model answers.

The one rule that shapes every branch below: an unknown is printed, never
skipped. A silently omitted polarity line looks identical to a component with no
polarity, and a model filling that silence produces exactly the confident wrong
answer this whole system exists to stop.
"""

from __future__ import annotations

from app.services.hardware.schema import (
    Category,
    Component,
    Direction,
    LedCommon,
    Polarity,
    Verified,
)


def _lines(*pairs: tuple[str, str]) -> list[str]:
    """Label/value lines, dropping only the genuinely empty ones."""
    return [f"    {label}: {value}" for label, value in pairs if value]


def _identity(c: Component) -> list[str]:
    out = [f"  IDENTITY: {c.manufacturer} {c.name}" + (f" ({c.model})" if c.model else "")]
    out += _lines(
        ("In kits", ", ".join(c.kits)),
        ("Category", c.category.value),
        ("What it is", c.summary),
        ("Also called", ", ".join(c.aliases)),
        ("Documentation", c.doc_url),
    )
    return out


def _electrical(c: Component) -> list[str]:
    el = c.electrical
    out = ["  ELECTRICAL:"]
    out += _lines(
        ("Operating voltage", el.operating_voltage),
        ("Recommended", el.recommended_voltage),
        ("Pins", "; ".join(el.pins)),
        ("Signal channels", str(el.signal_channels) if el.signal_channels else ""),
        ("Ground", "Required" if el.needs_ground else "Not required"),
        ("Current", el.current_notes),
    )
    # Polarity is stated even when it is unknown, and especially then.
    if el.polarity is Polarity.unknown:
        out.append("    Active level: NOT VERIFIED for this module - do not state one")
    elif el.polarity is not Polarity.not_applicable:
        out.append(f"    Active level: {el.polarity.value}")
    if el.led_common not in (LedCommon.not_applicable,):
        out.append(f"    LED wiring: {el.led_common.value}")
    out += _lines(
        ("Pull resistor", el.pull_resistor),
        ("On-board resistor", el.onboard_resistor),
        ("On-board trimmer", el.potentiometer),
    )
    return out


def _digital(c: Component) -> list[str]:
    d = c.digital
    if d is None:
        return []
    return ["  WHAT HIGH AND LOW DO ON THIS MODULE:"] + _lines(
        ("HIGH (1) means", d.high_means),
        ("LOW (0) means", d.low_means),
        ("Active state", d.active_state),
        ("Inactive state", d.inactive_state),
        ("At rest", d.resting_state),
        ("When triggered", d.triggered_state),
    )


def _analog(c: Component) -> list[str]:
    a = c.analog
    if a is None:
        return []
    out = ["  ANALOG BEHAVIOUR:"] + _lines(
        ("Raw range", a.raw_range),
        ("Minimum", a.minimum),
        ("Maximum", a.maximum),
    )
    if a.direction is Direction.unknown:
        out.append(
            "    Direction: NOT VERIFIED - do not claim a higher reading means "
            "more or less of the measured quantity"
        )
    elif a.direction is not Direction.not_applicable:
        out.append(f"    Direction: {a.direction.value}")
    out += _lines(
        ("Typical values", a.typical_values),
        ("Threshold", a.threshold),
        ("Calibration", a.calibration),
        ("Environment", a.environment),
    )
    return out


def _pwm(c: Component) -> list[str]:
    p = c.pwm
    if p is None:
        return []
    return ["  PWM BEHAVIOUR:"] + _lines(
        ("Range", p.value_range),
        ("Increasing the value", p.increasing_duty),
        ("Frequency", p.frequency),
        ("Useful range", p.useful_range),
    )


def _microbit(c: Component) -> list[str]:
    m = c.microbit
    out = _lines(
        ("Suggested pins", m.suggested_pins),
        ("Avoid", m.avoid_pins),
        ("Pin conflicts", m.conflicts),
        ("V1 / V2", m.version_notes),
        ("Wiring", m.wiring),
    )
    return ["  ON THE MICRO:BIT:"] + out if out else []


def _code(c: Component) -> list[str]:
    out: list[str] = []
    for label, notes in (("MAKECODE", c.makecode), ("MICROPYTHON", c.micropython)):
        block = _lines(
            ("Extension", notes.extension),
            ("Setup", notes.setup),
            ("Read", notes.read),
            ("Write", notes.write),
            ("Note", notes.notes),
        )
        if block:
            out += [f"  {label}:"] + block
    return out


def _teaching(c: Component) -> list[str]:
    t = c.teaching
    out = _lines(("Explain it as", t.beginner))
    if t.mistakes:
        out += ["    Common mistakes:"] + [f"      - {m}" for m in t.mistakes]
    if t.troubleshooting:
        out += ["    Troubleshooting:"] + [f"      - {m}" for m in t.troubleshooting]
    out += _lines(("Vocabulary", ", ".join(t.terms)), ("Safety", t.safety))
    return ["  TEACHING:"] + out if out else []


def _verification(c: Component) -> list[str]:
    v = c.verification
    out = [f"  CONFIDENCE: {v.status.value}"]
    out += _lines(
        ("Source", v.source),
        ("Reference", v.source_url),
        ("Last checked", v.last_verified),
        ("Note", v.notes),
    )
    if v.status is Verified.needs_verification:
        out.append(
            "    HOW TO ANSWER: say plainly that this detail has not been "
            "verified for this module, give the reasoning that does hold, and "
            "offer the classroom check above. Do not state a polarity or a "
            "direction as fact."
        )
    elif v.status is Verified.general_knowledge:
        out.append(
            "    HOW TO ANSWER: usual for this type of part, not confirmed "
            "against this exact board. Say so once, briefly, rather than "
            "presenting it as certain."
        )
    return out


def profile(c: Component) -> str:
    """The full text profile for one component."""
    blocks = [
        _identity(c),
        _electrical(c),
        _digital(c),
        _analog(c),
        _pwm(c),
        _microbit(c),
        _code(c),
        _teaching(c),
        _verification(c),
    ]
    body = "\n".join("\n".join(block) for block in blocks if block)
    return f"COMPONENT: {c.name} [{c.id}]\n{body}"


def roster(components: tuple[Component, ...]) -> str:
    """One line per component - the safety net under the search.

    Retrieval can miss. When it does, this is what stops the model falling back
    on a general rule: even unretrieved, every module in the kit has already
    contributed either its real polarity or an explicit "not verified".
    """
    lines = [f"  - {c.headline}" for c in components if c.category is not Category.passive]
    return "\n".join(lines)
