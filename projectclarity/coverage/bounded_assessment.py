"""Production bounded coverage orchestration, shared by designed and legacy runs.

Complete evidence and report ledgers stay in application storage. Small inputs are
assessed directly; larger collections advance a content-addressed reduction tree
one call at a time. The final request contains counts and bounded witnesses, never
all report rows. Source integrity and completion gates remain application-owned.
Summaries are lossy interpretations, not proof that every nuance was preserved.
"""
from collections import Counter

from projectclarity.answer_schema.completion import completion_criteria, contract_checks
from projectclarity.coverage import bounded_tree as tree
from projectclarity.coverage.bounded_contracts import BoundedDigest
from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.coverage.evidence_batches import fingerprint
from projectclarity.coverage.readiness import readiness_criteria
from projectclarity.coverage.result_inventory import coverage_results
from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.prompt_context import research_context
from projectclarity.prompt_loader import load_prompt
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.task_inputs import input_receipt

SYSTEM_PROMPT = load_prompt('bounded_evidence.txt')
MAX_DIRECT_INPUTS = 128
COUNT_KEYS = ('unit', 'count_basis', 'required_fields', 'identified', 'supported',
    'pending', 'disputed', 'identity_pending', 'incomplete', 'invalid', 'unassigned_count')


def context(session):
    """Bind reusable summaries to accepted criteria, scope and human guidance.

    Args:
        session: Planned research run with optional legacy readiness requirements.
    Returns:
        Fixed task context excluding prior judgments and preliminary global study.
    """
    from projectclarity.coverage.contract_assessment import policy_revision
    requirements = {c['check_id']: c['evidence_required'] for c in
        session.coverage_study.get('readiness', {}).get('coverage_checks', [])}
    criteria = [c.model_dump(mode='json') for c in (contract_checks(session) or completion_criteria(session))]
    criteria += [{**c.model_dump(mode='json'), 'evidence_required': requirements.get(c.criterion_id, [])}
        for c in readiness_criteria(session)]
    return dict(assessment_policy=policy_revision(), original_question=session.request.question,
        scope_in=session.plan.scope_in, scope_out=session.plan.scope_out,
        goals=[g.model_dump(mode='json') for g in session.plan.goals],
        criteria=criteria,
        **research_context(session, include_study=False))


def inventory_counts(ledgers):
    """Expose scalar report counts without exposing row or qualification journals.

    Args:
        ledgers: Complete freshly source-checked inventories, retained in application.
    Returns:
        Goal-keyed summaries with exact full-ledger hashes and mapping-gap counts.
        No record, candidate registry or per-row evidence travels in this projection.
    """
    return {gid: {**{k: ledger[k] for k in COUNT_KEYS},
        'mapping_gap_count': len(ledger['mapping_gaps']), 'manifest_sha256': fingerprint(ledger)}
        for gid, ledger in ledgers.items()}


def progress(session, graph, revision, count):
    """Publish current tree progress for scheduling without trusting saved counters.

    Args:
        session: Mutable run owning content-addressed receipts.
        graph: Reconstructed current tree and pending frontier.
        revision: Current evidence revision.
        count: Number of source-checked original claims.
    Returns:
        None; updates the scheduler-visible node IDs, statuses and leaf accounting.
    """
    receipts = session.coverage.setdefault('evidence_batches', {})
    done = [n for n in graph['nodes'] if tree.reusable(n, receipts.get(n['batch_id']), BoundedDigest)]
    failed = sum(receipts.get(n['batch_id'], {}).get('status') in {'failed', 'reserved', 'uncertain'}
        for n in graph['nodes'] if n not in done)
    session.coverage['batch_progress'] = dict(mode='bounded_tree', evidence_revision=revision,
        batch_ids=[n['batch_id'] for n in graph['nodes']], total=len(graph['nodes']),
        completed=len(done), failed=failed, pending=len(graph['nodes'])-len(done)-failed,
        candidate_total=count, candidates_reviewed=sum(len(n['candidate_ids']) for n in done))


