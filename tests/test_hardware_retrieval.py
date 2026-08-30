"""Finding the right module from a question that rarely names it.

The teacher types "what does 0 mean in this block?" and means the module on the
slide in front of them. Everything the search has to work with is the question,
the slide reading, the lesson title and the lesson text - and the last of those
mentions half the kit, which is why it is weighted almost to nothing.

The failure mode worth guarding is not "found nothing". It is "found something
plausible and wrong": pulling the Red LED profile into an RGB question would
hand the model an active-high explanation of an active-low part, which is the
original bug wearing a different hat. Hence the specificity tests.
"""

from __future__ import annotations

from app.models import Lesson
from app.services.hardware import hardware_note
from app.services.hardware.identify import THRESHOLD, identify


def _lesson(*, year: int = 2, grade: int = 7, title: str = "Lesson") -> Lesson:
    return Lesson(id="les", title=title, grade=grade, subject="STEAM", year=year, language="en")


def _names(**kw) -> list[str]:
    return [c.name for c in identify(**kw).components]


# --------------------------------------------------------------------------- #
# The case that started it.
# --------------------------------------------------------------------------- #
def test_the_grade_7_question_finds_the_rgb_module():
    """The teacher names no component. The slide does."""
    names = _names(
        kit_model="KS4010",
        question="What does 0 mean in this block?",
        slide="digital write pin P0 to 0 ... RGB LED module, R to P0, G to P1, B to P2",
        title="Grade 7 Lesson 1",
    )
    assert names[0] == "RGB LED Module"


def test_the_specific_alias_beats_the_generic_one():
    """"RGB LED module" must outrank "Red LED Module" on an RGB question.

    Both answer to "led". If the generic term won, the model would get an
    active-high profile for an active-low part - the original wrong answer,
    arrived at by a different route.
    """
    reading = identify(kit_model="KS4010", question="how do I use the RGB LED module?")
    assert reading.components[0].name == "RGB LED Module"
    scores = reading.scores
    assert scores["ks4010-rgb-led"] > scores.get("ks4010-red-led", 0)


def test_a_kit_scopes_the_search_before_it_ranks_it():
    """A Year 1 teacher cannot be shown a Year 2 module, however well it
    matches - they do not own it."""
    year_one = _names(kit_model="KS4011", question="tell me about the RGB module")
    assert year_one == ["5050 RGB Module"]

    year_two = _names(kit_model="KS4010", question="tell me about the RGB module")
    assert year_two == ["RGB LED Module"]


def test_no_kit_means_no_components():
    assert _names(kit_model=None, question="tell me about the RGB module") == []


# --------------------------------------------------------------------------- #
# Not finding something is a valid outcome, and must not be papered over.
# --------------------------------------------------------------------------- #
def test_a_general_question_matches_nothing_and_says_so():
    reading = identify(
        kit_model="KS4010", question="Does digital 1 mean ON and digital 0 mean OFF?"
    )
    assert reading.components == ()

    text = hardware_note(
        _lesson(), question="Does digital 1 mean ON and digital 0 mean OFF?"
    )
    assert "No component in this kit matched the question closely enough" in text
    assert "Do not fall back on a general rule about what 0 and 1 mean" in text


def test_an_incidental_mention_does_not_reach_the_threshold():
    """One passing word in a 20,000-character deck is not what the question is
    about. Lesson text is a tie-breaker, never a vote on its own."""
    reading = identify(
        kit_model="KS4010",
        question="how long should the lesson take?",
        lesson="... the relay module is introduced later in the year ...",
    )
    assert all(score < THRESHOLD for score in reading.scores.values()) or not reading.components


# --------------------------------------------------------------------------- #
# The other four things the engine works out.
# --------------------------------------------------------------------------- #
def test_named_pins_are_picked_up_with_their_real_constraints():
    text = hardware_note(_lesson(), question="Can I connect this sensor to P5?")
    assert "PINS NAMED IN THIS QUESTION" in text
    assert "P5: digital in/out, PWM out - SHARED WITH button A" in text


