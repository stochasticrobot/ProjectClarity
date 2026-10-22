"""Implement review_claims: ask a second LLM task whether the evidence supports claims.

One call reviews the anchored claims from one extraction passage. We retain the
passage context so a matching quote cannot hide a nearby negation. Invalid
anchors never reach this reviewer. Earlier reviewed batches are not rerun after
discovery. Layout whitespace may differ, but stored review anchors retain the original text.
A bad quote rejects only its claim; invalid batch IDs reject the whole batch.
Missing budgets, bad IDs, altered sources and malformed replies leave
explicit failures; they never become positive judgments.

This node records assessments only. adjudicate_claims_node.py assigns claim
statuses using Python policy and rechecks evidence before saving results.
"""
import json

from projectclarity.claim_review.claim_review_messages import review_messages
from projectclarity.claim_review.claim_review_contracts import ClaimReviewBatch
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.extraction.source_quote_matching import QuoteMatchError, match_source_quote
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.research_planner import read_api_key
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import save_session, write_text


def review_claims(state, output_root, key_reader=read_api_key, monitor=None):
    """Review new anchored candidates, retaining per-batch outcomes without publication."""
    session = state["session"]
    session.visited_nodes.append("review_claims")
    record = ExtractionRecord.model_validate(session.extraction)
    documents = {doc.source_id: doc for doc in session.documents}
    for attempt in record.attempts:
        if session.status == "cancelled":
            break
        candidates = [c for c in record.candidates if c.batch_id == attempt.batch_id
            and c.evidence_status == "anchored" and c.candidate_id not in session.claim_reviews]
        if not candidates:
            continue
        assessments, error, receipt = {}, None, None
        claim_errors, review_evidence = {}, {}
        try:
            if monitor:
                monitor.check_cancel()
            document = documents[attempt.source_id]
            if document.text_sha256 != attempt.text_sha256:
                raise EvidenceInputError("source_version_mismatch")
            text = read_verified_text(output_root / session.session_id, document)
            answer, receipt = invoke_structured(session, output_root,
                call_id="review_" + attempt.batch_id, task="review_claims",
                prompt_version="claim-review-v1", messages=review_messages(session, attempt, candidates, text),
                schema=ClaimReviewBatch, key_reader=key_reader, monitor=monitor)
            error = receipt["error_code"]
            if answer is not None:
                ids = [item.candidate_id for item in answer.assessments]
                if len(ids) != len(set(ids)) or set(ids) != {c.candidate_id for c in candidates}:
                    error = "review_candidate_id_mismatch"
                else:
                    passage = text[attempt.start:attempt.end]
                    # An invalid quotation belongs to this assessment, not its
                    # neighbors. Broken batch IDs still fail the whole response:
                    # we cannot safely attribute an ambiguous/incomplete roster.
                    for item in answer.assessments:
                        anchors = []
                        for quote in item.evidence_quotes:
                            try:
                                match = match_source_quote(passage, quote)
                            except QuoteMatchError as exc:
                                claim_errors[item.candidate_id] = "review_" + str(exc)
                            else:
                                anchors.append({"start": attempt.start + match.start,
                                    "end": attempt.start + match.end, "quote": match.quote,
                                    "submitted_quote": quote, "match_method": match.match_method})
                        if item.verdict != "insufficient_evidence" and not item.evidence_quotes:
                            claim_errors[item.candidate_id] = "review_missing_quote"
                        if item.candidate_id not in claim_errors:
                            assessments[item.candidate_id] = item.model_dump(mode="json")
                            review_evidence[item.candidate_id] = anchors
        except EvidenceInputError as exc:
            error = str(exc)
        except (RunCancelled, KeyboardInterrupt, EOFError):
            session.status, error = "cancelled", "cancelled"
            session.error = "Claim review cancelled; prior work retained."
        for candidate in candidates:
            session.claim_reviews[candidate.candidate_id] = {
                "validation_version": "source-quotes-v2",
                "assessment": assessments.get(candidate.candidate_id),
                "error_code": error or claim_errors.get(candidate.candidate_id),
                "evidence": review_evidence.get(candidate.candidate_id, []),
                "text_sha256": attempt.text_sha256,
                "call_id": receipt["call_id"] if receipt else None,
            }
        save_session(session, output_root)
        # An authentication/transport error must not create a storm of later calls.
        if error in {"provider_or_adapter_error", "cancelled"}:
            break
    write_text(output_root / session.session_id / "claim_reviews.json",
        json.dumps({"schema_version": "1.0", "reviews": session.claim_reviews}, indent=2) + "\n")
    save_session(session, output_root)
    return {"session": session}
