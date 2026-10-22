"""Describe the information exchanged during knowledge extraction.

Think of these models as blank forms. The model fills in names, claims and
quotations; Python fills in source versions, passage locations and validation
results. For example, 'Alice works at Lab X' can name two entities and a
relationship, but still needs an exact quotation before it is an anchored
candidate. 'Anchored' means we found the quotation, NOT that the claim is true.

knowledge_extraction_messages.py sends ExtractionBatch's generated JSON schema to the
provider, separately from its plain-language prompt. ExtractedEntity,
ExtractedRelationship, ExtractedClaim and QuotedEvidence are parts of that reply.
ExtractionAttempt, AnchoredEvidence, CandidateClaim and ExtractionRecord are
application-owned records, not fields the LLM can submit. These classes validate
field shapes; validate_evidence_node.py checks references against actual data.
This file does not read documents, call models, or approve claims.

Maintenance gotcha: Pydantic class docstrings and Field descriptions appear in
generated schemas. Editing them can change the model's instructions even when
no field is added. Use ordinary # comments for developer-only explanations.
"""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from projectclarity.research_schemas import Contract, Identifier, Text


class ExtractedEntity(Contract):
    """A named subject mentioned by this batch; IDs are local, not global identities."""

    entity_id: Identifier
    name: Text
    entity_type: Text


class ExtractedRelationship(Contract):
    """An optional subject/predicate/object form of the enclosing claim."""

    subject_id: Identifier
    predicate: Text
    object_id: Identifier


class QuotedEvidence(Contract):
    """A proposed verbatim quotation from the supplied passage, not a citation URL."""

    passage_id: Identifier
    # Do not strip whitespace: Python must match what the model actually quoted.
    quote: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ExtractedClaim(Contract):
    """One checkable assertion, its research purpose and its proposed evidence."""

    claim_id: Identifier
    statement: Text
    goal_ids: list[Identifier] = Field(min_length=1, max_length=8)
    entity_ids: list[Identifier] = Field(max_length=12)
    relationship: ExtractedRelationship | None
    qualifications: list[Text] = Field(max_length=6, description="Dates, attribution, uncertainty or scope restrictions; empty when none are stated.")
    evidence: list[QuotedEvidence] = Field(min_length=1, max_length=4)


class ExtractionBatch(Contract):
    """The complete LLM reply for one passage; empty lists are a valid no-findings result."""

    entities: list[ExtractedEntity] = Field(max_length=30)
    claims: list[ExtractedClaim] = Field(max_length=12)


class ExtractionAttempt(Contract):
    """Receipt for a passage, including skipped work and unsuccessful paid attempts."""

    batch_id: str
    source_id: Identifier
    text_sha256: str
    passage_id: Identifier
    start: int
    end: int
    status: Literal["not_attempted", "started", "completed", "failed"] = "not_attempted"
    error_code: str | None = None
    input_sha256: str | None = None
    # Relative to this run's folder. Older records and skipped/preparation-failed
    # passages can lack a trace. A trace is diagnostic data, not accepted evidence.
    trace_path: str | None = None
    response_model: str | None = None
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    elapsed_seconds: float | None = None
    result: ExtractionBatch | None = None


class AnchoredEvidence(Contract):
    """Python-resolved offsets: start inclusive, end exclusive, in saved normalized text."""

    source_id: Identifier
    text_sha256: str
    passage_id: Identifier
    start: int
    end: int
    quote: str
    # Original source slice above; submitted wording remains in proposal.evidence.
    match_method: Literal["exact", "whitespace_normalized"] = "exact"


class CandidateClaim(Contract):
    """An assertion kept for inspection; review does not publish or establish truth."""

    candidate_id: str
    batch_id: str
    source_id: Identifier
    text_sha256: str
    proposal: ExtractedClaim
    evidence_status: Literal["anchored", "invalid"]
    validation_errors: list[str]
    evidence: list[AnchoredEvidence]
    review_status: Literal["unreviewed", "reviewed", "review_failed"] = "unreviewed"
    adjudication_status: Literal["pending", "supported", "contradicted", "insufficient_evidence", "out_of_scope", "invalid_evidence", "needs_review"] = "pending"
    adjudication_reason: str | None = None
    publication_status: Literal["not_published"] = "not_published"


class ExtractionRecord(Contract):
    """Versioned local work record, including coverage gaps and validation outcomes."""

    schema_version: Literal["1.0", "1.1", "1.2", "1.3"] = "1.3"
    prompt_version: Literal["knowledge-extraction-v1"] = "knowledge-extraction-v1"
    retrieval_incomplete: bool = False
    attempts: list[ExtractionAttempt] = Field(default_factory=list)
    # These are application-owned codes, not raw provider/file exception messages.
    source_errors: list[dict[str, str]] = Field(default_factory=list)
    candidates: list[CandidateClaim] = Field(default_factory=list)
