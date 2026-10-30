"""Choose a small set of pages for the preliminary study of answer scope.

The study combines user-supplied source references with search results into one
candidate list. A model request chooses pages useful for understanding what the
question requires; Python verifies the selected IDs and any explicit requirement
links before acquisition. Neither source origin receives its own guaranteed quota.
Selection spends the existing model-call allowance and does not itself prove that
the selected pages are relevant, correct or representative.
"""
import json

from projectclarity.discovery.observed_link_inventory import canonical_url
from projectclarity.discovery.search_result_urls import normalize_search_result_url
from projectclarity.discovery.url_policy import url_denial
from projectclarity.nodes.study_coverage.contracts import CoverageStudySelection, ContractStudySelection
from projectclarity.nodes.web_search.node import save_search, search_source_limit
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.research_schemas import URLSource


def probe_candidates(session):
    """Combine all supplied references and eligible probe results without origin quotas.

    Args:
        session: Run with a study plan and its saved query/result inventory.
    Returns:
        Candidate dictionaries and explicit exclusions. Canonically identical URLs
        share a candidate, including result titles/snippets for supplied URLs. No
        prefix truncation, registration, acquisition or model call occurs. Metadata
        remains untrusted lead information, not source evidence.
    """
    study = session.coverage_study
    pool, exclusions = {}, []
    queries = {q['query_id']: q for q in session.search.get('queries', [])
        if q['query_id'] in study['query_ids']}
    for source in session.sources:
        row = {'candidate_id': source.source_id, 'source_id': source.source_id,
            'kind': source.kind, 'search_results': []}
        if source.kind == 'url':
            url, error = normalize_search_result_url(source.url)
            error = error or url_denial(url, session.request.url_policy)
            if error:
                exclusions.append({'candidate_id': source.source_id, 'reason': error})
                continue
            row['url'] = url
            # Organic wrappers need a destination registration, not a fetch of the wrapper.
            if url != canonical_url(source.url):
                row['source_id'] = None
            key = ('url', url)
        else:
            row['path'] = source.path
            key = ('file', source.path)
        existing = pool.setdefault(key, row)
        if existing['source_id'] is None and row['source_id'] is not None:
            existing['source_id'] = row['source_id']
    for result in session.search.get('results', []):
        if result['query_id'] not in queries:
            continue
        url, error = normalize_search_result_url(result['url'] or '')
        error = error or url_denial(url, session.request.url_policy)
        if error or (not result['eligible'] and result.get('exclusion_reason') != 'duplicate_url'):
            exclusions.append({'candidate_id': result['result_id'],
                'reason': error or result.get('exclusion_reason')})
            continue
        row = pool.setdefault(('url', url), {'candidate_id': result['result_id'],
            'source_id': None, 'kind': 'url', 'url': url, 'search_results': []})
        row['search_results'].append({k: result[k] for k in ('result_id', 'query_id', 'title', 'snippet')})
    # A stable location sort mixes origins; model choices, not order, determine acquisition.
    return [pool[key] for key in sorted(pool)], exclusions


