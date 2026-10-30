"""Define structured model replies about evidence coverage and remaining research gaps.

The coverage task asks which answer requirements are satisfied, which list items
have evidence, and which searches could address missing information. These
Pydantic models constrain that reply format. Proposed item keys group results
within an assessment; they are not independently established entity identities.
Later Python checks validate references and arithmetic, while meaning, currentness
and sufficiency remain model judgments. Class and field descriptions here are
included in the model's response schema.
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
    candidate_ids: list[str] = Field(min_length=1)


class CriterionAssessment(Contract):
    """An evidence-based judgment of one criterion, with unresolved gaps explicit."""

    criterion_id: Identifier
    judgment: Literal["unknown", "partial", "satisfied"]
    items: list[CoverageItem] = Field(max_length=1000)
    target_count: int | None = Field(ge=0, le=1000000)
    result_goal_id: Identifier | None = None
    benchmark_candidate_ids: list[str]
    evidence_candidate_ids: list[str]
    gaps: list[Text]
    rationale: Text


class CoverageAssessment(Contract):
    """Assess every offered criterion and propose bounded searches for remaining gaps."""

    assessments: list[CriterionAssessment]
    next_searches: list[ResearchSearch]
    # Historical fixture/record compatibility; missing revisions are explicitly recorded.
    scope_estimates: list[ScopeEstimate] = Field(default_factory=list, max_length=12)
