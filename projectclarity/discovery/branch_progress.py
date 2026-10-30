"""Decide whether following another link along a source path is still productive.

A branch is a sequence of pages reached by following links from a starting source.
The scheduler can stop extending a branch after a configured number of analyzed
pages produce no qualifying new evidence. These helpers compare claims, review
outcomes and remaining passage work, while preserving alternate routes to a page.
For example, an unproductive route must not hide the same page when it is reachable
through a productive source. No model is asked to make this decision; unread or
failed analysis is not treated as a successful read with no findings.
"""
from collections import defaultdict, deque
from urllib.parse import urljoin

from projectclarity.discovery.observed_link_inventory import canonical_url, origin, seed_origins
from projectclarity.discovery.url_policy import url_denial


USEFUL_ROLES = {'answer_evidence', 'lead', 'coverage_evidence', 'counterevidence'}
REJECTED = {'contradicted', 'out_of_scope', 'invalid_evidence'}
# Limit exploration of alternate link paths; exceeding this analysis limit means
# progress is unknown, not that the source branch should be closed.
PATH_STATE_LIMIT = 10_000


def normalized(value):
    """Normalize formatting only, without claiming semantic identity.

    Args:
        value: Claim statement, qualification or source quotation.

    Returns:
        Case-folded text with collapsed whitespace; paraphrases remain distinct.
    """
    return ' '.join(value.split()).casefold()


def source_progress(session):
    """Measure new grounded findings and analysis completeness for each source.

    Args:
        session: Run with source versions, validated candidates, passage receipts,
            review outcomes and finalized processing batches.

    Returns:
        Source-ID mapping with useful/empty/pending/unknown state and candidate
        reason receipts. No I/O or mutation. Supported means reviewed passage
        support, not independently verified truth. New exact claim keys, support
        upgrades and additional reviewed quotation text count as progress; copied
        quotations, context-only findings and rejected claims do not. Formatting
        normalization cannot detect paraphrases or establish source independence.
    """
    record = session.extraction or {}
    docs = {d.source_id: d for d in session.documents}
    goals = {g.goal_id for g in session.plan.goals} if session.plan else set()
    signals, candidates, attempts = defaultdict(list), defaultdict(list), defaultdict(list)
    seen = {}
    for candidate in record.get('candidates', []):
        sid = candidate['source_id']
        candidates[sid].append(candidate)
        proposal = candidate['proposal']
        doc = docs.get(sid)
        verdict = candidate.get('adjudication_status', 'pending')
        review = session.claim_reviews.get(candidate['candidate_id'])
        if (not doc or doc.status != 'retrieved' or candidate.get('text_sha256') != doc.text_sha256
                or candidate.get('evidence_status') != 'anchored' or candidate.get('validation_errors')
                or not candidate.get('evidence') or verdict in REJECTED
                or not set(proposal.get('goal_ids', [])) & goals
                or not set(proposal.get('research_roles', ['answer_evidence'])) & USEFUL_ROLES):
            continue
        # Wait for a requested review to return, including a recorded failure.
        # A failed review leaves a grounded lead, not verified support.
        if session.request.review_claims and not review:
            continue
        key = (normalized(proposal['statement']), tuple(sorted(proposal.get('goal_ids', []))),
            tuple(sorted(normalized(q) for q in proposal.get('qualifications', []))))
        quotes = {normalized(e['quote']) for e in candidate['evidence']}
        supported = verdict == 'supported'
        prior = seen.get(key)
        reason = ('new_supported_claim' if supported else 'new_grounded_lead') if prior is None else (
            'support_upgrade' if supported and not prior['supported'] else
            'additional_supporting_evidence' if supported and quotes - prior['quotes'] else None)
        if reason:
            signals[sid].append({'candidate_id': candidate['candidate_id'], 'reason': reason})
        if prior is None:
            seen[key] = {'supported': supported, 'quotes': quotes}
        else:
            prior['supported'] |= supported
            prior['quotes'].update(quotes)
    for attempt in record.get('attempts', []):
        attempts[attempt['source_id']].append(attempt)
    errors = {e['source_id'] for e in record.get('source_errors', [])}
    finalized = set(session.workflow.get('finalized_batches', []))
    result = {}
    for source in session.sources:
        sid = source.source_id
        doc, rows = docs.get(sid), attempts[sid]
        if signals[sid]:
            status, reason = 'useful', 'qualifying_new_progress'
        elif not doc or doc.status != 'retrieved':
            status, reason = 'unknown', 'source_not_retrieved'
        elif sid in errors or any(a['status'] == 'failed' for a in rows):
            status, reason = 'unknown', 'analysis_failed'
        elif any(c.get('evidence_status') == 'invalid' or c.get('review_status') == 'review_failed'
                 for c in candidates[sid]):
            status, reason = 'unknown', 'evidence_or_review_failed'
        elif (not rows or any(a['status'] != 'completed' or a['batch_id'] not in finalized
                or a['text_sha256'] != doc.text_sha256 for a in rows)
                or any(c.get('adjudication_status') == 'pending' for c in candidates[sid])
                or (session.request.review_claims and any(c['candidate_id'] not in session.claim_reviews
                    for c in candidates[sid] if c['evidence_status'] == 'anchored'))):
            status, reason = 'pending', 'analysis_not_complete'
        else:
            status, reason = 'empty', 'complete_without_new_progress'
        result[sid] = {'status': status, 'reason': reason, 'signals': signals[sid]}
    return result


