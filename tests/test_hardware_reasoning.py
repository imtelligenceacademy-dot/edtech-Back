"""The hardware reasoning suite.

What these tests can and cannot check is worth being clear about. They do not
call a model - there is no key in CI, and an assertion about a generated
sentence would be flaky in a way that hides real regressions. What they check is
the contract the model is given: that the correct electrical facts for the
correct kit reach the prompt, that every unverified fact arrives labelled as
unverified, and above all that no global rule about 0 and 1 exists anywhere in
the system for a model to pick up.

That last one is the whole point. The bug was never that the assistant lacked a
fact; it was that it had a plausible general rule and applied it. A test suite
that only checked "the RGB answer is right now" would pass while the rule was
still sitting there waiting for the next module.
"""

from __future__ import annotations

import pytest

from app.models import Lesson
from app.services.hardware import hardware_note, profile
from app.services.hardware.components import get
from app.services.hardware.schema import (
    Category,
    Component,
    Digital,
    Electrical,
    LedCommon,
    Polarity,
    Verification,
    Verified,
)

# The sentences that must not exist as general statements anywhere the model
# reads. Quoted from the answer that started this.
GLOBAL_RULES = (
    "1 means on",
    "0 means off",
    "1 = on",
    "0 = off",
    "1 turns it on",
    "0 turns it off",
)


def lesson(*, year: int = 2, grade: int = 7, title: str = "Lesson") -> Lesson:
    return Lesson(id="les_x", title=title, grade=grade, subject="STEAM", year=year, language="en")


def note(question: str, *, year: int = 2, grade: int = 7, title: str = "Lesson", **kw) -> str:
    return hardware_note(lesson(year=year, grade=grade, title=title), question=question, **kw)


# --------------------------------------------------------------------------- #
# The regression. This exact question, and the rule it must not confirm.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("year,grade", [(1, 1), (1, 7), (2, 3), (2, 7), (2, 12)])
def test_digital_one_is_high_not_on(year, grade):
    """"Does digital 1 mean ON and digital 0 mean OFF on a micro:bit?"

    The answer is no, and it is no in every grade, in both years and with either
    kit on the desk - so the rule that makes it no is sent unconditionally,
    before any component is named.
    """
    text = note(
        "Does digital 1 mean ON and digital 0 mean OFF on a micro:bit?",
        year=year,
        grade=grade,
    )
    assert "logic levels, not physical outcomes" in text
    assert "0 is LOW (near 0 V) and 1 is HIGH (near 3.3 V)" in text
    assert "a property of the component wired to that pin, and of nothing else" in text
    # The two shortcuts are quoted as forbidden, not merely left unsaid.
    assert '"1 means on and 0 means off"' in text
    assert '"0 means on and 1 means off"' in text


def test_no_global_zero_one_rule_is_ever_stated():
    """The general layers must contain no worked example of what a level does.

    Any such example is a rule waiting to be over-applied. Component profiles
    may say what HIGH does - under a component's name, which is the difference.
    """
    text = note("what is a digital pin?")
    # The core rule quotes these very sentences in order to forbid them, so the
    # quoted list is dropped before scanning. Everything else is fair game.
    general = " ".join(
        line
        for line in text.split("KIT ROSTER")[0].splitlines()
        if not line.strip().startswith('- "')
    ).lower()
    for rule in GLOBAL_RULES:
        assert rule not in general, f"the general layers assert a global rule: {rule!r}"


def test_the_rule_survives_having_no_kit():
    """An ICT Fair project has no year and no grade, so no kit. The teacher
    still deserves the correct answer about 0 and 1."""
    text = hardware_note(None, question="what does 1 do on P0?")
    assert "logic levels, not physical outcomes" in text
    assert "KIT ROSTER" not in text


# --------------------------------------------------------------------------- #
# Outputs: the same value, opposite results, two modules in the same kit.
# --------------------------------------------------------------------------- #
def test_active_low_output_lights_on_low():
    text = profile(get("ks4010-rgb-led"))
    assert "Active level: active-low" in text
    assert "LED wiring: common-anode" in text
    assert "LOW (0) means: That colour channel is ON" in text
    assert "HIGH (1) means: That colour channel is OFF" in text