def test_a_pin_that_shares_the_led_display_is_flagged():
    text = hardware_note(_lesson(), question="is P3 free?")
    assert "SHARED WITH the 5x5 LED display" in text


def test_writing_and_reading_are_told_apart():
    assert identify(kit_model="KS4010", question="digital write pin P0 to 1").writing
    assert identify(kit_model="KS4010", question="analog read pin P0").reading
    both = identify(kit_model="KS4010", question="I write to P0 then read P1 to detect it")
    assert both.direction == "both driving a pin and reading one"


def test_micropython_syntax_is_recognised_as_well_as_makecode():
    assert identify(kit_model="KS4010", question="pin0.write_digital(1)").writing
    assert identify(kit_model="KS4010", question="pin0.read_analog()").reading


def test_the_signal_type_is_noticed():
    assert "PWM" in identify(kit_model="KS4010", question="what does duty cycle mean?").signals
    assert "I2C" in identify(kit_model="KS4010", question="which pins are SCL and SDA?").signals


def test_the_reading_is_offered_as_a_reading_not_a_finding():
    """A wrong hint the model can overrule costs nothing. A wrong hint stated
    as fact would be one more confident error."""
    text = hardware_note(_lesson(), question="digital write pin P0 to 1 on the relay")
    assert "a first reading of it, not a finding" in text
    assert "Correct it from the question itself if it is wrong" in text


# --------------------------------------------------------------------------- #
# How much of the prompt this costs.
# --------------------------------------------------------------------------- #
def test_at_most_three_profiles_are_attached():
    """Profiles are long and the lesson text still has to fit."""
    reading = identify(
        kit_model="KS4010",
        question="compare the LED module, the RGB LED module, the buzzer, the relay, the servo and the fan",
    )
    assert len(reading.components) <= 3
    # ...but everything that matched is still reported, so the cap is visible.
    assert len(reading.scores) > 3


def test_the_roster_covers_what_the_search_misses():
    """The safety net. Even with no profile attached, every module in the kit
    has already contributed its polarity or an explicit "not verified"."""
    text = hardware_note(_lesson(), question="what is a logic level?")
    assert "RGB LED Module - common-anode; active-low" in text
    assert "Infrared Obstacle Detector Sensor - active-low" in text
    assert "Line Tracking Sensor - active level NOT VERIFIED" in text


def test_a_named_component_outranks_the_one_on_the_slide():
    """The teacher is looking at an RGB slide and asks about the relay.

    Ranking on total score alone put the RGB module first, because the slide
    and the title both shouted it - and the model would then have answered a
    relay question with an active-low profile in front of it. What the teacher
    typed wins; the slide stays attached behind it as context.
    """
    names = _names(
        kit_model="KS4010",
        question="Why does the relay turn on when the signal becomes HIGH?",
        slide="RGB LED module wired R to P0, G to P1, B to P2",
        title="Grade 7 Lesson 1 - RGB LED",
    )
    assert names[0] == "Single Relay Module"
    assert "RGB LED Module" in names


def test_the_slide_still_answers_when_the_question_names_nothing():
    """"What does 0 mean in this block?" names no component at all. The slide
    is the only thing that knows, and it is enough."""
    names = _names(
        kit_model="KS4010",
        question="What does 0 mean in this block?",
        slide="digital write pin P0 to 0 ... RGB LED module",
        title="Grade 7 Lesson 1",
    )
    assert names == ["RGB LED Module"]


def test_a_generic_alias_does_not_belong_to_one_component():
    """"led module" is inside "RGB LED Module", "White LED Module" and "3W LED
    Module". As an alias for the Red LED it pulled that profile into every RGB
    question - an active-high explanation of an active-low part."""
    names = _names(
        kit_model="KS4010",
        question="how do I use the RGB LED module?",
        slide="RGB LED module",
    )
    assert "Red LED Module" not in names
