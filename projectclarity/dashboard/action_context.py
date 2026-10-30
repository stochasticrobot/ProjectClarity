"""Explain the research worker's current action using its saved work records.

The dashboard needs to show which source or passage is being read, which research
goals it serves, and what work is waiting. These helpers join the recorded task,
source and goal identifiers to produce that explanation. They do not rerun the
scheduler or infer why a model made a choice. Finished work is labeled historical,
so a stopped run is not displayed as if it were still reading a document.
"""
from collections import Counter


def action_context(data, status, events, sources, task_labels):
    """Describe the current operation, linked goals and saved queue using existing receipts.

    Args:
        data: Serialized authoritative session; never mutated or scheduled again.
        status: Lock-aware worker status, including its latest source and stage.
        events: Ordered operational records, used only within the current node visit.
        sources: Source-ID mapping with safe display locations.
        task_labels: Mapping from node names to plain-English title/reason pairs.
    Returns:
        Display context with source/passage, goals, selected task, queue counts and
        explicitly labelled policy context. No files, prompts or model calls change.
        The versioned controller's recorded service reason is displayed when it owns
        the selected action. Missing ranking and goal assignment are not guessed.
    """
    workflow = data.get('workflow') or {}
    tasks = workflow.get('tasks', {})
    selected = tasks.get(workflow.get('active')) or {}
    extraction = data.get('extraction') or {}
    attempts = {a['batch_id']: a for a in extraction.get('attempts', [])}
    candidates = {c['candidate_id']: c for c in extraction.get('candidates', [])}
    last_node = next((i for i in range(len(events) - 1, -1, -1)
                      if events[i]['event'] == 'node_started'), None)
    visit = events[last_node:] if last_node is not None else []
    node = (visit[0].get('details', {}).get('node_name') if visit else None) or status.get('stage')
    finished = status.get('finished') or status.get('interrupted') or status.get('waiting_for_input')
    study = data.get('coverage_study') or {}
    studying = study.get('status') == 'running' and study.get('phase') != 'finished' and not finished
    batch = next((e.get('details', {}).get('batch_id') for e in reversed(visit)
                  if e['event'] == 'extraction_call_started'), None)
    if not batch and node == selected.get('action'):
        batch = selected.get('batch_id')
    passage = attempts.get(batch, {})
    source_id = passage.get('source_id')
    if not source_id:
        source_id = next((e.get('source_id') for e in reversed(visit) if e.get('source_id')), None)
    if not source_id and node == selected.get('action'):
        source_id = selected.get('source_id')
    # active_source_id persists across nodes: do not attribute an old page to a
    # new global review, scope planner or web-search selection.
    source = sources.get(source_id)
    title, why = task_labels.get(node, ('Preparing the next action', 'Waiting for the next recorded action.'))
    if studying:
        title = 'Researching scope'
        why = {'plan': 'Planning a bounded study of what adequate coverage would require.',
               'search': 'Searching for coverage benchmarks and authoritative overview sources.',
               'selection': 'Choosing sources for the coverage study.',
               'retrieve': 'Reading the selected coverage-study sources.',
               'assess': 'Establishing coverage guidelines, estimates and readiness checks.'}.get(
                   study.get('phase'), 'Working on the early coverage study.')
    in_flight = [dict(batch_id=a['call_id'], source_id=attempts[a['call_id']]['source_id'],
        passage_id=attempts[a['call_id']]['passage_id'],
        location=sources.get(attempts[a['call_id']]['source_id'], {}).get('location'))
        for a in data.get('model_attempts', []) if not finished and node == 'extract_knowledge'
        and a.get('task') == 'extract_knowledge' and a.get('status') == 'started'
        and a.get('call_id') in attempts]
    if len(in_flight) > 1:
        title = f'Reading {len(in_flight)} passages concurrently'
        why = 'Extracting independent passages in parallel; evidence checks and saved results stay coordinated.'
    elif source:
        title += ' · ' + source_id
    goals = (data.get('plan') or {}).get('goals', [])
    goal_ids, goal_basis = set(), 'No single active goal is recorded; these are the investigation goals.'
    focus = workflow.get('document_focus') or {}
    query = next((q for q in reversed((data.get('search') or {}).get('queries', []))
                  if q.get('status') in {'running', 'started'}), None)
    active_call = next((a for a in reversed(data.get('model_attempts', []))
                       if a.get('status') == 'started' and a.get('task') == node), {})
    review = workflow.get('review_batches', {}).get(active_call.get('call_id'), {})
    if node == 'review_claims' and review:
        claim_ids = list(review.get('assignments', {}))
        goal_basis = 'Goals attached to the claims in this review batch.'
    elif source_id and source_id == focus.get('source_id') and focus.get('status') in {'reading', 'reviewing'}:
        claim_ids = focus.get('candidate_ids', [])
        goal_basis = 'Goals attached to the candidate records that triggered continued reading of this document.'
    else:
        claim_ids = []
    for cid in claim_ids:
        goal_ids.update(candidates.get(cid, {}).get('proposal', {}).get('goal_ids', []))
    if node == 'web_search' and query:
        goal_ids.update(query.get('goal_ids', []))
        goal_basis = 'Goals explicitly attached to the current search query.'
    if studying:
        goal_ids.clear()
        goal_basis = 'Establishing coverage requirements for the investigation, before the main research loop.'
    relevant_goals = [g for g in goals if not goal_ids or g['goal_id'] in goal_ids]
    context = []
    controller = workflow.get('answer_controller') or {}
    if controller:
        context.append(controller.get('reason', 'Servicing the current research turn.'))
    rotation = workflow.get('source_rotation') or {}
    if rotation.get('status') == 'active':
        context.append(f"Paused {rotation['source_id']} after {rotation['no_gain_passages']} completed passages "
            'without new goal-level findings. Checking another source or search; unread passages remain queued.')
    if focus.get('status') in {'reading', 'reviewing', 'blocked'}:
        focused_source = sources.get(focus.get('source_id'), {})
        context.append(f"Document focus: {focus.get('source_id')} — {focused_source.get('location', 'unknown source')}. "
                       f"{len(focus.get('candidate_ids', []))} candidate records triggered continued reading; "
                       f"{focus.get('remaining_passages', 0)} passages remain. These are leads, not verified results.")
        if selected.get('batch_id') == focus.get('probe_batch_id'):
            context.append('The selected passage is a peer-document probe, keeping another source from being starved by the focused document.')
        elif selected.get('action') == 'extract_knowledge' and attempts.get(selected.get('batch_id'), {}).get('source_id') == focus.get('source_id'):
            context.append('The selected passage belongs to the productive-document focus. That policy prioritizes continued reading, with bounded peer probes and batched reviews.')
    cycle = workflow.get('exploration_cycle') or {}
    if cycle:
        phase_labels = {'analyze': 'analyzing available pages', 'explore': 'exploring links',
            'search': 'returning to web search', 'assess_gaps': 'assessing evidence gaps',
            'flush_reviews': 'reviewing queued claims before proceeding',
            'existing_search_results': 'choosing from existing search results'}
        context.append('Exploration phase: ' + phase_labels.get(cycle.get('phase'), str(cycle.get('phase', 'unknown'))) + '.')
        if cycle.get('release_reason') == 'crawl_call_quantum':
            context.append('The recorded exploration checkpoint released this wave toward review, gap assessment or search after its bounded model-call allowance.')
    if selected.get('action') == 'review_claims':
        context.append('Review policy collects bounded claim batches and flushes them at dependency or budget checkpoints; it does not require a model review after every page.')
    counts = Counter()
    for task in tasks.values():
        if task.get('status') not in {'pending', 'blocked', 'running', 'uncertain', 'failed'}:
            continue
        task_source = task.get('source_id') or attempts.get(task.get('batch_id'), {}).get('source_id')
        counts[(task['action'], task.get('status'), task_source, task.get('blocked') or task.get('error_code'))] += 1
    queue = [dict(action=action, title=task_labels.get(action, (action.replace('_', ' '), ''))[0],
                  status=state, source_id=sid, location=sources.get(sid, {}).get('location'),
                  reason=reason, count=count) for (action, state, sid, reason), count in counts.items()]
    chosen_passage = attempts.get(selected.get('batch_id'), {})
    chosen_sid = selected.get('source_id') or chosen_passage.get('source_id')
    plain_reason = why
    handoff = workflow.get('source_handoff') or {}
    if node in {'retrieve_sources', 'extract_knowledge'} and source_id == handoff.get('source_id'):
        plain_reason = 'Reading a selected search result before spending more calls looking for additional sources.'
    elif rotation.get('status') == 'active' and node in {'web_search', 'select_sources', 'assess_research_gaps'}:
        plain_reason = 'The current document stopped adding useful findings. Looking for evidence elsewhere; its unread passages are saved.'
    elif node == 'extract_knowledge' and source_id == focus.get('source_id'):
        plain_reason = ('This page has yielded possible answers. Reading more of it may fill gaps '
                        'and find additional targets.')
    elif node == 'extract_knowledge' and batch == focus.get('probe_batch_id'):
        plain_reason = 'Checking another page so useful evidence elsewhere is not missed.'
    elif node == 'review_claims':
        plain_reason = 'Checking the collected claims against their quotations before accepting them as supported.'
    elif node == 'assess_research_gaps':
        plain_reason = 'Checking what is still missing and whether the evidence meets the research requirements.'
    elif node == 'schedule_work':
        plain_reason = ('Next recorded choice: ' + task_labels.get(selected.get('action'),
                        ('No choice recorded yet', ''))[0] + '.')
    if controller and node == selected.get('action'):
        plain_reason = controller.get('reason') or plain_reason
    if finished:
        plain_reason = 'This run is no longer taking actions. Its findings and unfinished work remain available below.'
    return dict(node=node, title=title, description=why, source=source,
        plain_reason=plain_reason, in_flight=in_flight,
        concurrency_limit=data['request'].get('model_concurrency', 1),
        passage_id=passage.get('passage_id'), batch_id=batch,
        source_passages_total=sum(a.get('source_id') == source_id for a in attempts.values()) if source_id else None,
        goals=[dict(id=g['goal_id'], question=g['question']) for g in relevant_goals], goal_basis=goal_basis,
        studying=studying, study_status=study.get('status', 'not_started'),
        study_enabled=data['request'].get('coverage_study', False), study_phase=study.get('phase'),
        selected=dict(action=selected.get('action'), title=task_labels.get(selected.get('action'), ('Not recorded', ''))[0],
            source_id=chosen_sid, location=sources.get(chosen_sid, {}).get('location'),
            passage_id=chosen_passage.get('passage_id'), status=selected.get('status'), started_at=selected.get('started_at')),
        policy_context=context, queue=queue, query=query.get('query') if query else None,
        selection_note='Recorded selection and saved queue, not a ranked shortlist. Policy context explains the scheduling rules; the engine does not save a complete per-option selection rationale.',
        historical=bool(finished), next_action=workflow.get('next'))
