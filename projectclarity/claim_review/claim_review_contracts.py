"""Define the structured reply expected when a model reviews proposed source claims.

For each offered candidate statement, the reviewer reports whether its assigned
passage supports it and provides the corresponding evidence and report-field
assessment. A candidate is a proposed claim, not already accepted knowledge.
These Pydantic models check the reply's shape; later Python checks verify that IDs
and quotations belong to the offered material and apply support statuses.
No calls or file writes happen here. Class and field descriptions are included
in the JSON schema sent to the reviewing model.
"""
from typing import Literal
from pydantic import Field
from projectclarity.research_schemas import Contract, Text


class ReportMappingAssessment(Contract):
    """Assess whether this claim's contribution to one requested result shape is
    represented in its supplied records, independently of factual support.
    """
    goal_id: str
    status: Literal["mapped", "needs_mapping", "identity_unresolved", "not_applicable"]
    field_names: list[str]
    rationale: Text


class ClaimAssessment(Contract):
    """Assess one candidate's full meaning, qualifications and research relevance."""
    candidate_id: str = Field(min_length=1, max_length=64)
    verdict: Literal["supported", "contradicted", "insufficient_evidence"]
    in_scope: bool
    rationale: Text
    # Source quotations, checked by Python with whitespace-only tolerance and
    # original-text anchors. Words/punctuation must still match.
    # Insufficient evidence may have no quotation: absence is not a quote.
    evidence_quotes: list[str] = Field(max_length=4)
    report_mappings: list[ReportMappingAssessment]


class ClaimReviewBatch(Contract):
    """Return exactly one assessment for each requested candidate, no additional IDs."""
    assessments: list[ClaimAssessment] = Field(max_length=12)
