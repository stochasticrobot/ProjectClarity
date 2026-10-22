"""Implement validate_evidence: check whether proposed evidence actually exists.

The model might write a plausible quotation that is not in the source, invent a
goal ID, or connect an entity that it never defined. This Python-only node checks
those concrete mistakes. It tolerates layout whitespace while finding the quote in the saved passage and records
its character offsets. Multiple matches are ambiguous, not guessed locations.

This is NOT a fact checker. 'Alice visited Lab X' is a real quotation but does
not establish that Alice works there. Semantic support is assessed by the optional
review_claims node; this step alone leaves claims unreviewed and unpublished.
The graph calls validate_evidence() after extraction, including after a partial
failure, so earlier proposals remain inspectable. No model call or UI occurs.
"""

from collections import Counter
from pathlib import Path

from projectclarity.extraction.candidate_claim_storage import save_extraction_record
from projectclarity.extraction.extraction_contracts import AnchoredEvidence, CandidateClaim, ExtractionRecord
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.extraction.source_quote_matching import QuoteMatchError, match_source_quote


def validate_batch(attempt, text: str | None, known_goals: set[str], source_error=None) -> list[CandidateClaim]:
    """Return each claim with ID and source-quotation checks, retaining invalid proposals.

    The caller supplies verified source text or an explicit source error. Each
    quote must occur exactly once inside this attempt's passage after whitespace
    normalization. Anchors retain the original text; the model quote is kept in
    the proposal. Changed words or punctuation are never repaired. A matching
    quote does not establish that it supports the claim's meaning; that review
    belongs to the separate optional review_claims node. Entities without claims remain in the batch record.
    """
    batch = attempt.result
    entity_counts = Counter(entity.entity_id for entity in batch.entities)
    claim_counts = Counter(claim.claim_id for claim in batch.claims)
    candidates = []
    for index, claim in enumerate(batch.claims, 1):
        errors = [source_error] if source_error else []
        if claim_counts[claim.claim_id] != 1:
            errors.append("duplicate_claim_id")
        if any(count != 1 for count in entity_counts.values()):
            errors.append("duplicate_entity_id")
        if set(claim.goal_ids) - known_goals:
            errors.append("unknown_goal_id")
        referenced = set(claim.entity_ids)
        if claim.relationship:
            endpoints = {claim.relationship.subject_id, claim.relationship.object_id}
            if endpoints - referenced:
                errors.append("relationship_not_in_claim_entities")
            referenced |= endpoints
        if referenced - entity_counts.keys():
            errors.append("unknown_entity_id")
        anchored = []
        for evidence in claim.evidence:
            if evidence.passage_id != attempt.passage_id:
                errors.append("unknown_passage_id")
                continue
            if text is None:
                continue
            passage = text[attempt.start:attempt.end]
            try:
                match = match_source_quote(passage, evidence.quote)
            except QuoteMatchError as exc:
                errors.append(str(exc))
            else:
                anchored.append(AnchoredEvidence(source_id=attempt.source_id,
                    text_sha256=attempt.text_sha256, passage_id=attempt.passage_id,
                    start=attempt.start + match.start, end=attempt.start + match.end,
                    quote=match.quote, match_method=match.match_method))
        candidates.append(CandidateClaim(
            # Position makes even duplicated model IDs separately inspectable.
            candidate_id=f"{attempt.batch_id}_C{index}", batch_id=attempt.batch_id,
            source_id=attempt.source_id, text_sha256=attempt.text_sha256, proposal=claim,
            evidence_status="invalid" if errors else "anchored",
            validation_errors=sorted(set(errors)), evidence=anchored))
    return candidates


def validate_evidence(state: dict, output_root: Path, monitor=None) -> dict:
    """Recheck source versions and goal/entity/quote links without spending tokens."""
    session = state["session"]
    session.visited_nodes.append("validate_evidence")
    record = ExtractionRecord.model_validate(session.extraction)
    record.schema_version = "1.3"  # This pass uses source-mapped whitespace matching.
    prior_candidates = {c.candidate_id: c for c in record.candidates}
    record.candidates = []
    documents = {document.source_id: document for document in session.documents}
    if monitor:
        monitor.observe("evidence_validation_started", stage="validating_evidence")
    for attempt in record.attempts:
        if attempt.result is None:
            continue
        text, error = None, None
        try:
            document = documents[attempt.source_id]
            if document.text_sha256 != attempt.text_sha256:
                raise EvidenceInputError("source_version_mismatch")
            text = read_verified_text(output_root / session.session_id, document)
        except EvidenceInputError as exc:
            error = str(exc)
            entry = {"source_id": attempt.source_id, "error_code": error}
            if entry not in record.source_errors:
                record.source_errors.append(entry)
        for candidate in validate_batch(attempt, text, {goal.goal_id for goal in session.plan.goals}, error):
            previous = prior_candidates.get(candidate.candidate_id)
            if previous and previous.text_sha256 == candidate.text_sha256:
                candidate.review_status = previous.review_status
                candidate.adjudication_status = previous.adjudication_status
                candidate.adjudication_reason = previous.adjudication_reason
            record.candidates.append(candidate)
    save_extraction_record(session, record, output_root)
    if monitor:
        monitor.observe("evidence_validation_finished", stage="validating_evidence",
            candidate_count=len(record.candidates),
            invalid_candidates=sum(c.evidence_status == "invalid" for c in record.candidates))
    return {"session": session}
