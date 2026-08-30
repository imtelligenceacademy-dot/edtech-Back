"""Assembling the five layers into the block that goes into the prompt.

Order is an argument, not a formatting choice. The rule that a signal has no
meaning without a component comes first, before the model has read anything it
could over-generalise from. The general electronics and the board come next.
The kit roster comes after those, so by the time any specific module is named,
the reasoning it must be plugged into is already established. The retrieved
profiles come last, closest to the question, because they are the most specific
thing here and the last thing read is the thing answered from.

What is deliberately absent: any example, anywhere in the general layers, of
what a particular device does with a particular level. Every such statement in
this system is attached to a named component, because a worked example in a
general section is a rule waiting to be over-applied - which is the bug.
"""

from __future__ import annotations

from app.models import Lesson
from app.services import kits
from app.services.hardware import microbit, principles, render
from app.services.hardware.identify import Reading, identify
from app.services.hardware.components import in_kit

# The nine steps, as a procedure the model runs before answering. Written as an
# order of operations rather than as advice, because the failure being prevented
# is not ignorance of the components - it is answering from the code alone and
# never reaching them.
PROCEDURE = """HOW TO ANSWER A HARDWARE QUESTION - work through this before replying:
1. Which lesson is open, and which curriculum year and grade?
2. Which kit does that put on the teacher's desk?
3. Which component or components is the question about?
4. Which micro:bit pins are involved?
5. Is the code READING a pin or DRIVING one?
6. Is the signal digital, analog, PWM, or a protocol such as I2C?
7. What does the verified profile for that component say the levels or values physically mean?
8. Does anything in the lesson contradict that profile?
9. Only then answer.

If step 3 has no answer - the component is not in the kit, or not named in the profiles below - say which component you would need identified, and answer only the part that holds regardless. Do not substitute a similar module."""

_KIT_HEADER = """KIT ROSTER - every module in this kit, with the one fact most often got wrong. Anything marked [unconfirmed] or NOT VERIFIED must not be asserted:
{roster}"""

_PROFILE_HEADER = """VERIFIED HARDWARE PROFILES for the components this question appears to involve. These outrank the lesson material on electrical behaviour:"""

_NO_PROFILE = """No component in this kit matched the question closely enough to attach its profile. Use the kit roster above for polarity and direction, and say which module you would need named to be more specific. Do not fall back on a general rule about what 0 and 1 mean."""


def _hints(reading: Reading) -> str:
    """What the question looked like it was about, offered as a reading and
    labelled as one - a wrong hint that the model can overrule costs nothing,
    while a wrong hint stated as fact would be another confident error."""
    bits = [f"- Signal direction: {reading.direction}."]
    if reading.signals:
        bits.append(f"- Signal types mentioned: {', '.join(reading.signals)}.")
    if reading.pins:
        bits.append("- Pins named: " + ", ".join(p.name for p in reading.pins) + ".")
    if reading.components:
        bits.append(
            "- Components matched: "
            + ", ".join(c.name for c in reading.components)
            + "."
        )
    return (
        "WHAT THIS QUESTION LOOKS LIKE - a first reading of it, not a finding. "
        "Correct it from the question itself if it is wrong:\n" + "\n".join(bits)
    )


def hardware_note(
    lesson: Lesson | None,
    *,
    question: str,
    lesson_text: str = "",
    slide_reading: str = "",
) -> str:
    """The hardware-reasoning section of the teacher prompt.

    Always returns the general layers, even with no lesson open and no kit
    known: the rule about 0 and 1 is not a fact about this curriculum, and a
    teacher who asks the question with an ICT Fair project open deserves the
    same answer as one who asks it in Grade 7.
    """
    parts = [
        principles.CORE_RULE,
        PROCEDURE,
        principles.GENERAL_ELECTRONICS,
        microbit.board_note(question),
        principles.SOURCE_PRIORITY,
        principles.CONFLICT,
        principles.UNKNOWNS,
        principles.SAFETY,
    ]

    kit = kits.kit_for(lesson)
    if kit is None:
        return "\n\n".join(parts)

    components = in_kit(kit.model)
    if components:
        parts.append(_KIT_HEADER.format(roster=render.roster(components)))

    reading = identify(
        kit_model=kit.model,
        question=question,
        slide=slide_reading,
        title=lesson.title if lesson is not None else "",
        lesson=lesson_text,
    )
    if reading.components:
        profiles = "\n\n".join(render.profile(c) for c in reading.components)
        parts.append(f"{_PROFILE_HEADER}\n\n{profiles}")
    elif components:
        parts.append(_NO_PROFILE)

    parts.append(_hints(reading))
    return "\n\n".join(parts)
