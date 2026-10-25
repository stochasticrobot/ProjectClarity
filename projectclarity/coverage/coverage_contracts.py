"""Typed model assessments of criteria, evidence-backed items and further searches.

Item keys are model-proposed groupings for this assessment, not canonical entity
IDs. Python checks references and arithmetic; it does not certify semantic
identity, completeness, attribution or feasibility.
"""
from typing import Literal
from pydantic import Field
from projectclarity.research_schemas import Contract, Identifier, Text, ScopeEstimate


class ResearchSearch(Contract):
    """A proposed search query linked to existing research goals."""

    query: str = Field(min_length=1, max_length=300)
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    reason: Text


class CoverageItem(Contract):
    """One proposed distinct item satisfying the criterion, with retained evidence references."""

    key: str = Field(min_length=1)
    candidate_ids: list[str] = Field(min_length=1, max_length=20)


class CriterionAssessment(Contract):
    """An evidence-based judgment of one criterion, with unresolved gaps explicit."""

    criterion_id: Identifier
    judgment: Literal["unknown", "partial", "satisfied"]
    items: list[CoverageItem] = Field(max_length=100)
    target_count: int | None = Field(ge=0, le=1000000)
    benchmark_candidate_ids: list[str] = Field(max_length=20)
    evidence_candidate_ids: list[str] = Field(max_length=40)
    gaps: list[Text]
    rationale: Text


class CoverageAssessment(Contract):
    """Assess every offered criterion and propose bounded searches for remaining gaps."""

    assessments: list[CriterionAssessment] = Field(max_length=12)
    next_searches: list[ResearchSearch]
    # Historical fixture/record compatibility; missing revisions are explicitly recorded.
    scope_estimates: list[ScopeEstimate] = Field(default_factory=list, max_length=12)
