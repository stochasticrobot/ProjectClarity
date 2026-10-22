"""Check whether one research run combined sources into the exact expected roster.

Five names alone are not enough: they must be the right five people, with the
expected chamber and represented area, backed by reviewed source evidence.
Repeated names and explicit aliases count once. This is an answer-key check,
not a semantic judge: it cannot prove that a quotation means what a model says.
Only cases that opt into this check get a pass/fail result and a test exit code.
"""


def check_membership_result(case, score):
    """Compare the combined, reviewed output with the case's exact expectations.

    Count contributing source URLs, not fetched pages or repeated candidate IDs.
    A source contributes only if it supports a relationship in the answer key.
    Incomplete acquisition/extraction/review is reported separately from a
    completed run whose answer is wrong; neither can pass this built-in test.
    """
    policy = case['acceptance']
    expected_people = {member['name'] for member in case['expected_members']}
    supported = [c for c in score['scored_candidates'] if c['model_supported']]
    observed_people = {c['relationship'][0] for c in supported}
    contributing_urls = sorted({c['source_url'] for c in supported
                                if c['matches_reference'] and c['source_url']})
    metric = score['supported_relationships']
    checks = {
        'exact_people': observed_people == expected_people,
        'exact_relationships': not metric['missing'] and not metric['unexpected'],
        'multiple_sources_contributed': len(contributing_urls) >= policy['minimum_contributing_sources'],
    }
    incomplete = []
    if not score['source_coverage_complete']:
        incomplete.append('source_acquisition_incomplete')
    if score['verified_source_count'] != len({s['url'] for s in case['sources']}):
        incomplete.append('source_evidence_unavailable')
    if score['run_status'] != 'candidates_saved':
        incomplete.append('research_not_complete')
    if score['unfinished_extraction'] or score['extraction_source_errors']:
        incomplete.append('extraction_incomplete')
    if score['unfinished_reviews'] or score['review_errors']:
        incomplete.append('review_incomplete')
    status = 'incomplete' if incomplete else 'passed' if all(checks.values()) else 'failed'
    return {'status': status, 'checks': checks, 'incomplete_reasons': incomplete,
        'expected_people_count': len(expected_people), 'observed_people_count': len(observed_people),
        'matched_people': sorted(expected_people & observed_people),
        'missing_people': sorted(expected_people - observed_people),
        'unexpected_people': sorted(observed_people - expected_people),
        'minimum_contributing_sources': policy['minimum_contributing_sources'],
        'contributing_source_count': len(contributing_urls), 'contributing_source_urls': contributing_urls,
        'scope': 'Exact reviewed reference relationships across multiple sources; not semantic faithfulness.'}
