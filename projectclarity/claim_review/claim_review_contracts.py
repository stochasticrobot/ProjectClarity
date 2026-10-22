"""Define the reviewer's answer form, separate from the extraction answer form.

A reviewer must assess every requested candidate against its original passage,
not vote on how confident the extractor sounded. Verdicts describe support in
that passage, not universal truth. Python separately checks ID coverage and
applies policy; these models perform no calls, file writes or publication.
"""
from typing import Literal
from pydantic import Field
from projectclarity.research_schemas import Contract, Text


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


class ClaimReviewBatch(Contract):
    """Return exactly one assessment for each requested candidate, no additional IDs."""
    assessments: list[ClaimAssessment] = Field(max_length=12)
