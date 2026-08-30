"""Working out which piece of hardware a question is actually about.

There is no vector store in this project and this does not need one. The search
space is one kit - a few dozen modules with known names, aliases and model
numbers - and the teacher is looking at the slide while they type. Matching
names against the question, the slide reading and the lesson beats embeddings
here on every axis that matters: it is exact, it costs nothing per request, it
needs no build step, and when it picks the wrong module you can see why.

Two things make it work rather than merely run:

Scope before rank. Only components in the kit for this lesson are candidates. A
Year 2 teacher asking about "the buzzer" cannot be shown the Year 1 buzzer.

Specific terms outrank vague ones. "rgb led module" is worth more than "led",
so a question about the RGB module does not drag in every LED in the kit. Terms
are weighted by how many words they contain and where they were found - the
question counts for most, the whole lesson text for least, because a lesson
mentions many components and asks about one.

When nothing scores, nothing is retrieved. The roster and the general layers
still go out, so the answer degrades to "correct but less specific" rather than
to a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.hardware import microbit
from app.services.hardware.components import in_kit
from app.services.hardware.microbit import Pin
from app.services.hardware.schema import Component

# How much a hit is worth, by where it was found. The question dominates
# because it is the only text the teacher wrote on purpose; lesson text is
# barely more than a tie-breaker because a 24k-character deck mentions
# everything in the kit at least once.
WEIGHTS = {"question": 6.0, "slide": 3.0, "title": 4.0, "lesson": 0.5}

# Below this, a match is one incidental word and not worth a profile.
THRESHOLD = 3.0

# Profiles are long. Three is enough for "compare the two buzzers" and short of
# the point where the lesson text gets squeezed.
MAX_PROFILES = 3

# Terms this short match inside other words often enough to be noise on their
# own; they still count when they appear as part of a longer alias.
MIN_TERM_LENGTH = 3


def _pattern(term: str) -> re.Pattern[str]:
    # Tolerates a plural without matching a different word: "buzzers" hits
    # "buzzer", "led" does not hit "ledge".
    return re.compile(rf"\b{re.escape(term)}(?:e?s)?\b", re.IGNORECASE)


_PATTERNS: dict[str, re.Pattern[str]] = {}


def _hits(term: str, haystack: str) -> int:
    if not haystack:
        return 0
    pattern = _PATTERNS.get(term)
    if pattern is None:
        pattern = _PATTERNS[term] = _pattern(term)
    return len(pattern.findall(haystack))


# What the teacher is doing with the pin, in the words the two environments
# actually use. Read/write matters because the same module is explained from
# opposite ends depending on the direction.
_WRITE = re.compile(
    r"digital\s*write|analog\s*write|write_digital|write_analog|servo\s*write|"
    r"digitalwritepin|analogwritepin|set\s+pin|turn\s+(?:it\s+)?(?:on|off)|\bwrite\s+(?:to\s+)?p\d",
    re.IGNORECASE,
)
_READ = re.compile(
    r"digital\s*read|analog\s*read|read_digital|read_analog|digitalreadpin|"
    r"analogreadpin|reading|detect|sens(?:e|ing)|value\s+of|\bread\s+(?:from\s+)?p\d",
    re.IGNORECASE,
)
_SIGNAL_WORDS = {
    "PWM": re.compile(r"\bpwm\b|duty\s*cycle|analog\s*write|write_analog", re.IGNORECASE),
    "analog": re.compile(r"\banalog(?:ue)?\b|analog\s*read|read_analog", re.IGNORECASE),
    "digital": re.compile(r"\bdigital\b|\bhigh\b|\blow\b", re.IGNORECASE),
    "I2C": re.compile(r"\bi2c\b|\bscl\b|\bsda\b", re.IGNORECASE),
    "SPI": re.compile(r"\bspi\b|\bmosi\b|\bmiso\b", re.IGNORECASE),
    "UART": re.compile(r"\buart\b|\bserial\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class Reading:
    """What the question appears to be about. Every field is a hint, not a
    finding - the model is told as much, so a wrong guess here costs nothing."""

    components: tuple[Component, ...] = ()
    pins: tuple[Pin, ...] = ()
    signals: tuple[str, ...] = ()
    writing: bool = False
    reading: bool = False
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def direction(self) -> str:
        if self.writing and self.reading:
            return "both driving a pin and reading one"
        if self.writing:
            return "driving a pin (an output)"
        if self.reading:
            return "reading a pin (an input)"
        return "not clear from the question"


def score(
    component: Component,
    *,
    question: str,
    slide: str = "",
    title: str = "",
    lesson: str = "",
) -> float:
    """How strongly this component is implicated, across all four sources."""
    total = 0.0
    for term in component.match_terms:
        if len(term) < MIN_TERM_LENGTH:
            continue
        # Multi-word terms are specific; single words are often shared across
        # half the kit. Weighting by word count is what keeps "led" from
        # outvoting "rgb led module".
        specificity = 1.0 + term.count(" ")
        for source, text in (
            ("question", question),
            ("slide", slide),
            ("title", title),
            ("lesson", lesson),
        ):
            found = _hits(term, text)
            if found:
                # Repeats are worth less each time - a lesson naming the same
                # module forty times is not forty times more about it.
                total += WEIGHTS[source] * specificity * (1 + 0.25 * (found - 1))
    return total


def identify(
    *,
    kit_model: str | None,
    question: str,
    slide: str = "",
    title: str = "",
    lesson: str = "",
    limit: int = MAX_PROFILES,
) -> Reading:
    """Rank the kit's components against everything known about the question.

    Two tiers, not one. A component the teacher NAMED outranks any component
    that is merely on the slide in front of them, however strongly the slide
    matches - otherwise a lesson about the RGB module answers a question about
    the relay with the RGB profile attached, which is a plausible wrong answer
    rather than a missing one. Context still decides the order within each tier,
    and still supplies the whole answer when the question names nothing.
    """
    candidates = in_kit(kit_model)
    scored = [
        (
            component,
            score(component, question=question, slide=slide, title=title, lesson=lesson),
            score(component, question=question),
        )
        for component in candidates
    ]
    hits = sorted(
        ((c, total, asked) for c, total, asked in scored if total >= THRESHOLD),
        key=lambda row: (-(row[2] > 0), -row[1], row[0].id),
    )
    hits = [(c, total) for c, total, _ in hits]
    text = f"{question}\n{slide}\n{title}"
    return Reading(
        components=tuple(c for c, _ in hits[:limit]),
        pins=microbit.pins_mentioned(question),
        signals=tuple(name for name, pattern in _SIGNAL_WORDS.items() if pattern.search(text)),
        writing=bool(_WRITE.search(text)),
        reading=bool(_READ.search(text)),
        scores={c.id: round(s, 1) for c, s in hits},
    )
