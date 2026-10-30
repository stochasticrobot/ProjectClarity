"""Keep a cumulative record of proposed claims for each research-plan question.

A candidate is a statement proposed during extraction, before acceptance as a
supported finding. refresh_goal_register records candidates, their source and
quotation checks, review statuses and status history in goal_register.json. Claims
missing from a later snapshot remain marked as historical rather than disappearing.

The register lets progress displays and model tasks see what has been found so
far even when one assessment considers only some evidence. Counts refer to claims:
five claims about one researcher are not five researchers. This module saves the
register but does not resolve identities, review claims or declare goals complete.
"""
from collections import Counter
import json

from projectclarity.session_storage import write_text


def refresh_goal_register(session, output_root):
    """Merge all candidate facts into a durable register without dropping deferred evidence.

    Args:
        session: Run with extraction records, goals and any previous register snapshot.
        output_root: Local run root for a separate goal_register.json projection.

    Returns:
        None. Update session.coverage.goal_register and its JSON file. Existing entries
        absent from a later snapshot remain as retained_not_in_current_snapshot, not
        supported current evidence. Status changes append revisions. Counts refer to
        candidate facts, never distinct people/organizations or satisfied criteria.
    """
    if not session.plan:
        return
    register = session.coverage.setdefault('goal_register', {'schema_version': 1,
        'count_basis': 'source_scoped_candidate_facts_not_distinct_entities', 'facts': {}})
    facts = register['facts']
    present = set()
    for candidate in session.extraction.get('candidates', []):
        cid = candidate['candidate_id']
        present.add(cid)
        claim = candidate['proposal']
        current = {'candidate_id': cid, 'source_id': candidate['source_id'],
            'statement': claim['statement'], 'goal_ids': list(claim['goal_ids']),
            'goal_association': 'model_proposed', 'research_roles': claim.get('research_roles', []),
            'qualifications': claim.get('qualifications', []), 'evidence': candidate.get('evidence', []),
            'evidence_status': candidate['evidence_status'],
            'review_status': candidate.get('review_status', 'not_reviewed'),
            'adjudication_status': candidate.get('adjudication_status', 'needs_review'),
            'adjudication_reason': candidate.get('adjudication_reason'),
            'validation_errors': candidate.get('validation_errors', []), 'availability': 'current',
            'verification_scope': 'claim_support_only_not_goal_completion'}
        previous = facts.get(cid)
        history = list(previous.get('revisions', [])) if previous else []
        if previous and any(previous.get(k) != v for k, v in current.items()):
            history.append({k: previous.get(k) for k in ('evidence_status', 'review_status',
                'adjudication_status', 'adjudication_reason', 'availability')})
        facts[cid] = {**current, 'revisions': history}
    for cid, fact in facts.items():
        if cid not in present:
            fact['availability'] = 'retained_not_in_current_snapshot'
    register['goals'] = []
    for goal in session.plan.goals:
        entries = [f for f in facts.values() if goal.goal_id in f['goal_ids']]
        current = [f for f in entries if f['availability'] == 'current']
        register['goals'].append({'goal_id': goal.goal_id, 'question': goal.question,
            'candidate_ids': [f['candidate_id'] for f in entries], 'discovered_facts': len(entries),
            'current_facts': len(current),
            'evidence_anchored': sum(f['evidence_status'] == 'anchored' for f in current),
            'review_outcomes': dict(Counter(f['adjudication_status'] for f in current)),
            'criterion_verification': 'see_separate_coverage_assessments'})
    register['total_facts'] = len(facts)
    write_text(output_root / session.session_id / 'goal_register.json',
        json.dumps(register, indent=2, ensure_ascii=False) + '\n')


def goal_progress_summary(session):
    """Return per-goal counts from the saved cumulative claim register.

    These small summaries let model tasks see the amount and status of recorded work
    without copying all claim text. Counts alone are not evidence of the statements
    or a count of distinct answer items.

    Args:
        session: Run whose register is refreshed when extraction/review records are saved.

    Returns:
        Small goal-count summaries for research prompts. No statements or unseen evidence
        IDs are added; callers must still supply actual evidence before asking for judgment.
    """
    register = session.coverage.get('goal_register', {})
    return {'count_basis': 'candidate_facts_not_entities_or_completed_goals',
        'total_facts': register.get('total_facts', 0),
        'goals': [{k: v for k, v in goal.items() if k not in {'candidate_ids', 'question'}}
            for goal in register.get('goals', [])]}
