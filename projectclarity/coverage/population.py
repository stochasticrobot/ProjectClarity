"""Describe benchmark support independently of report-field completeness.

These receipts validate reference status and scope, not whether a quotation
actually entails a total. Population interpretation remains a model judgment.
"""

POPULATION_POLICY = 'population-benchmark-and-exact-reference-v1'


def population_receipt(criterion, assessment, offered, allowed_goals):
    """Separate a proposed population from the quality of its benchmark references.

    Args:
        criterion: Accepted completion criterion defining the target basis.
        assessment: Parsed model assessment with a possibly unknown target.
        offered: Source-checked candidate mapping; referenced IDs already validated.
        allowed_goals: Goals explicitly associated with this criterion.
    Returns:
        JSON receipt retaining the proposed total, basis and reference defects.
        Evidence-backed means structurally supported, not verified entailment.
        No input is mutated and missing totals are never reconstructed.
    """
    ids = sorted(set(assessment.benchmark_candidate_ids))
    unsupported = [cid for cid in ids if offered[cid].adjudication_status != 'supported']
    outside = [cid for cid in ids if not set(offered[cid].proposal.goal_ids) & allowed_goals]
    if criterion.target_basis == 'not_applicable':
        state = 'not_applicable'
    elif criterion.target_basis == 'user_request':
        state = 'requested'
    elif assessment.target_count is None:
        state = 'unknown'
    else:
        state = 'unverified' if not ids or unsupported or outside else 'model_asserted_supported_benchmark'
    return dict(basis=criterion.target_basis, state=state,
        proposed_count=assessment.target_count, benchmark_candidate_ids=ids,
        unsupported_candidate_ids=unsupported, out_of_scope_candidate_ids=outside)
