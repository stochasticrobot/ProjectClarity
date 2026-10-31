"""Bound intermediate model responses so a reduction tree has bounded fan-in cost.

The production summary task uses this approved schema. Length limits constrain
transport, not semantic fidelity; application validation separately checks input
accounting, witness provenance and inherited uncertainty flags.
"""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=96)]


class BoundedDigest(BaseModel):
    """A bounded model interpretation with complete input accounting and explicit unresolved-detail flags."""
    model_config = ConfigDict(extra='forbid')
    covered_input_ids: list[Identifier] = Field(min_length=1, max_length=32)
    summary: str = Field(min_length=1, max_length=6000)
    witness_candidate_ids: list[Identifier] = Field(max_length=24)
    limitations: list[Annotated[str, Field(max_length=500)]] = Field(max_length=8)
    unresolved_conflict: bool
    unresolved_identity: bool
    requires_detail: bool
