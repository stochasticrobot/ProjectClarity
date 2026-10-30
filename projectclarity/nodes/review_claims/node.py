"""Ask the model whether proposed claims are supported by their original source passages.

The scheduler invokes this step for a batch of waiting claims after quotation
validation. Each request contains at most twelve claims from four passages and
must fit the shared input-size limit. The saved reservation records which claims
belong to the request before it is sent, so interruption cannot silently lose them
or trigger an automatic repeat of a request whose outcome is unknown.

Returned quotations are checked against each claim's own passage, not a neighboring
source in the same batch. Missing or invalid assessments remain explicit failures.
The later adjudication step rechecks source integrity and assigns final candidate
statuses. A successful model review still does not independently establish truth.
"""
import hashlib
import json
from collections import Counter

from projectclarity.claim_review.claim_review_messages import batch_review_messages
from projectclarity.claim_review.claim_review_contracts import ClaimReviewBatch
from projectclarity.claim_review.review_queue import MAX_REVIEW_CLAIMS, MAX_REVIEW_PASSAGES
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.extraction.source_quote_matching import QuoteMatchError, match_source_quote
from projectclarity.providers.structured_model_call_execution import invoke_structured, MAX_MESSAGE_CHARACTERS
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.session_storage import save_session, write_text


def failed_review(candidate, error, call_id=None):
    """Retain a claim-specific failure instead of dropping an unreviewable candidate.

    Args:
        candidate: Anchored candidate whose review did not yield a usable assessment.
        error: Application-owned reason, not raw provider exception text.
        call_id: Optional attempted review identifier; None means no model call.

    Returns:
        A failed review receipt with original source fingerprint and no verdict.
    """
    return {'validation_version': 'source-quotes-v2', 'assessment': None,
        'error_code': error, 'evidence': [], 'text_sha256': candidate.text_sha256,
        'call_id': call_id}


def prepare_review(session, record, output_root):
    """Pack complete claim/source contexts without exceeding response or input bounds.

    Args:
        session: Run whose existing review outcomes exclude already handled claims.
        record: Validated extraction record retaining original passage ownership.
        output_root: Run-root directory containing immutable source text snapshots.

    Returns:
        Ordered (attempt, candidates, verified text) groups for one call. Oversized
        single items and bad sources receive explicit failure receipts in session;
        other unselected candidates remain pending for later batches. Never trim
        passage text or silently discard claims to make a batch fit.
    """
    documents = {doc.source_id: doc for doc in session.documents}
    groups, count = [], 0
    for attempt in record.attempts:
        candidates = [c for c in record.candidates if c.batch_id == attempt.batch_id
            and c.evidence_status == 'anchored' and c.candidate_id not in session.claim_reviews]
        if not candidates:
            continue
        if len(groups) >= MAX_REVIEW_PASSAGES:
            break
        try:
            document = documents[attempt.source_id]
            if document.text_sha256 != attempt.text_sha256:
                raise EvidenceInputError('source_version_mismatch')
            text = read_verified_text(output_root / session.session_id, document)
        except (EvidenceInputError, KeyError) as exc:
            error = str(exc) if isinstance(exc, EvidenceInputError) else 'source_not_available'
            for candidate in candidates:
                session.claim_reviews[candidate.candidate_id] = failed_review(candidate, error)
            continue
        selected = []
        for candidate in candidates:
            if count >= MAX_REVIEW_CLAIMS:
                return groups
            trial = groups[:-1] + [(attempt, selected + [candidate], text)] if selected else groups + [(attempt, [candidate], text)]
            messages = batch_review_messages(session, trial)
            if len(json.dumps(messages, ensure_ascii=False)) > MAX_MESSAGE_CHARACTERS:
                if groups:
                    return groups
                session.claim_reviews[candidate.candidate_id] = failed_review(candidate, 'input_too_large')
                continue
            selected.append(candidate)
            groups = trial
            count += 1
    return groups