def branch_progress(session):
    """Compute permitted simple routes while retaining nondominated alternatives.

    Args:
        session: Run containing observed HTML edges and the saved branch threshold.

    Returns:
        JSON-shaped policy/decision ledger. For enabled runs, each source reports
        its evidence state, chosen path, consecutive unproductive count and expansion
        block. Search/supplied seeds start at zero. A route dominates another only
        with no greater dry count and a subset of visited nodes; this preserves
        alternatives needed to avoid cycles. Never revisit a node within a path
        or cross an exhausted parent. At PATH_STATE_LIMIT generated states, mark
        unresolved/exhausted route conclusions unknown instead of pruning. No
        network/model calls or session mutation occur.
    """
    limit = session.request.max_unproductive_depth
    if limit is None or not session.request.follow_links:
        return {'version': 1, 'enabled': False, 'limit': limit, 'sources': {}}
    outcomes = source_progress(session)
    sources = {s.source_id: s for s in session.sources if s.kind == 'url'}
    known = {canonical_url(s.url): sid for sid, s in sources.items()}
    known.update({canonical_url(d.final_url): d.source_id for d in session.documents if d.final_url})
    permitted = seed_origins(session)
    edges = defaultdict(set)
    for doc in session.documents:
        if doc.source_id not in sources or doc.status != 'retrieved' or not doc.final_url:
            continue
        parent = sources[doc.source_id]
        if session.request.max_link_depth is not None and parent.depth >= session.request.max_link_depth:
            continue
        for anchor in doc.links:
            url = canonical_url(urljoin(doc.final_url, anchor['href']))
            if (not url or url_denial(url, session.request.url_policy)
                    or (session.request.same_origin_links and origin(url) not in permitted)):
                continue
            if known.get(url) in sources:
                edges[doc.source_id].add(known[url])
    roots = {sid for sid, source in sources.items() if not source.discovered_from_link}
    routes = {sid: [(0, (sid,))] for sid in roots}
    queue = deque((sid, (0, (sid,))) for sid in sorted(roots))
    created, complete = len(roots), True
    while queue and complete:
        parent, route = queue.popleft()
        if route not in routes[parent]:
            continue  # Another retained route reaches this page with an equal or better
            # progress history, so this queued alternative need not be expanded.
        dry, path = route
        if dry >= limit:
            continue
        for child in sorted(edges[parent]):
            if child in path or child in roots:
                continue
            status = outcomes[child]['status']
            if status not in {'useful', 'empty'}:
                continue
            count = 0 if status == 'useful' else dry + 1
            candidate = (count, path + (child,))
            visited = set(candidate[1])
            previous = routes.get(child, [])
            if any(old[0] <= count and set(old[1]) <= visited for old in previous):
                continue
            if created >= PATH_STATE_LIMIT:
                complete = False
                break
            routes[child] = [old for old in previous
                if not (count <= old[0] and visited <= set(old[1]))] + [candidate]
            created += 1
            queue.append((child, candidate))
    best = {sid: min(options, key=lambda r: (r[0], len(r[1]), r[1])) for sid, options in routes.items()}
    decisions = {}
    for sid in sources:
        entry = outcomes[sid]
        route = best.get(sid)
        block = (None if route and route[0] < limit else 'branch_path_search_limit' if not complete else
            'branch_unproductive_limit' if route else
            'branch_analysis_pending' if entry['status'] in {'pending', 'unknown'} else 'branch_path_unavailable')
        decisions[sid] = {**entry, 'seed': sid in roots, 'unproductive_count': route[0] if route else None,
            'path': list(route[1]) if route else [], 'expansion_block': block}
    return {'version': 1, 'enabled': True, 'limit': limit, 'sources': decisions,
        'path_search_complete': complete, 'path_states_generated': created, 'path_state_limit': PATH_STATE_LIMIT,
        'signal_basis': 'Exact normalized claim novelty and existing anchored/reviewed evidence; not truth or calibrated confidence.'}
