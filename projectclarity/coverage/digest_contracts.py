"""Define the model response for summarizing one batch of research evidence.

When the evidence is too large for a single completion assessment, the model
summarizes smaller batches first. These Pydantic models describe each batch's
findings, evidence references and unresolved issues for the later whole-answer
assessment. They do not declare the research complete. Their class descriptions
and field guidance form part of the JSON schema supplied to the model.
"""
from pydantic import Field
from projectclarity.research_schemas import Contract


class DigestFinding(Contract):
    """A bounded interpretation retaining its original evidence references and unresolved limitations."""

    statement: str = Field(min_length=1, max_length=3000)
    candidate_ids: list[str] = Field(min_length=1, max_length=64)
    criterion_ids: list[str] = Field(min_length=1, max_length=12)
    limitations: list[str] = Field(max_length=12)


class EvidenceDigest(Contract):
    """A source-linked model interpretation of one complete bounded evidence batch, not primary evidence."""

    findings: list[DigestFinding] = Field(min_length=1, max_length=64)
