"""Define checked profile contributions without conflating a subject with namesakes.

Each batch assesses whether its source facts concern the requested entity, then
proposes cited overview and section findings. Unknown sections are supplied by
assembly, never fabricated by a model. Literal identity excerpts are checked against
the supplied sources; their meaning and attribution remain model judgments.
"""
from typing import Literal
from projectclarity.claim_review.support_diagnostics import FailureCategory, failure_categories_field
from pydantic import BaseModel, ConfigDict


class IdentityAssessment(BaseModel):
    """Source-bound subject assessment; ambiguous evidence cannot support findings."""
    model_config = ConfigDict(extra='forbid', strict=True)
    fact_id: str
    status: Literal['match', 'ambiguous', 'not_target']
    reason: str
    failure_categories: list[FailureCategory] = failure_categories_field()
    identity_quote: str | None


class ProfileFinding(BaseModel):
    """One cited statement, retaining contradictory evidence rather than choosing it."""
    model_config = ConfigDict(extra='forbid', strict=True)
    text: str
    status: Literal['supported', 'conflicting']
    fact_ids: list[str]


class ProfileSection(BaseModel):
    """Contributions to one question-specific requirement, not a fixed biography template."""
    model_config = ConfigDict(extra='forbid', strict=True)
    requirement_id: str
    findings: list[ProfileFinding]


class ProfileBatch(BaseModel):
    """Bounded source assessment and findings with explicit unused matched evidence."""
    model_config = ConfigDict(extra='forbid', strict=True)
    identity_assessments: list[IdentityAssessment]
    overview: list[ProfileFinding]
    sections: list[ProfileSection]
    unused_fact_ids: list[str]
