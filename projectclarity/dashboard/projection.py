"""Build browser-facing snapshots from saved runs and checked source evidence.

The dashboard HTTP server calls list_runs for navigation and snapshot for a
selected investigation. snapshot reuses production result counting, source-span
validation and completion checks; it does not derive a second definition of a
supported result. Logs and source metadata become display records, not new facts.
For example, a worker that has stopped remains historical activity rather than
being shown as still reading. These projections make no model calls or file
writes; the server handles caching and turns read/validation failures into errors."""
import json
from collections import Counter

from projectclarity.dashboard.metrics import entity_groups, measurement, token_usage
from projectclarity.dashboard.action_context import action_context
from projectclarity.research_schemas import ResearchSession
from projectclarity.run_status_commands import read_status, run_directory
from projectclarity.nodes.assess_research_gaps.node import checked_candidates
from projectclarity.coverage.freshness import coverage_freshness
from projectclarity.research_results import result_progress
from projectclarity.dashboard.targets import target_groups, scope_summary
from projectclarity.coverage.stopping import completion_status

TASKS = {
    'design_answer': ('Designing the answer', 'Choosing the report structure and the information needed to answer your question.'),
    'plan_research': ('Planning the investigation', 'Defining goals, scope and evidence requirements.'),
    'study_coverage': ('Studying the research scope', 'Establishing a method and a provisional sense of scale.'),
    'web_search': ('Searching the web', 'Looking for sources that address the research goals.'),
    'select_sources': ('Choosing search results to read', 'Selecting promising pages from search results.'),
    'retrieve_sources': ('Retrieving sources', 'Saving page content for evidence-based reading.'),
    'extract_knowledge': ('Reading a document', 'Collecting candidate entities, claims and evidence.'),
    'validate_evidence': ('Checking quoted evidence', 'Checking that quotations occur in the saved source.'),
    'persist_candidates': ('Saving discoveries', 'Keeping candidates and unresolved leads available.'),
    'review_claims': ('Reviewing candidate claims', 'Checking what the supplied evidence actually supports.'),
    'adjudicate_claims': ('Updating claim statuses', 'Recording support, uncertainty and counterevidence.'),
    'assess_research_gaps': ('Assessing coverage and gaps', 'Comparing the collected evidence with the research criteria.'),
    'summarize_evidence': ('Reviewing an evidence batch', 'Retaining source-linked findings for the whole-answer assessment.'),
    'select_followup_sources': ('Choosing links to follow', 'Looking for useful next pages within the crawl policy.'),
    'accumulate_knowledge': ('Collecting broader knowledge', 'Recording entities and relationships beyond direct answers.'),
    'schedule_work': ('Choosing the next research action', 'Balancing queued work against the remaining budget.'),
}


def read_json(path, default=None):
    """Read an optional atomically replaced JSON artifact.

    Args:
        path: Artifact path selected by the application, not an HTTP path.
        default: Value returned while an optional artifact is absent or incomplete.
    Returns:
        Decoded content, or default on I/O/JSON errors; no writes occur.
    """
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def read_lines(path):
    """Read complete JSON lines while allowing a worker to append the next line.

    Args:
        path: A run-owned event or measurement log.
    Returns:
        Valid records in file order; absent files and incomplete trailing lines
        contribute nothing. No history is synthesized or rewritten.
    """
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def list_runs(root):
    """List research runs without opening private model traces or changing history.

    Args:
        root: Configured output directory.
    Returns:
        Newest-first summaries with questions and recorded status. Invalid or
        symlinked run directories are omitted, never followed outside the root.
    """
    rows = []
    for path in root.glob('*/session.json'):
        if path.parent.is_symlink() or path.is_symlink():
            continue
        data = read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get('request'), dict):
            continue
        if not {'created_at', 'status', 'usage'} <= data.keys() or 'question' not in data['request']:
            continue
        rows.append(dict(id=path.parent.name, question=data['request']['question'],
            created_at=data['created_at'], status=data['status'],
            calls=data['usage']['requests_attempted']))
    return sorted(rows, key=lambda x: x['created_at'], reverse=True)