def test_active_high_output_lights_on_high():
    text = profile(get("ks4010-white-led"))
    assert "Active level: active-high" in text
    assert "HIGH (1) means: LED on" in text
    assert "LOW (0) means: LED off" in text


def test_both_polarities_ship_in_the_same_kit():
    """The reason a per-component profile is the only workable design: one kit
    contains an LED that lights on 1 and an LED that lights on 0."""
    rgb = get("ks4010-rgb-led").electrical.polarity
    white = get("ks4010-white-led").electrical.polarity
    assert rgb is Polarity.active_low
    assert white is Polarity.active_high


def test_both_kits_rgb_modules_are_common_anode():
    """Confirmed from Keyestudio's own project pages for each kit. Recorded
    because it is true of these two modules, not of RGB LEDs."""
    for cid in ("ks4010-rgb-led", "ks4011-5050-rgb"):
        component = get(cid)
        assert component.electrical.led_common is LedCommon.anode
        assert component.electrical.polarity is Polarity.active_low
        assert component.verification.status is Verified.manufacturer_doc


# --------------------------------------------------------------------------- #
# Inputs: HIGH is "triggered" on one sensor and "idle" on the next.
# --------------------------------------------------------------------------- #
def test_active_high_input_reads_high_when_triggered():
    text = profile(get("ks4010-pir"))
    assert "Active level: active-high" in text
    assert "HIGH (1) means: Motion detected" in text
    assert "LOW (0) means: No motion" in text


def test_active_low_input_reads_low_when_triggered():
    text = profile(get("ks4010-ir-obstacle"))
    assert "Active level: active-low" in text
    assert "LOW (0) means: An obstacle is reflecting infrared back" in text
    assert "HIGH (1) means: Nothing detected" in text


def test_the_two_sensor_polarities_are_in_one_kit_too():
    assert get("ks4010-pir").electrical.polarity is Polarity.active_high
    assert get("ks4010-ir-obstacle").electrical.polarity is Polarity.active_low


# --------------------------------------------------------------------------- #
# Analog: a bigger number is not automatically more of anything.
# --------------------------------------------------------------------------- #
def test_analog_direction_is_per_module_and_may_be_unknown():
    """The photocell's direction genuinely depends on which half of its divider
    is read, and Keyestudio does not say. It must not be guessed."""
    text = profile(get("ks4010-photocell"))
    assert "Direction: NOT VERIFIED" in text
    assert "do not claim a higher reading means" in text
    # An unknown that comes with a way to resolve it is worth far more than one
    # that only refuses.
    assert "cup your hand over it" in text


def test_a_known_analog_direction_is_stated_plainly():
    text = profile(get("ks4010-water-level"))
    assert "Direction: more of the measured quantity gives a HIGHER number" in text


def test_the_general_layer_never_claims_a_direction():
    text = note("does a larger reading mean more light?")
    general = text.split("KIT ROSTER")[0]
    assert "depends on how the sensing element is wired into its divider" in general
    assert "it is a property of the module" in general


# --------------------------------------------------------------------------- #
# Buzzers: same word, incompatible parts.
# --------------------------------------------------------------------------- #
def test_active_and_passive_buzzers_are_separate_components():
    active = get("ks4010-active-buzzer")
    passive = get("ks4010-passive-buzzer")
    assert active.id != passive.id
    assert active.electrical.polarity is Polarity.active_high
    assert passive.electrical.polarity is Polarity.not_applicable


def test_the_active_buzzer_is_explained_by_its_internal_oscillator():
    text = profile(get("ks4010-active-buzzer"))
    assert "built-in frequency" in text or "own built-in frequency" in text
    assert "cannot change the pitch" in text
    assert "Do NOT use the music blocks" in text


def test_the_passive_buzzer_needs_a_changing_signal():
    text = profile(get("ks4010-passive-buzzer"))
    assert "No single level activates it" in text
    assert "requires the signal to keep changing" in text
    assert "The frequency carries the note" in text or "Frequency carries the note" in text


