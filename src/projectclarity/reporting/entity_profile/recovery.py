"""Keep usable profile findings when other parts of a model reply are invalid.

Checks each identity assessment and cited finding against the supplied records.
Invalid contributions remain in the saved reply with diagnostic reasons; missing
facts remain eligible for bounded recovery rather than being silently excluded.
These checks preserve references and quotations, not the truth of model judgments.
"""
import json
from collections import Counter
from projectclarity.reporting.entity_profile.contracts import IdentityAssessment, ProfileFinding


def unique_object(pairs):
    """Reject ambiguous JSON objects before any field-level recovery.

    Args: pairs: Ordered JSON object key/value pairs from the decoder.
    Returns: Dictionary; duplicate keys raise ValueError without choosing a value.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate profile JSON key.')
        result[key] = value
    return result


def inspect_profile(text, packet, facts):
    """Retain independently checked contributions and expose unresolved fact IDs.

    Args:
        text: Original provider JSON, never rewritten or repaired.
        packet: Frozen requirements and requested identity.
        facts: Exact records offered to this task.
    Returns:
        Checked fragment and private recovery diagnostics. Invalid individual
        assessments, sections and findings are quarantined. Duplicate assessments
        invalidate every assessment for that ID. Rejected findings cannot become
        exclusions through unused IDs. Unknown IDs, invented quotes and uncertain
        citations never pass. Invalid JSON or envelope raises ValueError. These
        mechanical checks do not establish semantic entailment or real identity.
    """
    raw = json.loads(text, object_pairs_hook=unique_object)
    keys = {'identity_assessments', 'overview', 'sections', 'unused_fact_ids'}
    if not isinstance(raw, dict) or set(raw) != keys or any(not isinstance(raw[k], list) for k in keys):
        raise ValueError('Invalid profile response envelope.')
    allowed = {f['id']: f for f in facts}
    required = {r['id'] for r in packet['requirements']}
    issues, assessments = [], {}
    counts = Counter(x.get('fact_id') for x in raw['identity_assessments']
                     if isinstance(x, dict) and isinstance(x.get('fact_id'), str))
    for index, candidate in enumerate(raw['identity_assessments']):
        try:
            row = IdentityAssessment.model_validate(candidate).model_dump()
            fid, quote = row['fact_id'], row['identity_quote']
            if fid not in allowed or counts[fid] != 1 or not row['reason'].strip():
                raise ValueError('Invalid or repeated identity assessment.')
            if row['status'] == 'match' and (not quote or not quote.strip()):
                raise ValueError('Matching identity requires a source excerpt.')
            if quote is not None and (not quote.strip() or not any(quote in e['quote'] for e in allowed[fid]['evidence'])):
                raise ValueError('Identity quote is not in supplied evidence.')
            assessments[fid] = row
        except ValueError as error:
            issues.append({'location': f'identity_assessments[{index}]', 'reason': str(error)})
    used, rejected = set(), set()

    def findings(candidates, location):
        """Check each finding independently, retaining only complete valid citations.

        Args: candidates: Raw finding list; location: Audit location in the reply.
        Returns: Checked findings; updates local used/rejected IDs and diagnostics.
            No citation is removed to make a faulty sentence appear supported.
        """
        accepted = []
        for index, candidate in enumerate(candidates):
            try:
                row = ProfileFinding.model_validate(candidate).model_dump()
                ids = row['fact_ids']
                if not row['text'].strip() or not ids or len(ids) != len(set(ids)):
                    raise ValueError('Finding requires text and distinct citations.')
                if any(fid not in assessments or assessments[fid]['status'] != 'match' for fid in ids):
                    raise ValueError('Finding cites absent or uncertain identity.')
                used.update(ids)
                accepted.append(row)
            except ValueError as error:
                if isinstance(candidate, dict) and isinstance(candidate.get('fact_ids'), list):
                    rejected.update(fid for fid in candidate['fact_ids'] if isinstance(fid, str) and fid in allowed)
                issues.append({'location': f'{location}[{index}]', 'reason': str(error)})
        return accepted

    summary = findings(raw['overview'], 'overview')
    sections = []
    section_counts = Counter(s.get('requirement_id') for s in raw['sections']
                             if isinstance(s, dict) and isinstance(s.get('requirement_id'), str))
    for index, section in enumerate(raw['sections']):
        if (not isinstance(section, dict) or set(section) != {'requirement_id', 'findings'}
                or not isinstance(section.get('requirement_id'), str)
                or section['requirement_id'] not in required or section_counts[section['requirement_id']] != 1 or not isinstance(section.get('findings'), list)):
            issues.append({'location': f'sections[{index}]', 'reason': 'Invalid section envelope or requirement.'})
            if isinstance(section, dict) and isinstance(section.get('findings'), list):
                for candidate in section['findings']:
                    if isinstance(candidate, dict) and isinstance(candidate.get('fact_ids'), list):
                        rejected.update(fid for fid in candidate['fact_ids'] if isinstance(fid, str) and fid in allowed)
            continue
        sections.append({'requirement_id': section['requirement_id'],
                         'findings': findings(section['findings'], f'sections[{index}].findings')})
    unused = set()
    for fid in raw['unused_fact_ids']:
        if not isinstance(fid, str) or fid not in assessments or fid in unused:
            issues.append({'location': 'unused_fact_ids', 'reason': 'Unused fact lacks a unique valid assessment.'})
            continue
        unused.add(fid)
    withheld = {fid for fid, row in assessments.items() if row['status'] != 'match'}
    excluded = ((unused - rejected) | withheld) - used
    return {'quarantined_fields': issues, 'summary': summary, 'sections': sections, 'identity_assessments': list(assessments.values()),
            'exclusions': [{'fact_id': fid, 'failure_categories': assessments[fid].get('failure_categories', []), 'reason': assessments[fid]['reason'] if fid in withheld
                            else 'Assessed subject evidence does not answer the requested profile sections.'}
                           for fid in sorted(excluded)],
            '_recovery': {'missing_fact_ids': [fid for fid in allowed if fid not in used | excluded],
                          'quarantined_fields': issues}}
