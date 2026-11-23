"""Assemble a profile without merging identities or inventing missing section content.

Validated batch statements retain their own citations and uncertainty. Exact repeats
are combined; distinct statements are kept, not rewritten by an unbounded final
model request. Conflicting identity decisions withhold affected findings. This is
not cross-source entity resolution or automatic semantic contradiction detection.
"""
from collections import defaultdict
from projectclarity.reporting.entity_profile.contracts import ProfileFinding


def combine_findings(findings, blocked):
    """Deduplicate exact statements while retaining evidence and identity uncertainty.

    Args:
        findings: Validated source-bound statements from bounded batch responses.
        blocked: Fact IDs whose identity assessments disagree across fragments.
    Returns:
        Ordered finding dictionaries. A finding touching any blocked fact is wholly
        withheld, not rewritten with fewer citations. Exact text/status repeats
        union citations; differing statements remain distinct. No I/O occurs.
    """
    combined = {}
    for finding in findings:
        if set(finding['fact_ids']) & blocked:
            continue
        key = (finding['text'], finding['status'])
        row = combined.setdefault(key, {**finding, 'fact_ids': []})
        row['fact_ids'] = sorted(set(row['fact_ids']) | set(finding['fact_ids']))
    return list(combined.values())


def assemble_profile(fragments, packet):
    """Preserve all requested sections and distinguish uncertainty from unreviewed work.

    Args:
        fragments: Independently validated batch outputs, including retained parents.
        packet: Frozen plan, full facts and requirements used for every batch.
    Returns:
        Profile result with cited overview, section findings, identity uncertainty,
        explicit exclusions and gaps. Empty sections are unknown. Disagreement about
        a fact's identity blocks its findings and is retained for inspection; facts
        lost from those findings remain unreviewed unless separately accounted for.
        No calls, alias inference, source edits or private file writes occur.
    """
    assessments = defaultdict(list)
    for fragment in fragments:
        for row in fragment['identity_assessments']:
            if row not in assessments[row['fact_id']]:
                assessments[row['fact_id']].append(row)
    blocked = {fid for fid, rows in assessments.items() if len({r['status'] for r in rows}) > 1}
    overview = combine_findings([f for part in fragments for f in part['summary']], blocked)
    sections = []
    gaps = []
    for requirement in packet['requirements']:
        findings = combine_findings([f for part in fragments for section in part['sections']
                                    if section['requirement_id'] == requirement['id'] for f in section['findings']], blocked)
        status = 'unknown' if not findings else 'conflicting' if any(f['status'] == 'conflicting' for f in findings) else 'supported'
        sections.append({'requirement_id': requirement['id'], 'title': requirement['name'],
                         'status': status, 'findings': findings})
        if not findings:
            gaps.append('Not established: ' + requirement['name'] + '.')
    uncertainty = [row for fid, rows in assessments.items() for row in rows if fid in blocked or row['status'] == 'ambiguous']
    if uncertainty:
        gaps.append('Some source evidence could not be attributed unambiguously to the requested subject and was withheld.')
    if not overview:
        gaps.append('No source-supported overview is available for the requested subject.')
    used = {fid for f in overview + [f for s in sections for f in s['findings']] for fid in f['fact_ids']}
    exclusions = {row['fact_id']: row for part in fragments for row in part['exclusions'] if row['fact_id'] not in used}
    for fid in blocked:
        exclusions[fid] = {'fact_id': fid, 'reason': 'Conflicting subject-identity assessments; findings withheld.'}
    return {'summary': overview, 'entries': [], 'gaps': gaps, 'exclusions': list(exclusions.values()),
            'profile': {'subject': packet['report_plan']['subject'], 'subject_kind': packet['report_plan']['subject_kind'],
                        'subject_scope': packet['report_plan']['subject_scope'], 'sections': sections,
                        'identity_uncertainty': uncertainty}}


def validate_profile(result, packet):
    """Check final profile scope, section completeness, citations and full accounting.

    Args:
        result: Locally assembled profile, not raw untrusted model output.
        packet: Exact saved plan and the subset of facts assessed by successful batches.
    Returns:
        Unchanged result on success; ValueError on scope drift, missing/duplicate
        sections, invalid findings or unaccounted facts. Checks references and
        structure, not semantic entailment or the correctness of identity judgments.
    """
    profile = result['profile']
    plan = packet['report_plan']
    if result['entries'] or any(profile[k] != plan[k] for k in ('subject', 'subject_kind', 'subject_scope')):
        raise ValueError('Profile identity must preserve the saved requested scope.')
    if [s['requirement_id'] for s in profile['sections']] != [r['id'] for r in packet['requirements']]:
        raise ValueError('Profile must preserve every requested section in order.')
    allowed = {f['id'] for f in packet['facts']}
    used = set()
    for finding in result['summary'] + [f for s in profile['sections'] for f in s['findings']]:
        row = ProfileFinding.model_validate(finding)
        if not row.text.strip() or not row.fact_ids or len(row.fact_ids) != len(set(row.fact_ids)) or not set(row.fact_ids) <= allowed:
            raise ValueError('Invalid final profile citation.')
        used.update(row.fact_ids)
    excluded = [r['fact_id'] for r in result['exclusions']]
    if len(excluded) != len(set(excluded)) or used & set(excluded) or used | set(excluded) != allowed:
        raise ValueError('Profile facts must be cited or explicitly excluded exactly once from exclusions.')
    return result