def test_a_buzzer_question_retrieves_both_so_they_can_be_told_apart():
    text = note("Is this a passive or active buzzer?", title="Sound")
    assert "COMPONENT: Active Buzzer Module" in text
    assert "COMPONENT: Passive Buzzer Module" in text


# --------------------------------------------------------------------------- #
# PWM: a duty cycle, and one whose direction the component can invert.
# --------------------------------------------------------------------------- #
def test_pwm_is_a_duty_cycle_not_an_on_off():
    text = note("what does analog write 512 do?")
    assert "PWM does not produce an in-between voltage" in text
    assert "duty cycle is the fraction of each period spent HIGH" in text
    assert "depends on what is being driven" in text


def test_pwm_direction_follows_the_component_not_the_number():
    """Bigger is brighter on the white LED and dimmer on the RGB module,
    because one is active-high and the other active-low. Same board, same
    block, opposite results."""
    assert "Brighter" in profile(get("ks4010-white-led"))
    rgb = profile(get("ks4010-rgb-led"))
    assert "make that channel DIMMER" in rgb
    assert "0 is full brightness, 1023 is dark" in rgb


def test_a_servo_is_not_explained_as_a_duty_percentage():
    text = profile(get("ks4010-servo"))
    assert "longer pulse means a larger angle" in text
    assert "measures the pulse width" in text
    assert "About 50 Hz" in text


# --------------------------------------------------------------------------- #
# The two programming environments.
# --------------------------------------------------------------------------- #
def test_makecode_and_micropython_get_the_same_electrical_explanation():
    text = note("what is the MicroPython equivalent of digital write pin P0 to 0?")
    assert "the language changes, the electronics do not" in text
    assert "Explain the physical result identically in both" in text
    assert "keep the electrical explanation word for word the same" in text


def test_both_environments_are_shown_for_the_module_in_question():
    text = profile(get("ks4010-rgb-led"))
    assert "MAKECODE:" in text and "MICROPYTHON:" in text
    # And neither restates the polarity differently from the other.
    assert "pins.digitalWritePin(DigitalPin.P0, 0) lights that channel" in text
    assert "pin0.write_digital(0) lights that channel" in text


# --------------------------------------------------------------------------- #
# Unknowns.
# --------------------------------------------------------------------------- #
def test_an_unverified_module_says_so_instead_of_guessing():
    text = profile(get("ks4010-line-tracking"))
    assert "Active level: NOT VERIFIED for this module - do not state one" in text
    assert "HOW TO ANSWER: say plainly that this detail has not been verified" in text
    assert "Do not state a polarity or a direction as fact" in text


def test_general_knowledge_is_flagged_as_not_confirmed_for_this_board():
    text = profile(get("ks4010-ir-obstacle"))
    assert "NOT confirmed for this exact module" in text
    assert "rather than presenting it as certain" in text


def test_the_policy_says_what_to_do_with_a_gap():
    text = note("is this sensor active high or active low?")
    assert "Never fill a gap with the behaviour of a similar-sounding part" in text
    assert "Two modules with the same name can be electrically opposite" in text
    assert "Inventing a specification is worse than any amount of hedging" in text


# --------------------------------------------------------------------------- #
# Polarity is rendered from the data, so a future module gets it for free.
# These use components that do not exist in either kit, on purpose: they check
# the mechanism rather than the catalogue.
# --------------------------------------------------------------------------- #
def _rgb(common: LedCommon, polarity: Polarity, high: str, low: str) -> Component:
    return Component(
        id="test-rgb",
        name="Test RGB Module",
        kits=("KSTEST",),
        category=Category.light,
        summary="A hypothetical module, used to check the renderer.",
        electrical=Electrical(polarity=polarity, led_common=common),
        digital=Digital(high_means=high, low_means=low),
        verification=Verification(status=Verified.imt_verified),
    )


