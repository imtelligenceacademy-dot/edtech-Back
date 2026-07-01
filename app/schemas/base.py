"""Base Pydantic model that serializes to camelCase to match the frontend's
`types/index.ts`, while still accepting snake_case input from Python code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
