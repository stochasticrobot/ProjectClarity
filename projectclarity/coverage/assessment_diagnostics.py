"""Record coverage decisions without changing prompts or claiming semantic validation.

The contract assessor stores these small snapshots in coverage history. Existing
private model traces own full input/output text; these records link that evidence
to local backlog, search admission and validator outcomes. Counts and hashes are
observations, not proof that the model read a reference or ignored a document.
No helper sends requests, resolves identities or changes scheduling policy.
"""
from collections import Counter
from copy import deepcopy
import hashlib


def query_key(query):
    """Use the existing exact query equivalence rule, never semantic similarity.

    Args:
        query: Search text to compare, not a URL or credential.
    Returns:
        Case-folded whitespace-normalized string; wording differences remain distinct.
    """
    return ' '.join(query.split()).casefold()


def query_fingerprint(query):
    """Link queue decisions to saved queries without copying text into diagnostics.

    Args:
        query: Search text already retained in private application receipts.
    Returns:
        SHA-256 of the exact-comparison key. This is not anonymization or encryption.
    """
    return hashlib.sha256(query_key(query).encode()).hexdigest()


def begin_assessment(payload, normalization):
    """Snapshot the evidence-work inventory at preparation, before provider dispatch.

    Args:
        payload: Complete pre-encoding coverage input; no values are modified.
        normalization: Existing size/encoding/hash receipt for the input.
    Returns:
        Independent JSON diagnostics with per-source work counts and raw totals.
        Offered IDs permit exact reference-failure diagnostics. Declared and effective checks expose inherited requirements without changing
        them. Alias sources are labeled; totals describe supplied rows, not deduplicated
        reading opportunities. Source bodies and model instructions are not copied.
    """
    rows = [{key: deepcopy(row.get(key)) for key in (
        'source_id', 'shared_extraction_source_id', 'acquisition_status',
        'passage_count', 'unread_passages', 'pending_review')}
        for row in payload.get('acquired_work', [])]
    return dict(version='coverage-diagnostics-v1', stage='prepared',
        source_work=rows, unread_passages=sum(row['unread_passages'] or 0 for row in rows),
        pending_reviews=sum(row['pending_review'] or 0 for row in rows),
        normalization=deepcopy(normalization), validation_status='not_run',
        search_decisions=[], warnings=[],
        offered_candidate_ids=sorted({row['candidate_id'] for row in
            payload.get('evidence_registry', payload.get('candidates', []))}),
        effective_criteria=deepcopy(payload.get('criteria', [])),
        declared_criteria=deepcopy((payload.get('answer_schema') or {}).get('completion_checks', [])),
        section_requirements=[{key: deepcopy(section.get(key)) for key in
            ('goal_id', 'format', 'unit', 'evidence_requirements')}
            for section in (payload.get('answer_schema') or {}).get('sections', [])])


def finish_assessment(diagnostics, answer, checked=None, error=None):
    """Retain raw-versus-accepted decisions and explicit non-semantic risk indicators.

    Args:
        diagnostics: Mutable history-owned snapshot from begin_assessment.
        answer: Parsed CoverageAssessment, or None for provider/preflight failure.
        checked: Validator rows after successful validation; None means not accepted.
        error: Existing error code, or None. Full provider errors remain in linked traces.
    Returns:
        None. Updates only diagnostics, never judgments, queues, budgets or prompts.
        Backlog plus searches is flagged for inspection, not called model neglect.
        Population receipts distinguish numeric proposals from benchmark support.
        Reference and result-binding failures retain the actual offending IDs.
    """
    diagnostics.update(stage='finished', error_code=error,
        validation_status='accepted' if checked is not None else 'rejected' if answer is not None else 'not_run')
    accepted = {row['criterion_id']: row for row in checked or []}
    declared = {row['criterion_id']: row for row in diagnostics.get('effective_criteria', [])}
    offered = set(diagnostics.get('offered_candidate_ids', []))
    diagnostics['criteria'] = [dict(criterion_id=row.criterion_id,
        proposed_judgment=row.judgment, proposed_target_count=row.target_count,
        gap_count=len(row.gaps), benchmark_count=len(row.benchmark_candidate_ids),
        accepted_status=accepted.get(row.criterion_id, {}).get('status'),
        population=deepcopy(accepted.get(row.criterion_id, {}).get('population')),
        reference_normalization=deepcopy(accepted.get(row.criterion_id, {}).get('reference_normalization', {})),
        non_candidate_input_ids=sorted(set(row.benchmark_candidate_ids + row.evidence_candidate_ids +
            [cid for item in row.items for cid in item.candidate_ids]) - offered),
        proposed_result_goal_id=row.result_goal_id,
        expected_result_goal_id=declared.get(row.criterion_id, {}).get('result_goal_id'),
        blocking_reasons=accepted.get(row.criterion_id, {}).get('blocking_reasons', []))
        for row in answer.assessments] if answer is not None else []
    if answer is not None and answer.next_searches and (
            diagnostics['unread_passages'] or diagnostics['pending_reviews']):
        diagnostics['warnings'].append('search_proposed_with_local_backlog')
    if answer is not None and any(row.judgment == 'satisfied' and row.gaps for row in answer.assessments):
        diagnostics['warnings'].append('satisfied_proposal_has_gaps')


def scheduling_snapshot(session, available):
    """Explain actual reading/search/coverage admission from computed scheduler state.

    Args:
        session: Run after the controller chose its phase, before task dispatch.
        available: Eligible tasks before controller prioritization, not blocked work.
    Returns:
        Text-free compact counts, recent gain allocation and controller reason. This observes existing policy;
        unread counts alone do not mean a task was runnable or relevant to a gap.
    """
    controller = session.workflow.get('answer_controller', {})
    attempts = (session.extraction or {}).get('attempts', [])
    return dict(version='coverage-scheduling-v1',
        eligible_actions=dict(Counter(task['action'] for task in available)),
        controller_policy=session.workflow.get('controller_policy'),
        phase=controller.get('phase'), cycle=controller.get('cycle'), reason=controller.get('reason'),
        reading_allocation=controller.get('reading_allocation'),
        unread_passages=sum(row['status'] == 'not_attempted' for row in attempts),
        coverage_round=len(session.coverage.get('history', [])),
        model_requests_before=session.usage.requests_attempted)
