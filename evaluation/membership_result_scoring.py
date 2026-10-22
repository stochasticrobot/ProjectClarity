"""Compare saved membership relationships with a dated, independent reference set.

This scores structured MEMBER_OF and REPRESENTS triples, not name mentions in
arbitrary prose. It reports anchored and model-supported results separately,
keeps missed/extra triples visible, and deduplicates repeated source findings.
The aliases are explicit reference data, not fuzzy name matching.

These are relation-reference metrics, NOT a semantic judge or citation-faithfulness
score. A model could attach a correct-looking triple to contradictory prose;
that still needs semantic evaluation. Reference verification is recorded separately
from model-output quality; a verified answer key does not make a model run correct.
"""
from collections import Counter
import unicodedata

from evaluation.membership_reference_cases import reference_validation
from evaluation.multi_source_membership_check import check_membership_result
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.research_schemas import ResearchSession


def normalize_name(value):
    """Ignore case and layout whitespace, but do not guess identities or remove accents."""
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def reference_relations(suite, case):
    """Build canonical expected triples and the explicitly permitted name aliases."""
    aliases = {}
    for canonical, alternatives in suite['entity_aliases'].items():
        for name in [canonical, *alternatives]:
            aliases[normalize_name(name)] = canonical
    expected = set()
    for member in case['expected_members']:
        for name in [member['name'], *member['aliases']]:
            aliases[normalize_name(name)] = member['name']
        for predicate, field in [('MEMBER_OF', 'chamber'), ('REPRESENTS', 'region')]:
            aliases[normalize_name(member[field])] = member[field]
            expected.add((member['name'], predicate, member[field]))
    return expected, aliases


def relation_metrics(expected, observed):
    """Return inspectable set comparisons; empty predictions have undefined precision, not 100%."""
    correct = expected & observed
    precision = len(correct) / len(observed) if observed else None
    recall = len(correct) / len(expected) if expected else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision is not None and recall is not None and precision + recall else 0.0)
    return {'expected_count': len(expected), 'predicted_count': len(observed),
        'correct_count': len(correct), 'precision': precision, 'recall': recall, 'f1': f1,
        'matched': sorted(correct), 'missing': sorted(expected - observed),
        'unexpected': sorted(observed - expected)}


