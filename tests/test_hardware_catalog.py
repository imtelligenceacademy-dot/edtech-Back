"""Catalogue integrity - the invariants that keep the profiles trustworthy.

These are the tests that fail when someone adds a component in a hurry. The one
that matters most is the last: a polarity may only be stated by a profile that
says where it came from. Without that check, "not verified" quietly becomes a
default that nobody ever clears, and a plausible guess typed into a data file
reads exactly like a fact read off a datasheet.
"""

from __future__ import annotations

import re

import pytest

from app.services import kits
from app.services.hardware import principles, render
from app.services.hardware.components import ALL, BY_ID, in_kit, kit_models
from app.services.hardware.schema import (
    Category,
    Direction,
    Polarity,
    Signal,
    Verified,
)

# "Alligator Clip Cables (x10)" and "Alligator Clip Cables" are the same part.
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def _normalise(name: str) -> str:
    return _PARENTHETICAL.sub("", name).strip().lower()


def test_component_ids_are_unique():
    assert len(BY_ID) == len(ALL)


def test_every_component_belongs_to_a_kit_that_exists():
    known = {kits.HONEYCOMB.model, kits.SENSOR_45_IN_1.model}
    assert set(kit_models()) <= known


@pytest.mark.parametrize("kit", [kits.HONEYCOMB, kits.SENSOR_45_IN_1])
def test_every_part_on_the_printed_kit_list_has_a_profile(kit):
    """The objective was the kits, not the lessons currently using them.

    A teacher can ask about any part in the box, and a part with no profile is
    indistinguishable from a part that does not exist - so the catalogue is
    checked against the kit's own printed contents rather than against usage.
    """
    profiled = {_normalise(c.name) for c in in_kit(kit.model)}
    missing = [item for item in kit.contents if _normalise(item) not in profiled]
    assert not missing, f"{kit.model} parts with no component profile: {missing}"


@pytest.mark.parametrize("kit", [kits.HONEYCOMB, kits.SENSOR_45_IN_1])
def test_no_profile_invents_a_part_the_kit_does_not_contain(kit):
    """The reverse direction. A profile for something not in the box would be
    offered to a teacher as if they owned it."""
    listed = {_normalise(item) for item in kit.contents}
    extra = [c.name for c in in_kit(kit.model) if _normalise(c.name) not in listed]
    assert not extra, f"{kit.model} profiles for parts not on the kit list: {extra}"


def test_a_stated_polarity_carries_a_source():
    """Nothing may claim an active level on nobody's authority.

    This is the invariant that makes NOT VERIFIED mean something. If a guess
    could be typed in without a source, every unverified module would slowly
    acquire a confident polarity and the labelling would stop being a signal.
    """
    guessed = [
        c.id
        for c in ALL
        if c.electrical.polarity in (Polarity.active_high, Polarity.active_low)
        and c.verification.status is Verified.needs_verification
    ]
    assert not guessed, f"polarity stated without a source: {guessed}"


def test_a_stated_analog_direction_carries_a_source():
    guessed = [
        c.id
        for c in ALL
        if c.analog is not None
        and c.analog.direction in (Direction.more_is_higher, Direction.more_is_lower)
        and c.verification.status is Verified.needs_verification
    ]
    assert not guessed, f"analog direction stated without a source: {guessed}"


def test_an_unverified_module_offers_a_way_to_settle_it():
    """A refusal on its own leaves the teacher stuck. Every module whose
    polarity or direction is unknown carries the classroom check that resolves
    it - which is the difference between honest and useless."""
    stuck = []
    for c in ALL:
        if c.category is Category.passive:
            continue
        unknown_level = c.electrical.polarity is Polarity.unknown
        unknown_direction = c.analog is not None and c.analog.direction is Direction.unknown
        if not (unknown_level or unknown_direction):
            continue
        help_text = " ".join(
            (c.teaching.beginner, *c.teaching.troubleshooting, c.analog.calibration if c.analog else "")
        )
        if not help_text.strip():
            stuck.append(c.id)
    assert not stuck, f"unknown behaviour with no way to resolve it: {stuck}"


def test_every_component_says_where_its_facts_came_from():
    assert all(c.verification.source for c in ALL if c.category is not Category.passive)


def test_electrical_components_declare_how_they_are_talked_to():
    silent = [
        c.id
        for c in ALL
        if c.category is not Category.passive and not c.signals and c.category is not Category.board
    ]
    assert not silent, f"components with no signal type declared: {silent}"


def test_the_roster_leaves_out_the_parts_with_no_behaviour():
    """Cables and battery holders have nothing to say about polarity. Forty-odd
    roster lines is already a lot of prompt; three of them being 'USB cable'
    would be waste."""
    roster = render.roster(in_kit("KS4010"))
    assert "USB Cable" not in roster
    assert "Dupont" not in roster
    assert "RGB LED Module" in roster


def test_the_roster_marks_everything_it_is_not_sure_of():
    roster = render.roster(in_kit("KS4010"))
    for line in roster.splitlines():
        if "NOT VERIFIED" in line:
            assert "[unconfirmed]" in line, f"unverified without a flag: {line}"


def test_the_general_layers_name_no_component():
    """Layer 1 must stay general. The moment it names a module, that module's
    behaviour starts leaking into answers about every other one."""
    general = (
        principles.CORE_RULE
        + principles.GENERAL_ELECTRONICS
        + principles.SOURCE_PRIORITY
        + principles.CONFLICT
        + principles.UNKNOWNS
    ).lower()
    for name in ("rgb", "buzzer", "photoresistor", "servo", "relay", "keyestudio"):
        assert name not in general, f"the general layer names a component: {name}"


def test_adding_a_kit_needs_no_prompt_change():
    """The retrieval path is driven entirely by data.

    Every kit's roster and every profile is generated from the catalogue, so a
    new kit file plus a line in CATALOGUES is the whole change. This asserts the
    property that makes that true: no prompt constant mentions a kit.
    """
    from app.services.hardware import context

    prompt_text = (
        context.PROCEDURE + context._KIT_HEADER + context._PROFILE_HEADER + context._NO_PROFILE
    )
    for model in ("KS4010", "KS4011", "KS4009"):
        assert model not in prompt_text


def test_the_two_kits_are_separate_sets():
    honeycomb = {c.id for c in in_kit("KS4011")}
    sensor_kit = {c.id for c in in_kit("KS4010")}
    assert honeycomb and sensor_kit
    assert not honeycomb & sensor_kit


def test_modules_sharing_a_name_across_kits_are_separate_profiles():
    """The kits both contain a "Soil Humidity Sensor" and a "Hall Magnetic
    Sensor". They are not asserted to be the same board, because two modules
    with the same name can be electrically opposite - which is the reason the
    schema keys on a module, not on a part type.
    """
    for name in ("soil humidity sensor", "hall magnetic sensor", "traffic light module"):
        matches = [c for c in ALL if c.name.lower() == name]
        assert len(matches) == 2, name
        assert {c.kits for c in matches} == {("KS4011",), ("KS4010",)}


def test_the_kit_a_component_claims_matches_the_kit_it_is_filed_under():
    for component in ALL:
        assert component.kits, component.id
        for model in component.kits:
            assert component in in_kit(model)


def test_signal_none_is_only_used_for_non_electronic_parts():
    wrong = [
        c.id
        for c in ALL
        if Signal.none in c.signals and c.category not in (Category.passive, Category.breakout)
    ]
    assert not wrong, f"electrical parts declared as having no connection: {wrong}"