def manifest(graph, receipts, valid, ledgers):
    """Bind final assessment to all leaves, exact child outputs and complete ledgers.

    Args:
        graph: Completed reconstructed graph, or None for small direct assessment.
        receipts: Application-owned summary receipts.
        valid: Source-checked original candidate mapping.
        ledgers: Complete report ledgers, not their model projection.
    Returns:
        Compact hash/count manifest. Complete IDs remain in history and descriptors;
        none are expanded into the final model request. Incomplete graphs raise.
    """
    root = graph['root'] if graph else None
    if graph and (graph['pending'] or not root):
        raise ValueError('bounded_tree_incomplete')
    nodes = graph['nodes'] if graph else []
    ids = [cid for node in nodes for cid in node['candidate_ids']] if graph else list(valid)
    if len(ids) != len(set(ids)) or set(ids) != set(valid):
        raise ValueError('bounded_manifest_accounting_mismatch')
    return dict(policy='bounded-input-v1', candidate_count=len(valid),
        candidate_sha256=fingerprint(sorted(valid)), ledger_sha256=fingerprint(ledgers),
        node_count=len(nodes), root_id=root['batch_id'] if root else None,
        tree_sha256=fingerprint([[n['batch_id'], receipts[n['batch_id']]['output_sha256']] for n in nodes]))


def completion_blockers(session, last, valid):
    """Rebuild production provenance before allowing a bounded assessment to stop.

    Args:
        session: Run whose latest assessment claims bounded-input accounting.
        last: Final history entry, not a leaf or reducer-only entry.
        valid: Freshly source-checked evidence mapping.
    Returns:
        Plain-language blockers for changed/missing outputs, manifests or unresolved
        detail. This verifies processing coverage, not summary semantic accuracy.
    """
    try:
        receipts = session.coverage.get('evidence_batches', {})
        graph = tree.plan_tree(session, valid, context(session), SYSTEM_PROMPT, BoundedDigest, receipts) \
            if last.get('input_mode') == 'bounded_tree' else None
        expected = manifest(graph, receipts, valid, coverage_results(session, valid))
        if expected != last.get('bounded_manifest'):
            return ['Bounded coverage manifest is missing or no longer current.']
        if graph:
            answer = receipts[graph['root']['batch_id']]['assessment']
            if any(answer[flag] for flag in tree.FLAGS):
                return ['Bounded summaries retain unresolved conflicts, identity or required detail.']
            offered = set(answer['witness_candidate_ids'])
        else:
            offered = set(valid)
        if offered != set(last.get('offered_candidate_ids', [])):
            return ['Final coverage witness selection does not match the current manifest.']
    except (ValueError, KeyError, TypeError):
        return ['Bounded evidence review is incomplete or its receipts have changed.']
    return []


def run_summary(session, output_root, node, revision, entry, key_reader, monitor):
    """Reserve and execute one tree node, refusing ambiguous automatic replay.

    Args:
        session: Mutable run with shared budgets and receipt journal.
        output_root: Parent directory of saved runs and evidence.
        node: Current pending leaf or reduction descriptor.
        revision: Evidence revision preceding dispatch.
        entry: Current history row receiving status and trace link.
        key_reader: Existing credential callback, invoked only after preflight.
        monitor: Optional cancellation observer.
    Returns:
        None. Saves reservation before I/O and a validated result or explicit failure.
        Failed/uncertain logical summaries are not silently resubmitted.
    """
    from projectclarity.coverage.stopping import evidence_revision
    from projectclarity.nodes.assess_research_gaps.node import checked_candidates, save_coverage
    receipts = session.coverage.setdefault('evidence_batches', {})
    if node['batch_id'] in receipts:
        if receipts[node['batch_id']].get('status') == 'reserved':
            receipts[node['batch_id']].update(status='uncertain', error_code='interrupted_outcome_unknown')
        raise ValueError('bounded_summary_requires_recovery')
    call_id = 'bounded_' + node['batch_id'][3:43]
    receipt = dict(batch_id=node['batch_id'], status='reserved', attempts=1,
        input_ids=node['input_ids'], covered_candidate_ids=node['candidate_ids'],
        assessment=None, error_code=None, call_id=call_id, input_contract=node['input_contract'])
    receipts[node['batch_id']] = receipt
    entry.update(batch_id=node['batch_id'], call_id=call_id)
    save_coverage(session, output_root)
    answer, call = invoke_structured(session, output_root, call_id=call_id, task='bounded_evidence',
        prompt_version='bounded-evidence-v1', messages=payload_messages(SYSTEM_PROMPT, node['payload']),
        schema=BoundedDigest, key_reader=key_reader, monitor=monitor)
    error = call.get('error_code')
    if answer is not None:
        try:
            tree.validate_summary(node, answer, BoundedDigest)
            _, current = checked_candidates(session, output_root)
            if evidence_revision(session) != revision or set(current) != set(session.coverage['active_candidate_ids']):
                raise ValueError('evidence_changed_during_batch')
        except ValueError as exc:
            error = str(exc)
        else:
            receipt['assessment'] = answer.model_dump(mode='json')
            receipt['output_sha256'] = fingerprint(receipt['assessment'])
    receipt.update(status='failed' if error or answer is None else 'completed',
        error_code=error or ('bounded_missing_answer' if answer is None else None))
    entry.update(status='batch_reviewed' if receipt['status']=='completed' else 'not_assessed',
        error_code=receipt['error_code'])