def test_a_common_cathode_module_would_render_as_active_high():
    """Neither current kit ships one, which is exactly why this is tested from
    the schema: the day a common-cathode module is added, the opposite
    explanation has to come out without touching a prompt."""
    text = profile(
        _rgb(LedCommon.cathode, Polarity.active_high, "Channel ON.", "Channel OFF.")
    )
    assert "LED wiring: common-cathode" in text
    assert "Active level: active-high" in text
    assert "HIGH (1) means: Channel ON." in text


def test_an_addressable_module_would_not_render_as_one_pin_per_colour():
    text = profile(
        _rgb(
            LedCommon.addressable,
            Polarity.not_applicable,
            "Part of a serial data frame, not a colour.",
            "Part of a serial data frame, not a colour.",
        )
    )
    assert "not one pin per colour" in text
    # not_applicable polarity says nothing rather than inventing a level.
    assert "Active level:" not in text


# --------------------------------------------------------------------------- #
# The kits stay apart.
# --------------------------------------------------------------------------- #
def test_year_one_and_year_two_get_different_hardware():
    honeycomb = note("what modules do I have?", year=1, grade=3)
    sensor_kit = note("what modules do I have?", year=2, grade=9)

    assert "5050 RGB Module" in honeycomb
    assert "5050 RGB Module" not in sensor_kit
    assert "micro:bit Sensor Shield V2" in sensor_kit
    assert "micro:bit Sensor Shield V2" not in honeycomb


def test_a_year_one_teacher_is_never_shown_the_year_two_buzzer():
    """Year 1 has only a passive buzzer. Offering the active one would be
    offering hardware the school does not own."""
    honeycomb = note("tell me about the buzzer", year=1, grade=3)
    assert "Active Buzzer Module" not in honeycomb
    assert "Passive Buzzer" in honeycomb


# --------------------------------------------------------------------------- #
# End to end: does any of this actually reach the model?
# --------------------------------------------------------------------------- #
def test_the_hardware_block_reaches_the_assembled_prompt(db):
    """Everything above tests the block. This tests that it is sent.

    It also pins the ORDER, which is not cosmetic: the rule has to be read
    before any component, and the components have to be the last thing read
    before the question.
    """
    from app.models import LessonAssignment, School, User
    from app.models.enums import Role, UserStatus
    from app.routers import ai
    from app.schemas.ai import AIChatRequest
    from app.utils import new_id

    school = School(id=new_id("sch"), name="S", program_year=2)
    db.add(school)
    teacher = User(
        id=new_id("u"),
        name="T",
        email=f"{new_id('t')}@x.com",
        password_hash="x",
        role=Role.teacher,
        status=UserStatus.active,
        school_id=school.id,
        grades=["G7"],
        language="en",
    )
    db.add(teacher)
    db.flush()
    les = Lesson(
        id=new_id("les"),
        title="Grade 7 Lesson 1 - RGB LED",
        grade=7,
        subject="STEAM",
        language="en",
        year=2,
        course="python",
        lesson_no=1,
        created_by=teacher.id,
    )
    db.add(les)
    db.flush()
    db.add(
        LessonAssignment(
            id=new_id("la"), lesson_id=les.id, teacher_id=teacher.id, source="rule"
        )
    )
    db.commit()

    bundle = ai._build_prompt(
        db,
        teacher,
        AIChatRequest(
            message="What does digital write pin P0 to 0 do to the RGB LED?",
            lesson_id=les.id,
        ),
    )
    system = bundle.system

    assert "logic levels, not physical outcomes" in system
    assert "COMPONENT: RGB LED Module" in system
    assert "KIT ROSTER" in system

    # The kit says what is on the desk; the hardware block says what it does.
    assert system.index("CLASSROOM HARDWARE") < system.index("SIGNAL REASONING")
    # The rule precedes every component fact...
    assert system.index("SIGNAL REASONING") < system.index("KIT ROSTER")
    assert system.index("KIT ROSTER") < system.index("COMPONENT: RGB LED Module")
    # ...and the lesson itself still comes last, as it always did.
    assert system.index("COMPONENT: RGB LED Module") < system.index("The open lesson is")