def activity(events, sources, attempts=()):
    """Translate meaningful operational events into plain-language feed entries.

    Args:
        events: Recorded operational events, never model chain-of-thought.
        sources: Source-ID mapping containing saved URLs or local labels.
        attempts: Saved extraction attempts used to name newly observed labels.
    Returns:
        Last eighty useful actions and their reasons. No extra model calls are
        made; bookkeeping-only node visits are intentionally omitted.
    """
    rows, seen_labels = [], set()
    batches = {a['batch_id']: a.get('result') or {} for a in attempts}
    last_supported = None
    for event in events:
        details = event.get('details', {})
        kind = event['event']
        title, why = '', ''
        if kind == 'node_started' and details.get('node_name') in TASKS:
            node = details['node_name']
            if node in {'schedule_work', 'persist_candidates', 'adjudicate_claims', 'validate_evidence'}:
                continue
            title, why = TASKS[node]
        elif kind == 'extraction_call_started':
            title = 'Reading passage ' + str(details.get('passage_id', ''))
        elif kind == 'extraction_call_finished' and details.get('call_status') == 'completed':
            names = []
            for entity in batches.get(details.get('batch_id'), {}).get('entities', []):
                label = (entity['name'].casefold(), entity['entity_type'].casefold())
                if label not in seen_labels:
                    seen_labels.add(label)
                    names.append(entity['name'])
            title = f"Read {details.get('response_claims', 0)} candidate claims"
            if names:
                title = f'Discovered {len(names)} new entity labels'
                why = ', '.join(names[:4]) + ('…' if len(names) > 4 else '')
        elif kind == 'claims_adjudicated':
            count = details.get('supported_claims')
            if count == last_supported:
                continue
            last_supported = count
            title = f'{count} claims now supported by source evidence'
            why = 'A claim-support judgment, not confirmation that every entity meets the research goal.'
        elif kind == 'source_finished':
            title = 'Source retrieved' if details.get('source_status') == 'retrieved' else 'Source could not be retrieved'
            why = details.get('error_code') or 'Page content saved; retrieval alone does not verify its claims.'
        elif kind == 'model_retry_scheduled':
            title, why = 'Retrying a model request', str(details.get('error_code', 'Transient request failure'))
        elif kind == 'model_attempt_finished' and details.get('call_status') != 'response_received':
            title, why = 'Model request did not complete', str(details.get('call_status', 'unknown'))
        elif kind == 'review_required':
            title, why = 'Your clarification is needed', 'Review the plan and answer the outstanding questions.'
        elif kind in {'run_finished', 'run_failed', 'cancellation_observed'}:
            title = {'run_finished': 'Research run ended', 'run_failed': 'Research run failed',
                     'cancellation_observed': 'Stop request acknowledged'}[kind]
        if title:
            rows.append(dict(id=event.get('sequence'), timestamp=event['timestamp'],
                title=title, why=why, source_id=event.get('source_id'),
                source=sources.get(event.get('source_id'), {}).get('location'), level=event['level']))
    return rows[-80:][::-1]


