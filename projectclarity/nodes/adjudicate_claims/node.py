"""Implement adjudicate_claims: apply conservative Python rules to model assessments.

A review is advice. This node requires an unchanged, structurally anchored source
and a complete checked assessment before assigning supported/contradicted status.
Queued review remains pending; failed review becomes needs_review; bad evidence becomes invalid_evidence.
Out-of-scope assertions are retained for inspection but not accepted as answers.

Supported means this model assessed this passage as supporting the candidate.
It is not a truth guarantee, a cross-source consensus or publication permission.
Every claim remains not_published. A future human-review/publication policy can
consume these explicit statuses without mixing them with extraction confidence.
"""
from projectclarity.nodes.persist_candidates.node import persist_candidates
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text


def adjudicate_claims(state, output_root, monitor=None):
    """Turn saved reviewer assessments into explicit candidate statuses so downstream code
    can distinguish supported, contradicted, unreviewed and invalid-evidence claims.

    Recheck source integrity and quote anchors before applying deterministic status rules,
    then refresh candidate files. Independently queue source-bound report mapping
    repairs; supported factual verdicts remain unchanged by mapping defects. Supported means model-assessed passage support, not
    independently established truth, resolved identity or permission to publish.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    session.visited_nodes.append("adjudicate_claims")
    record = ExtractionRecord.model_validate(session.extraction)
    documents = {doc.source_id: doc for doc in session.documents}
    for candidate in record.candidates:
        review = session.claim_reviews.get(candidate.candidate_id)
        assessment = review.get("assessment") if review else None
        candidate.review_status = "reviewed" if assessment else "review_failed" if review else "unreviewed"
        candidate.adjudication_status = "needs_review" if review else "pending"
        candidate.adjudication_reason = ((review or {}).get("error_code") or "no_completed_review") if review else "review_queued"
        try:
            doc = documents[candidate.source_id]
            text = read_verified_text(output_root / session.session_id, doc)
            review_anchors = (review or {}).get("evidence", [])
            if any(text[e["start"]:e["end"]] != e["quote"] for e in review_anchors):
                raise EvidenceInputError("source_version_mismatch")
            if doc.text_sha256 != candidate.text_sha256 or any(
                e.text_sha256 != candidate.text_sha256 or text[e.start:e.end] != e.quote for e in candidate.evidence):
                raise EvidenceInputError("source_version_mismatch")
        except EvidenceInputError as exc:
            candidate.evidence_status = "invalid"
            if str(exc) not in candidate.validation_errors:
                candidate.validation_errors.append(str(exc))
        if candidate.evidence_status != "anchored":
            candidate.adjudication_status, candidate.adjudication_reason = "invalid_evidence", "structural_checks_failed"
        elif assessment and not review.get("error_code") and review["text_sha256"] == candidate.text_sha256:
            candidate.adjudication_status = assessment["verdict"] if assessment["in_scope"] else "out_of_scope"
            candidate.adjudication_reason = assessment["rationale"]
    from projectclarity.claim_review.report_mapping import queue_mapping_repairs
    queue_mapping_repairs(session, record)
    session.extraction = record.model_dump(mode="json")
    # Refresh the candidate files without recording persist_candidates as an
    # executed workflow step; this update is part of adjudicate_claims.
    persist_candidates({"session": session}, output_root, monitor, record_visit=False)
    if monitor:
        monitor.observe("claims_adjudicated", stage="adjudicate_claims", candidate_count=len(record.candidates),
            supported_claims=sum(c.adjudication_status == "supported" for c in record.candidates),
            unresolved_claims=sum(c.adjudication_status in {"needs_review", "insufficient_evidence"} for c in record.candidates))
    return {"session": session}