def validate_final(answer, session, offered, ledgers, criteria=None):
    """Reject unoffered references before applying existing result and count checks.

    Args:
        answer: Parsed primary or supplemental response subset.
        session: Current run owning accepted criteria and report definitions.
        offered: Exactly the direct claims or tree witnesses offered to this call.
        ledgers: Full source-checked application inventories, not model inputs.
        criteria: Optional supplemental criteria; None uses primary criteria.
    Returns:
        Validated progress rows. Unknown references raise ValueError before legacy
        row-reference normalization; callers keep supplemental failures separate.
    """
    from projectclarity.nodes.assess_research_gaps.node import validate_assessment
    for assessment in answer.assessments:
        references = set(assessment.benchmark_candidate_ids + assessment.evidence_candidate_ids)
        references.update(cid for item in assessment.items for cid in item.candidate_ids)
        if references - set(offered):
            raise ValueError('unknown_or_changed_evidence')
    return validate_assessment(answer, session, offered, result_ledgers=ledgers, criteria_override=criteria)


def assess(state, output_root, key_reader, monitor=None):
    """Advance the real production coverage path within fixed per-stage input bounds.

    Args:
        state: Graph state containing the mutable typed session.
        output_root: Parent directory of saved runs; source integrity is rechecked.
        key_reader: Existing credential callback, never retained in state.
        monitor: Optional cancellation/progress interface.
    Returns:
        Session update after at most one logical shared-budget call. Direct inputs
        contain at most 128 claims and must fit the small envelope; tree inputs
        contain at most 32 claims or four summaries;
        final tree calls contain at most 24 witnesses and no report rows. Oversized
        fixed context or indivisible evidence fails closed. Legacy readiness stays
        separately validated. Full ledgers and accounting remain in saved storage.
    """
    from projectclarity.coverage.contract_assessment import queue_searches
    from projectclarity.coverage.assessment_diagnostics import begin_assessment, finish_assessment
    from projectclarity.coverage.scope_estimation import revise_scope
    from projectclarity.coverage.stopping import evidence_revision
    from projectclarity.nodes.assess_research_gaps.node import (
        SYSTEM_PROMPT as FINAL_PROMPT, checked_candidates, save_coverage)
    from projectclarity.answer_schema.requirements import work_inventory_context
    session = state['session']
    session.visited_nodes.append('assess_research_gaps')
    save_coverage(session, output_root)
    coverage = session.coverage
    revision, round_id = evidence_revision(session), len(coverage['history']) + 1
    entry = dict(round=round_id, status='not_assessed', error_code=None,
        evidence_revision=revision, inventory_truncated=False)
    coverage['history'].append(entry)
    coverage.setdefault('next_searches', [])
    coverage.setdefault('criteria_progress', [])
    coverage.setdefault('readiness_progress', [])
    record, valid = checked_candidates(session, output_root)
    entry.update(candidate_total=len(record.candidates), intact_anchored_total=len(valid))
    coverage['active_candidate_ids'] = list(valid)
    ledgers, fixed = coverage_results(session, valid), context(session)
    receipts = coverage.setdefault('evidence_batches', {})
    # No summary call is active on entry. Persisted reservations represent an
    # interrupted outcome, including nodes invalidated by changed evidence.
    for receipt in receipts.values():
        if receipt.get('status') == 'reserved':
            receipt.update(status='uncertain', error_code='interrupted_outcome_unknown')
    queries = session.search.get('queries', [])
    work = work_inventory_context(session)
    payload = dict(**fixed, result_inventory=inventory_counts(ledgers), candidates=[], sources={},
        candidate_total=len(record.candidates), intact_anchored_total=len(valid), offered_total=len(valid),
        inventory_truncated=False, context_inventory=dict(total=len(valid), offered=len(valid), deferred=0),
        candidate_status_counts=dict(Counter(c.adjudication_status for c in valid.values())),
        search_allowed=session.request.web_search, current_scope_estimates=coverage.get('scope_estimates', []),
        work_counts=dict(unread_passages=sum(row.get('unread_passages', 0) or 0 for row in work),
            pending_reviews=sum(row.get('pending_review', 0) or 0 for row in work), sources=len(work)),
        previous_searches=[q['query'] for q in queries[-8:]],
        search_outcomes=[{k:q.get(k) for k in ('query','status','error_code','result_count')} for q in queries[-8:]],
        search_history_counts=dict(total=len(queries), included=min(8, len(queries))))
    diagnostics = entry['diagnostics'] = begin_assessment({**payload, 'acquired_work': work}, {})
    answer, checked = None, None
    try:
        uncertain = [bid for bid, receipt in receipts.items()
            if bid.startswith('BT_') and receipt.get('status') == 'uncertain']
        if uncertain:
            entry['uncertain_batch_ids'] = uncertain
            raise ValueError('bounded_summary_requires_recovery')
        if not completion_criteria(session):
            raise ValueError('legacy_plan_without_criteria')
        graph = None
        if len(valid) <= MAX_DIRECT_INPUTS:
            payload['candidates'] = [tree.candidate_input(session, c) for c in valid.values()]
            payload['sources'] = {row['source_id']: row.pop('source_context') for row in payload['candidates']}
        direct = input_receipt('bounded_coverage', payload_messages(FINAL_PROMPT, payload), CoverageAssessment)
        entry['direct_input_characters'] = direct['total_characters'] if len(valid) <= MAX_DIRECT_INPUTS else None
        if len(valid) > MAX_DIRECT_INPUTS:
            entry.update(direct_skipped_reason='candidate_count_bound', base_input_characters=direct['total_characters'])
        if len(valid) > MAX_DIRECT_INPUTS or direct['errors']:
            entry['input_mode'] = 'bounded_tree'
            payload['candidates'] = []
            payload['sources'] = {}
            base_receipt = input_receipt('bounded_coverage', payload_messages(FINAL_PROMPT, payload), CoverageAssessment)
            if base_receipt['errors']:
                entry['input_contract'] = base_receipt
                diagnostics['normalization'] = base_receipt
                raise ValueError('bounded_final_context_too_large')
            graph = tree.plan_tree(session, valid, fixed, SYSTEM_PROMPT, BoundedDigest, receipts)
            progress(session, graph, revision, len(valid))
            if graph['pending']:
                run_summary(session, output_root, graph['pending'][0], revision, entry, key_reader, monitor)
                graph = tree.plan_tree(session, valid, fixed, SYSTEM_PROMPT, BoundedDigest, receipts)
                progress(session, graph, revision, len(valid))
                coverage['assessment_status'] = 'reviewing' if entry['status']=='batch_reviewed' else 'not_assessed'
                diagnostics.update(stage='batch_only', call_id=entry.get('call_id'), error_code=entry['error_code'])
                save_coverage(session, output_root)
                return {'session': session}
            if not graph['root']:
                raise ValueError('bounded_context_too_large')
            root = graph['root']
            root_answer = receipts[root['batch_id']]['assessment']
            payload['evidence_batches'] = [tree.child_input(root, receipts[root['batch_id']])]
            offered = {cid: valid[cid] for cid in root_answer['witness_candidate_ids']}
            # Full quotes were assessed at leaves. Witness metadata gives final refs
            # provenance without reattaching arbitrarily large quoted passages.
            payload['evidence_registry'] = [dict(candidate_id=cid, source_id=c.source_id,
                status=c.adjudication_status, goal_ids=c.proposal.goal_ids) for cid,c in offered.items()]
            entry['summary_blockers'] = [flag for flag in tree.FLAGS if root_answer[flag]]
        else:
            entry['input_mode'] = 'direct'
            offered = valid
            coverage['batch_progress'] = dict(mode='direct', evidence_revision=revision,
                batch_ids=[], total=0, completed=0, pending=0, failed=0,
                candidate_total=len(valid), candidates_reviewed=0)
        payload['bounded_manifest'] = entry['bounded_manifest'] = manifest(graph, receipts, valid, ledgers)
        payload['context_inventory'].update(offered=len(payload['candidates']),
            summarized=len(valid) if graph else 0, witness_references=len(offered) if graph else 0)
        entry.update(offered_candidate_ids=list(offered), accounted_candidate_ids=list(valid),
            candidate_total=len(record.candidates), deferred_candidate_ids=[], context_inventory=payload['context_inventory'])
        payload['offered_total'] = len(offered)
        messages = payload_messages(FINAL_PROMPT, payload)
        preflight = input_receipt('bounded_coverage', messages, CoverageAssessment)
        entry['input_contract'] = preflight
        diagnostics.update(stage='dispatch_pending', normalization=preflight,
            offered_candidate_ids=list(offered), call_id=f'coverage_{round_id}')
        if preflight['errors']:
            raise ValueError(preflight['errors'][0])
        save_coverage(session, output_root)
        answer, call = invoke_structured(session, output_root, call_id=f'coverage_{round_id}',
            task='bounded_coverage', prompt_version='bounded-coverage-v1', messages=messages,
            schema=CoverageAssessment, key_reader=key_reader, monitor=monitor)
        entry['call_id'] = call['call_id']
        diagnostics['provider_receipt'] = {k:call.get(k) for k in ('call_id','trace_path','status','error_code',
            'stop_reason','input_tokens','output_tokens','elapsed_seconds','response_model')}
        if answer is None:
            entry['error_code'] = call.get('error_code') or 'coverage_missing_answer'
        else:
            entry['proposal'] = answer.model_dump(mode='json')
            entry['search_proposals_accepted'] = queue_searches(session, answer, diagnostics)
            _, current = checked_candidates(session, output_root)
            if evidence_revision(session) != revision or set(current) != set(valid):
                raise ValueError('evidence_changed_during_assessment')
            supplemental = readiness_criteria(session)
            primary_ids = {c.criterion_id for c in completion_criteria(session)}
            primary = answer.model_copy(update={'assessments':[a for a in answer.assessments if a.criterion_id in primary_ids]}) if supplemental else answer
            checked = validate_final(primary, session, offered, ledgers)
            for row in checked:
                if entry.get('summary_blockers'):
                    row['blocking_reasons'].extend(entry['summary_blockers'])
                    row['status'] = 'partial'
            entry.update(status='assessed', criteria=checked)
            coverage.update(criteria_progress=checked, last_successful_round=round_id)
            if supplemental:
                try:
                    extra = answer.model_copy(update={'assessments':[a for a in answer.assessments if a.criterion_id not in primary_ids]})
                    readiness = validate_final(extra, session, offered, ledgers, criteria=supplemental)
                except ValueError as exc:
                    entry['readiness_error'] = str(exc)
                else:
                    entry['readiness'] = readiness
                    coverage.update(readiness_progress=readiness, readiness_assessment_round=round_id,
                        readiness_evidence_revision=revision)
            revise_scope(coverage, answer, session, offered, round_id)
    except ValueError as exc:
        entry['error_code'] = str(exc)
    finish_assessment(diagnostics, answer, checked, entry['error_code'])
    coverage['assessment_status'] = entry['status']
    if entry['error_code'] and session.status != 'cancelled':
        session.status = 'extraction_partial'
        session.error = 'Bounded coverage incomplete; evidence and summary receipts are retained.'
    save_coverage(session, output_root)
    return {'session': session}
