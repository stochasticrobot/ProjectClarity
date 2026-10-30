"""Define model responses for a short preliminary study of a research question.

Before the main research loop, the optional study can propose sample searches and
pages, describe what evidence the question needs, and estimate the scope where a
number makes sense. These Pydantic models validate those plans, source choices,
quotations and estimates. For example, an explanation can need evidence checks
without an estimated population. The study does not certify research completion.
Class and field descriptions are included in the schema sent to the model.
"""
from typing import Literal
from pydantic import Field

from projectclarity.coverage.coverage_contracts import ResearchSearch
from projectclarity.research_schemas import Contract, Identifier, Text, PlanningScopeRange


class CoverageCheck(Contract):
    """A proposed evidence requirement and a method for assessing it against research goals."""

    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    description: Text
    kind: Literal['enumeration', 'constraints', 'evidence_sufficiency']
    evidence_required: list[Text] = Field(min_length=1, max_length=6)
    assessment_method: Text


class AnswerReadinessProfile(Contract):
    """Task-specific coverage method, separate from progress toward answering the question."""

    answer_type: Text
    evidence_scope: Literal['provided_material', 'external_sources', 'mixed']
    numeric_estimate_applicable: bool
    coverage_checks: list[CoverageCheck] = Field(max_length=12)
    unresolved_questions: list[Text] = Field(max_length=8)


class CoverageStudyPlan(Contract):
    """Decide whether a preliminary coverage study is useful and propose a short plan."""

    needed: bool
    reason: Text
    plan: list[Text] = Field(max_length=4)
    queries: list[ResearchSearch] = Field(max_length=2)
    source_ids: list[Identifier] = Field(max_length=4)
    readiness: AnswerReadinessProfile


class StudyEvidence(Contract):
    """An exact quotation from an offered retrieved page, never a search snippet."""

    source_id: Identifier
    quote: str = Field(min_length=1, max_length=1000)


class CoverageStudyChoice(Contract):
    """A coverage-source candidate ID and the reason for selecting it."""

    candidate_id: Identifier
    reason: Text


class CoverageStudySelection(Contract):
    """Coverage-focused choices from the offered source and search-result references."""

    selections: list[CoverageStudyChoice]


class CoverageStudyEstimate(Contract):
    """A coverage range or boundary, with model priors separate from sourced estimates."""

    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    unit: Text
    range: PlanningScopeRange | None
    basis: Literal['evidence_informed', 'provisional_prior', 'unknown']
    method: Text
    rationale: Text
    assumptions: list[Text] = Field(max_length=6)
    limitations: list[Text] = Field(max_length=6)
    evidence: list[StudyEvidence] = Field(max_length=4)


class CoverageStudyAssessment(Contract):
    """Report a task-specific coverage method, optional estimates and uncertainty; never certify completion."""

    summary: Text
    coverage_boundary: Text
    coverage_outcome: Literal['established', 'partial', 'unknown', 'unnecessary']
    readiness: AnswerReadinessProfile
    estimates: list[CoverageStudyEstimate] = Field(max_length=8)
    limitations: list[Text] = Field(max_length=8)


from projectclarity.discovery.source_disposition import SourceDisposition


class ContractStudyChoice(CoverageStudyChoice, SourceDisposition):
    """A preliminary source decision explicitly linked to accepted requirements."""

    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)


class ContractStudySelection(Contract):
    """Preliminary source dispositions; rejected results never authorize acquisition."""

    selections: list[ContractStudyChoice]
