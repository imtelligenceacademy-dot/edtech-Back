"""The component catalogue, assembled from one module per kit.

Adding a kit is adding a file here and adding it to CATALOGUES. Nothing else in
the system needs to change: kits are looked up by model number, components
declare which kits contain them, and the prompt is built from whatever is
registered. No prompt string anywhere names a specific component.

Components declare their kits rather than kits listing their components, because
the same module genuinely does appear in more than one kit - and when it does,
it is one profile with two kit tags, so a correction made once reaches every
lesson that uses it. Two modules that share a NAME but differ electrically are
two entries with two ids, which is the distinction that matters: an RGB module
is not a kind of thing, it is a specific board with a specific common leg.
"""

from __future__ import annotations

from app.services.hardware.components import ks4010, ks4011
from app.services.hardware.schema import Component

# Registration order decides nothing except the roster's stability. Add new kits
# to the end.
CATALOGUES = (ks4011.COMPONENTS, ks4010.COMPONENTS)

ALL: tuple[Component, ...] = tuple(c for catalogue in CATALOGUES for c in catalogue)

BY_ID: dict[str, Component] = {c.id: c for c in ALL}

if len(BY_ID) != len(ALL):  # pragma: no cover - a typo in a catalogue, caught at import
    counted: dict[str, int] = {}
    for component in ALL:
        counted[component.id] = counted.get(component.id, 0) + 1
    duplicates = sorted(cid for cid, n in counted.items() if n > 1)
    raise RuntimeError(f"duplicate component ids in the hardware catalogue: {duplicates}")


def get(component_id: str) -> Component | None:
    return BY_ID.get(component_id)


def in_kit(kit_model: str | None) -> tuple[Component, ...]:
    """Every component in one kit, in catalogue order."""
    if not kit_model:
        return ()
    return tuple(c for c in ALL if c.in_kit(kit_model))


def kit_models() -> tuple[str, ...]:
    """Every kit any component claims membership of.

    Deliberately derived from the components rather than from the kit registry,
    so a component tagged with a kit that does not exist shows up as an extra
    entry here instead of silently never being retrieved.
    """
    models: list[str] = []
    for component in ALL:
        for model in component.kits:
            if model not in models:
                models.append(model)
    return tuple(models)