def snapshot(root, run_id):
    """Build a current research view with source-integrity checks and honest unknowns.

    Args:
        root: Configured run-output directory.
        run_id: Validated single run ID; never an arbitrary path.
    Returns:
        Plan, estimates, completion gates, goal-specific target lists, entities,
        candidate evidence, usage, history and status. Claims and targets with changed
        evidence cannot appear supported. Goal-level
        entity verification is explicitly unavailable, not inferred from claim
        support. Old histories are not backfilled with current verdicts. Raises on
        a missing/invalid authoritative session so callers can show a stale banner.
    """
    directory = run_directory(root, run_id)
    session = ResearchSession.model_validate_json((directory / 'session.json').read_text())
    if session.session_id != run_id:
        raise ValueError('Run identity mismatch')
    data = session.model_dump(mode='json')
    status = read_status(directory)
    documents = {d['source_id']: d for d in data['documents']}
    sources = {}
    for source in data['sources']:
        doc = documents.get(source['source_id'], {})
        sources[source['source_id']] = dict(id=source['source_id'],
            location=source.get('url') or str(source.get('path', 'Local document')),
            status=doc.get('status', 'pending'), error=doc.get('error_code'),
            depth=source.get('depth', 0), characters=doc.get('character_count', 0),
            origin='followed link' if source.get('discovered_from_link') else
                   'search result' if source.get('discovered_from_search') else 'supplied source')
    valid = {}
    if session.extraction and session.plan:
        _, valid = checked_candidates(session, root)
    claims = []
    for candidate in (data.get('extraction') or {}).get('candidates', []):
        proposal = candidate['proposal']
        intact = candidate['candidate_id'] in valid
        claims.append(dict(id=candidate['candidate_id'], source_id=candidate['source_id'],
            statement=proposal['statement'], goals=proposal['goal_ids'],
            status=candidate['adjudication_status'] if intact else 'invalid_evidence',
            recorded_status=candidate['adjudication_status'], evidence_intact=intact,
            reason=candidate.get('adjudication_reason'), qualifications=proposal['qualifications'],
            roles=proposal['research_roles'], results=proposal.get('result_records', []),
            evidence=candidate['evidence'], errors=candidate['validation_errors']))
    claim_map = {c['id']: c for c in claims}
    entities = entity_groups(data)
    for entity in entities:
        attached = [claim_map[c] for c in entity['claim_ids'] if c in claim_map]
        entity.update(supported_claims=sum(c['status'] == 'supported' for c in attached),
            pending_claims=sum(c['status'] in {'pending', 'needs_review', 'insufficient_evidence'} for c in attached),
            goals=sorted({g for c in attached for g in c['goals']}), goal_verification='not_assessed')
    current = measurement(data)
    current['supported_claims'] = sum(c['status'] == 'supported' for c in claims)
    current['supported_entity_labels'] = sum(e['supported_claims'] > 0 for e in entities)
    results = result_progress(data, set(valid)) if session.plan else {}
    targets = target_groups(results)
    current['targets'] = {g['goal_id']: {k: g[k] for k in
        ('identified', 'supported', 'pending', 'disputed', 'identity_pending', 'count_basis')} for g in targets}
    events = read_lines(directory / 'events.jsonl')
    history = read_lines(directory / 'dashboard_metrics.jsonl')
    # Never mix superseded name-group counts into the corrected result graph.
    from projectclarity.result_identity import ACCOUNTING_VERSION
    for sample in history:
        sample['values']['targets'] = {gid: row for gid, row in sample['values'].get('targets', {}).items()
            if row.get('count_basis') == current['targets'].get(gid, {}).get('count_basis', ACCOUNTING_VERSION)}
    # Older runs have trustworthy operational claim counts, but no entity or
    # token series. Leave these series null rather than retroactively assigning
    # today's verdict to yesterday's evidence.
    if not history:
        for event in events:
            d = event.get('details', {})
            if event['event'] == 'claims_adjudicated':
                from datetime import datetime
                history.append(dict(timestamp=datetime.fromisoformat(event['timestamp']).timestamp(),
                    values=dict(entity_labels=None, candidate_claims=d.get('candidate_count'),
                        supported_claims=d.get('supported_claims'), goal_verified_entities=None,
                        tokens=None)))
    latest_node = next((e.get('details', {}).get('node_name') for e in reversed(events)
                       if e['event'] == 'node_started'), None)
    now, why = TASKS.get(latest_node, ('Starting research', 'Waiting for the worker to publish its first action.'))
    if status.get('finished') or status.get('interrupted') or status.get('waiting_for_input'):
        now = 'Clarification needed' if status.get('waiting_for_input') else (
            'Worker interrupted' if status.get('interrupted') else 'Run finished')
        why = session.error or 'Inspect findings, remaining gaps and the recorded stopping reason below.'
    attempts = (data.get('extraction') or {}).get('attempts', [])
    for sid, source in sources.items():
        source['passages'] = dict(Counter(a['status'] for a in attempts if a['source_id'] == sid))
        source['supported_claims'] = sum(c['source_id'] == sid and c['status'] == 'supported' for c in claims)
    coverage = data.get('coverage') or {}
    context = action_context(data, status, events, sources, TASKS)
    if not context['historical']:
        now, why = context['title'], context['description']
    return dict(id=run_id, question=session.request.question, created_at=data['created_at'],
        status=status, now=now, why=why, plan=data.get('plan'), pending_review=data.get('pending_review'),
        action_context=context, answer_schema=data.get('answer_schema'),
        estimates=dict(initial=(data.get('plan') or {}).get('scope_estimates', []),
                       study=data.get('coverage_study', {}), current=coverage.get('scope_estimates', [])),
        criteria=coverage.get('criteria_progress', []), readiness=coverage.get('readiness_progress', []),
        readiness_error=(coverage.get('history') or [{}])[-1].get('readiness_error'),
        coverage_freshness=coverage_freshness(session) if session.plan else {'status': 'not_assessed'},
        stop_reason=session.workflow.get('stop_reason'), stop_details=session.workflow.get('stop_details', []),
        automatic_stopping=session.request.stop_when_satisfied,
        completion=completion_status(session, root, valid),
        targets=targets, scope_summary=scope_summary(data),
        metrics=current, tokens=token_usage(data), entities=entities, claims=claims,
        result_progress=results,
        sources=list(sources.values()), searches=(data.get('search') or {}).get('queries', []),
        activity=activity(events, sources, attempts), history=history, native_history=(directory / 'dashboard_metrics.jsonl').exists(),
        broad_capture_enabled=session.request.accumulate_knowledge,
        entity_basis='Exact name/type display groups, not resolved identities. Includes extraction entities and result items.',
        verification_note='Per-entity goal verification is not yet emitted by the engine. Supported claims are shown separately.')
