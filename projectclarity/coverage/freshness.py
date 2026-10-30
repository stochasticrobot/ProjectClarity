"""Label saved coverage judgments as current, stale or unverifiable.

Dashboard and answer displays compare the assessment's saved evidence fingerprint
with the current run. For example, adding reviewed claims after an assessment
makes that assessment stale even if it previously said every criterion passed.
This module reports the mismatch without rerunning the model, altering history,
or treating numerical population estimates as proof of completion."""

from projectclarity.coverage.stopping import evidence_revision


def coverage_freshness(session):
    """Compare the last assessment's evidence fingerprint with this run's state.

    Args:
        session: Typed research run containing its own evidence and coverage history.

    Returns:
        Status and observed assessment/current candidate counts. An older assessment
        without a fingerprint is unverified, never assumed current. This pure check
        does not rerun assessment, infer completeness, or mutate saved history.
    """
    coverage = session.coverage
    history = coverage.get('history', [])
    last = history[-1] if history else {}
    status = coverage.get('assessment_status', 'not_assessed')
    if status == 'assessed':
        revision = last.get('evidence_revision')
        status = ('fresh' if session.plan and revision == evidence_revision(session)
                  else 'stale' if revision else 'freshness_unknown')
    successful = next((h for h in reversed(history) if h.get('status') == 'assessed'), {})
    return {'status': status, 'last_successful_round': successful.get('round'),
        'last_successful_is_current': bool(session.plan and successful.get('evidence_revision') == evidence_revision(session)), 'assessment_round': last.get('round'),
        'assessed_candidate_count': last.get('candidate_total'),
        'offered_candidate_count': len(last.get('offered_candidate_ids', [])),
        'current_candidate_count': len((session.extraction or {}).get('candidates', [])),
        'assessment_was_partial': bool(last.get('inventory_truncated'))}
