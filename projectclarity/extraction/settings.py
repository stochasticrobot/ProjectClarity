"""Immutable, operator-owned extraction packing and transport settings.

Presets are experimental application limits, not provider capability claims.
The request stores resolved values so restart never re-resolves a changed preset.
Only extraction consumes these settings; review and coverage retain their budgets.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_EXTRACTION_PROFILE = 'medium'


class ExtractionSettings(BaseModel):
    """Resolved local settings, never a model-facing response schema."""

    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    version: Literal['extraction-sizing-v1'] = 'extraction-sizing-v1'
    profile: Literal['small', 'medium', 'large'] = 'small'
    max_records_per_passage: int = Field(default=8, ge=1, le=64)
    record_target_characters: int = Field(default=3000, ge=1, le=48000)
    max_passage_characters: int = Field(default=12000, ge=1, le=48000)
    max_message_characters: int = Field(default=48000, ge=1, le=128000)
    max_input_characters: int = Field(default=64000, ge=1, le=160000)
    max_output_tokens: int = Field(default=8192, ge=128, le=32768)

    @model_validator(mode='after')
    def consistent_limits(self):
        """Reject internally contradictory bounds before any work is scheduled.

        Args:
            self: Frozen settings being constructed from an operator request.
        Returns:
            Validated settings; raises ValueError if structural targets exceed
            passage, message or whole-envelope ceilings. Fit is checked again
            on each actual serialized call, not inferred from these bounds.
        """
        if not (self.record_target_characters <= self.max_passage_characters
                <= self.max_message_characters <= self.max_input_characters):
            raise ValueError('Extraction bounds require target <= passage <= messages <= input')
        return self

    @classmethod
    def for_profile(cls, profile, **overrides):
        """Resolve a preset once, applying explicit numeric operator overrides.

        Args:
            cls: Settings class to validate and return.
            profile: Small, medium or large preset name.
            overrides: Named settings fields overriding preset values; unknown
                fields and invalid bounds are rejected, never silently clamped.
        Returns:
            Frozen resolved settings. Unknown profiles raise ValueError. No
            provider access or model-capability inference is performed.
        """
        factors = {'small': 1, 'medium': 2, 'large': 4}
        if profile not in factors:
            raise ValueError('Unknown extraction profile')
        factor = factors[profile]
        return cls(**(dict(profile=profile, max_records_per_passage=8 * factor,
            record_target_characters=3000 * factor, max_passage_characters=12000 * factor,
            max_message_characters={'small': 48000, 'medium': 80000, 'large': 112000}[profile],
            max_input_characters={'small': 64000, 'medium': 96000, 'large': 128000}[profile],
            max_output_tokens=8192 * factor) | overrides))