def review_claims(state, output_root, key_reader=read_api_key, monitor=None):
    """Review one source-bound batch and account for every candidate in its reservation.

    Args:
        state: Graph state containing the typed research session and pending evidence.
        output_root: Directory containing run subdirectories and saved source text.
        key_reader: Callable returning the API key only when a request is attempted.
        monitor: Optional progress/cancellation interface; None disables those hooks.

    Returns:
        Session update with checked factual assessments, separate report-mapping
        judgments, or explicit per-claim failures. Reference/completeness validation
        of mapping judgments occurs during adjudication and shared projection;
        invalid mapping references never overwrite a valid factual verdict.
        Makes at most one model call, preserves unselected pending claims and saves
        reservations/results atomically in session.json. No claims are published.
    """
    session = state['session']
    session.visited_nodes.append('review_claims')
    record = ExtractionRecord.model_validate(session.extraction)
    if session.status == 'cancelled':
        return {'session': session}
    groups = prepare_review(session, record, output_root)
    if groups:
        candidates = [c for _, rows, _ in groups for c in rows]
        assignments = {c.candidate_id: {'batch_id': attempt.batch_id, 'source_id': attempt.source_id,
            'text_sha256': attempt.text_sha256} for attempt, rows, _ in groups for c in rows}
        # Preserve familiar IDs for whole single passages; split/mixed batches use
        # a stable candidate-roster hash so no trace file is overwritten.
        whole = len(groups) == 1 and len(candidates) == sum(c.batch_id == groups[0][0].batch_id
            and c.evidence_status == 'anchored' for c in record.candidates)
        call_id = ('review_' + groups[0][0].batch_id if whole else
            'review_batch_' + hashlib.sha256(json.dumps(assignments, sort_keys=True).encode()).hexdigest()[:24])
        ledger = session.workflow.setdefault('review_batches', {})
        ledger[call_id] = {'status': 'reserved', 'assignments': assignments}
        save_session(session, output_root)
        answer, receipt, error = None, None, None
        try:
            if monitor:
                monitor.check_cancel()
            answer, receipt = invoke_structured(session, output_root, call_id=call_id,
                task='review_claims', prompt_version='claim-review-v6',
                messages=batch_review_messages(session, groups), schema=ClaimReviewBatch,
                key_reader=key_reader, monitor=monitor)
            error = receipt['error_code']
        except (RunCancelled, KeyboardInterrupt, EOFError):
            session.status, error = 'cancelled', 'cancelled'
            session.error = 'Claim review cancelled; prior work retained.'
        items = answer.assessments if answer is not None else []
        counts = Counter(item.candidate_id for item in items)
        if set(counts) - assignments.keys():
            error = 'review_candidate_id_mismatch'
        assessments = {item.candidate_id: item for item in items}
        for attempt, rows, text in groups:
            passage = text[attempt.start:attempt.end]
            for candidate in rows:
                candidate_id = candidate.candidate_id
                item = assessments.get(candidate_id)
                claim_error = error or ('review_candidate_id_mismatch' if counts[candidate_id] != 1 else None)
                anchors = []
                if item is not None and not claim_error:
                    for quote in item.evidence_quotes:
                        try:
                            match = match_source_quote(passage, quote)
                        except QuoteMatchError as exc:
                            claim_error = 'review_' + str(exc)
                        else:
                            anchors.append({'start': attempt.start + match.start,
                                'end': attempt.start + match.end, 'quote': match.quote,
                                'submitted_quote': quote, 'match_method': match.match_method})
                    if item.verdict != 'insufficient_evidence' and not item.evidence_quotes:
                        claim_error = 'review_missing_quote'
                result = failed_review(candidate, claim_error, call_id)
                if item is not None and not claim_error:
                    result.update(assessment=item.model_dump(mode='json'), evidence=anchors)
                session.claim_reviews[candidate_id] = result
        ledger[call_id].update(status='completed', error_code=error,
            failed_candidate_ids=[c.candidate_id for c in candidates
                if session.claim_reviews[c.candidate_id]['error_code']])
    write_text(output_root / session.session_id / 'claim_reviews.json',
        json.dumps({'schema_version': '1.0', 'reviews': session.claim_reviews}, indent=2) + '\n')
    save_session(session, output_root)
    return {'session': session}
