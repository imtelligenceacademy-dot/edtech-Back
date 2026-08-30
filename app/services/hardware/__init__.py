"""Hardware intelligence for the teacher assistant.

Five layers, each in its own module, each answering a different question:

  principles  Layer 1  general electronics, and the rule that a programming
                       value never determines a physical outcome on its own
  microbit    Layer 2  the board: pin capabilities, conflicts, V1 vs V2, and
                       the equivalence of the two programming environments
  kits        Layer 3  which kit is on the desk - lives in app/services/kits.py,
                       which predates this package and is still its owner
  components  Layer 4  what each specific module does with a level or a value
  lesson      Layer 5  supplied per request by the caller: the open lesson's
                       title, extracted text and slide reading

Layer 4 is where behaviour lives, and it lives there because that is where it is
true. A lesson references a kit and a kit contains components; neither defines
what HIGH does, and there is nowhere in this package to write a rule keyed on a
grade, a lesson number or a component name pattern.

Adding a kit means adding one file under components/ and listing it in
components.CATALOGUES. No prompt text changes, because no prompt text names a
component.
"""

from __future__ import annotations

# Exported under a different name than the module it lives in: binding a
# function called `identify` onto the package would shadow the `identify`
# submodule, and the next person to write `hardware.identify.identify` would get
# a confusing AttributeError instead of the module.
from app.services.hardware.context import hardware_note
from app.services.hardware.identify import identify as identify_components
from app.services.hardware.render import profile, roster
from app.services.hardware.schema import Component

__all__ = ["hardware_note", "identify_components", "profile", "roster", "Component"]
