"""Coverage-specific recovery invariants over the shared response-correction path.

Invalid summaries stay untrusted. Their explicit caveats and true uncertainty
flags cannot disappear merely because a correction parses successfully. Exact
caveats are packed into the existing bounded schema, or recovery fails closed.
The scheduler checks current content-addressed nodes to avoid retry loops without
blocking unrelated new evidence or accepting source integrity on metadata alone.
"""
from projectclarity.coverage import bounded_tree as tree
from projectclarity.coverage.bounded_contracts import BoundedDigest

RECOVERABLE_ERRORS = frozenset({'invalid_output_schema', 'bounded_unknown_witness',
    'bounded_missing_witness', 'bounded_input_accounting_mismatch', 'bounded_cleared_child_blocker'})


def preserve_caveats(answer, previous):
    """Retain exact rejected-response caveats and uncertainty in a valid correction.

    Args:
        answer: Structurally valid corrected BoundedDigest, not yet accepted.
        previous: Parsed rejected object; never evidence or trusted witness IDs.
    Returns:
        New bounded digest with exact distinct caveat strings retained and true
        flags preserved. Raises ValueError if preservation cannot fit; never clips,
        paraphrases or drops caveats. Packing may conservatively retain a mistaken
        model caveat and therefore does not establish semantic correctness.
    """
    old = previous.get('limitations', [])
    if not isinstance(old, list) or any(not isinstance(x, str) for x in old):
        raise ValueError('bounded_recovery_invalid_caveats')
    values = list(dict.fromkeys([*old, *answer.limitations]))
    bins = []
    for value in values:
        if len(value) > 500:
            raise ValueError('bounded_recovery_caveats_do_not_fit')
        for index, existing in enumerate(bins):
            if len(existing) + 1 + len(value) <= 500:
                bins[index] = existing + '\n' + value
                break
        else:
            bins.append(value)
    if len(bins) > 8:
        raise ValueError('bounded_recovery_caveats_do_not_fit')
    data = answer.model_dump(mode='json')
    data.update(limitations=bins)
    for flag in tree.FLAGS:
        data[flag] = data[flag] or previous.get(flag) is True
    return BoundedDigest.model_validate(data)


def scheduling_blocker(session):
    """Suppress terminal summary work only while its exact node remains required.

    Args:
        session: Current persisted run; no filesystem access or mutation occurs.
    Returns:
        A diagnostic blocker or None. Reconstructs only when terminal recovery
        receipts exist. Changed node inputs permit new work; uncertain outcomes
        remain globally blocked even after repartitioning. Source integrity is
        still checked by production assessment before any result can be accepted.
    """
    receipts = session.coverage.get('evidence_batches', {})
    if any(k.startswith('BT_') and r.get('status') in {'uncertain', 'reserved'}
            for k, r in receipts.items()):
        return 'bounded_summary_requires_recovery'
    blocked = {k for k, r in receipts.items() if r.get('recovery_state') == 'blocked'}
    if not blocked:
        return None
    from projectclarity.extraction.extraction_contracts import ExtractionRecord
    from projectclarity.coverage.bounded_assessment import (
        context, initial_payload, MAX_DIRECT_INPUTS, SYSTEM_PROMPT)
    record = ExtractionRecord.model_validate(session.extraction)
    valid = {c.candidate_id: c for c in record.candidates if c.evidence_status == 'anchored' and c.evidence}
    try:
        fixed = context(session)
        if len(valid) <= MAX_DIRECT_INPUTS:
            from projectclarity.coverage.result_inventory import coverage_results
            from projectclarity.coverage.coverage_contracts import CoverageAssessment
            from projectclarity.nodes.assess_research_gaps.node import SYSTEM_PROMPT as FINAL_PROMPT
            from projectclarity.discovery.inventory_context import payload_messages
            from projectclarity.task_inputs import input_receipt
            payload, _ = initial_payload(session, len(record.candidates), valid,
                fixed, coverage_results(session, valid))
            payload['candidates'] = [tree.candidate_input(session, c) for c in valid.values()]
            payload['sources'] = {row['source_id']: row.pop('source_context') for row in payload['candidates']}
            if not input_receipt('bounded_coverage', payload_messages(FINAL_PROMPT, payload), CoverageAssessment)['errors']:
                return None
        graph = tree.plan_tree(session, valid, fixed, SYSTEM_PROMPT, BoundedDigest, receipts)
    except (ValueError, KeyError):
        return None
    return 'bounded_summary_recovery_exhausted' if any(
        node['batch_id'] in blocked for node in graph['pending']) else None