def score_session(suite, case, session, verified_sources):
    """Score one case-matched session using independently hash-checked source text.

    verified_sources maps source IDs to (hash, original saved text). Missing,
    altered or invalid evidence never enters either metric. A supported label
    also needs its successful in-scope review record, not just an entity mention.
    """
    if session['request']['question'] != case['question']:
        raise ValueError('This run used a different research question; refusing to score the wrong case.')
    expected_urls = {s['url'] for s in case['sources']}
    supplied_urls = {s['url'] for s in session['sources'] if s['kind'] == 'url'}
    if supplied_urls != expected_urls or any(s['kind'] != 'url' for s in session['sources']):
        raise ValueError('This run used different sources; use the matching evaluation case.')
    expected, aliases = reference_relations(suite, case)
    extraction = session.get('extraction') or {}
    batches = {a['batch_id']: a for a in extraction.get('attempts', [])}
    source_urls = {s['source_id']: s['url'] for s in session['sources']}
    anchored, supported, ignored = set(), set(), Counter()
    relation_claims = []
    for candidate in extraction.get('candidates', []):
        relation = candidate['proposal'].get('relationship')
        if not relation or relation['predicate'].upper().replace(' ', '_') not in {'MEMBER_OF', 'REPRESENTS'}:
            ignored['not_a_requested_relationship'] += 1
            continue
        source = verified_sources.get(candidate['source_id'])
        evidence = candidate['evidence']
        if (candidate['evidence_status'] != 'anchored' or not source or not evidence
                or candidate['text_sha256'] != source[0] or any(
                    e['source_id'] != candidate['source_id'] or e['text_sha256'] != source[0]
                    or not (0 <= e['start'] < e['end'] <= len(source[1]))
                    or not e['quote'].strip() or source[1][e['start']:e['end']] != e['quote'] for e in evidence)):
            ignored['invalid_or_unavailable_evidence'] += 1
            continue
        batch = batches.get(candidate['batch_id'], {}).get('result') or {}
        entities = {e['entity_id']: e['name'] for e in batch.get('entities', [])}
        if relation['subject_id'] not in entities or relation['object_id'] not in entities:
            ignored['invalid_entity_reference'] += 1
            continue
        subject, obj = entities[relation['subject_id']], entities[relation['object_id']]
        triple = (aliases.get(normalize_name(subject), normalize_name(subject)),
            relation['predicate'].upper().replace(' ', '_'), aliases.get(normalize_name(obj), normalize_name(obj)))
        anchored.add(triple)
        review = session.get('claim_reviews', {}).get(candidate['candidate_id']) or {}
        assessment = review.get('assessment') or {}
        is_supported = (candidate.get('adjudication_status') == 'supported'
            and not review.get('error_code') and review.get('text_sha256') == source[0]
            and assessment.get('verdict') == 'supported' and assessment.get('in_scope') is True)
        if is_supported:
            supported.add(triple)
        relation_claims.append({'candidate_id': candidate['candidate_id'], 'relationship': triple,
            'source_id': candidate['source_id'], 'source_url': source_urls.get(candidate['source_id']),
            'model_supported': is_supported, 'matches_reference': triple in expected})
    documents = session.get('documents', [])
    result = {'case_id': case['case_id'], 'reference_date': suite['reference_date'],
        'human_reference_review': suite.get('human_reference_review', 'not_recorded'),
        'reference_validation': reference_validation(suite), 'run_id': session['session_id'],
        'run_status': session['status'], 'usage': session['usage'],
        'anchored_relationships': relation_metrics(expected, anchored),
        'supported_relationships': relation_metrics(expected, supported),
        'ignored_candidate_counts': dict(ignored), 'scored_candidates': relation_claims,
        'sources': [{'source_id': d['source_id'], 'status': d['status'], 'error_code': d.get('error_code'),
                    'acquired_at': d.get('acquired_at'), 'text_sha256': d.get('text_sha256')} for d in documents],
        'source_coverage_complete': len(documents) == len(expected_urls) and all(d['status'] == 'retrieved' for d in documents),
        'unfinished_extraction': sum(a['status'] != 'completed' for a in extraction.get('attempts', [])),
        'extraction_source_errors': extraction.get('source_errors', []),
        'verified_source_count': len(set(verified_sources) & set(source_urls)),
        'unfinished_reviews': sum(c['evidence_status'] == 'anchored' and not (
            (session.get('claim_reviews', {}).get(c['candidate_id']) or {}).get('assessment'))
            for c in extraction.get('candidates', [])),
        'review_errors': dict(Counter(v['error_code'] for v in session.get('claim_reviews', {}).values() if v.get('error_code'))),
        'semantic_correctness': 'not_automatically_scored', 'citation_faithfulness': 'not_automatically_scored',
        'note': 'Reference relationship comparison only. Inspect prose, scope and temporal support; this is not a published-facts score.'}
    if case.get('acceptance'):
        result['acceptance'] = check_membership_result(case, result)
    return result


def score_saved_run(suite, case, directory):
    """Verify saved source hashes before scoring; report corrupt/missing files separately."""
    session = ResearchSession.model_validate_json((directory / 'session.json').read_text(encoding='utf-8'))
    sources, failures = {}, {}
    for document in session.documents:
        if document.status != 'retrieved':
            continue
        try:
            sources[document.source_id] = (document.text_sha256, read_verified_text(directory, document))
        except EvidenceInputError as exc:
            failures[document.source_id] = str(exc)
    result = score_session(suite, case, session.model_dump(mode='json'), sources)
    result['source_integrity_errors'] = failures
    return result