def select_probe_sources(session, output_root, base, system_prompt, key_reader, monitor=None):
    """Spend one bounded selection call on coverage value, then validate each chosen ID.

    Args:
        session: Mutable run sharing all model, source and acquisition limits.
        output_root: Run root for durable model and selection receipts.
        base: Existing question, goals and interpreted research context.
        system_prompt: Approved coverage-study prompt loaded from its owned file.
        key_reader: Credential resolver for the shared model executor.
        monitor: Optional cancellation and source-registration observer.
    Returns:
        Up to four source IDs, an empty list when none are usable, or None on a
        failed model call. Record per-item rejection/defer reasons without discarding
        valid neighbors. Only selected destinations are registered; unchosen search
        leads remain pending for the main investigation. New answer runs apply the shared
        requirement/disposition boundary; explicit rejected results are closed, not
        acquired or automatically reconsidered. Supplied sources remain authorized
        for the main loop even if not useful to this preliminary probe. Never fetch URLs.
    """
    study, search = session.coverage_study, session.search
    candidates, excluded = probe_candidates(session)
    selection = {'offered': candidates, 'excluded': excluded, 'proposal': None,
        'items': [], 'error_code': None, 'call_id': None, 'capacity': 4}
    study['selection'] = selection
    study['selection_policy'] = 'Coverage-focused model selection from combined supplied references and search results; at most four pages.'
    if not candidates:
        return []
    payload = {**base, 'phase': 'selection', 'study_plan': study['plan'],
        'candidates': candidates, 'capacity': 4,
        'new_source_capacity': max(0, session.request.max_sources - len(session.sources)),
        'new_search_source_capacity': max(0, search_source_limit(session) - len(search.get('selected_sources', []))),
        'search_queries': [q for q in search.get('queries', []) if q['query_id'] in study['query_ids']]}
    from projectclarity.nodes.schedule_work.answer_controller import enabled
    from projectclarity.discovery.source_disposition import disposition_error
    controlled = enabled(session)
    answer, receipt = invoke_structured(session, output_root, call_id='coverage_study_select',
        task='study_coverage', prompt_version='study-source-dispositions-v1' if controlled else 'coverage-study-v3',
        messages=[('system', system_prompt), ('human', json.dumps(payload, ensure_ascii=False))],
        schema=ContractStudySelection if controlled else CoverageStudySelection, key_reader=key_reader, monitor=monitor)
    selection['call_id'] = receipt['call_id']
    if answer is None:
        selection['error_code'] = receipt['error_code']
        return None
    selection['proposal'] = answer.model_dump(mode='json')
    offered = {c['candidate_id']: c for c in candidates}
    seen, selected, accepted_results, declined_results = set(), [], [], []
    for choice in answer.selections:
        row = offered.get(choice.candidate_id)
        item = {**choice.model_dump(mode='json'), 'status': 'rejected', 'reason_code': None}
        selection['items'].append(item)
        if row is None or choice.candidate_id in seen:
            item['reason_code'] = 'unknown_or_unoffered_id' if row is None else 'duplicate_id'
            continue
        seen.add(choice.candidate_id)
        if controlled:
            error = ('unknown_goal' if set(choice.goal_ids) - {g.goal_id for g in session.plan.goals}
                     else disposition_error(session, choice))
            if error:
                item['reason_code'] = error
                continue
            if choice.disposition == 'reject':
                item.update(status='declined', reason_code='model_rejected')
                declined_results.extend(r['result_id'] for r in row['search_results'])
                continue
        new_source = row['source_id'] is None
        if (len(selected) >= 4 or (new_source and len(session.sources) >= session.request.max_sources)
                or (new_source and row['search_results']
                    and len(search.get('selected_sources', [])) >= search_source_limit(session))):
            item.update(status='deferred', reason_code='capacity')
            continue
        source_id = row['source_id']
        if new_source:
            result = row['search_results'][0] if row['search_results'] else None
            source = URLSource(source_id=f'S{len(session.sources) + 1}', url=row['url'],
                discovered_from_search=result['result_id'] if result else None)
            session.sources.append(source)
            source_id = source.source_id
            if result:
                search.setdefault('selected_sources', []).append({'source_id': source_id,
                    'result_id': result['result_id'], 'goal_ids': study['query_goals'][result['query_id']],
                    'reason': choice.reason})
        selected.append(source_id)
        accepted_results.extend(r['result_id'] for r in row['search_results'])
        item.update(status='accepted', source_id=source_id)
    if any(i['status'] == 'rejected' for i in selection['items']):
        selection['error_code'] = 'invalid_study_selection'
    offered_results = [r['result_id'] for c in candidates for r in c['search_results']]
    if offered_results:
        search.setdefault('selections', []).append({'round': search.get('round', 0),
            'call_id': receipt['call_id'], 'offered_ids': offered_results,
            'accepted_ids': accepted_results,
            'deferred_ids': [i for i in offered_results if i not in accepted_results + declined_results],
            'capacity': 4, 'proposal': selection['proposal'], 'error_code': selection['error_code'],
            'selection_method': 'coverage_focused_combined_pool'})
    study['source_ids'] = selected
    if monitor:
        monitor.register_sources(session.sources)
    save_search(session, output_root)
    return selected
