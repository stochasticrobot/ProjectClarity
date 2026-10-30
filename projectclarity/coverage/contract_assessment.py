"""Ask the model whether the collected evidence meets the saved answer requirements.

The assess_research_gaps workflow step calls this module for runs with an explicit
answer specification. A small evidence collection is assessed in one request.
A larger collection is divided into batches: each request summarizes one batch,
and a later request assesses all batch summaries together with the report entries.
A batch summary is a model interpretation with evidence references, not new evidence.

The module records progress and reuses completed batches only when their evidence,
source context and assessment policy still match. Every request uses the normal
run allowances, cancellation checks and saved call records. An interrupted batch
with an unknown outcome is not sent again automatically. Finishing the batches
alone does not mean the final answer requirements have been satisfied.
"""
import json
from collections import Counter

from projectclarity.answer_schema.completion import contract_checks
from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.coverage.digest_contracts import EvidenceDigest
from projectclarity.coverage.evidence_batches import fingerprint, plan_evidence_batches, reusable_batch
from projectclarity.coverage.result_inventory import coverage_results
from projectclarity.coverage.scope_estimation import revise_scope
from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.nodes.assess_research_gaps.node import (
    SYSTEM_PROMPT, checked_candidates, save_coverage, validate_assessment)
from projectclarity.prompt_context import research_context
from projectclarity.prompt_loader import load_prompt
from projectclarity.providers.structured_model_call_execution import invoke_structured, MAX_COVERAGE_CHARACTERS
from projectclarity.session_storage import save_session
from projectclarity.source_context import source_context

DIGEST_PROMPT = load_prompt('summarize_evidence.txt')
BATCH_CHARACTERS = 128_000
RETRYABLE_DIGEST = {'invalid_output_schema', 'incomplete_model_output',
    'digest_candidate_id_mismatch', 'digest_criterion_id_mismatch'}


def policy_revision():
    """Hash the loaded assessment instructions and batch-summary response schema.

    Saved completion judgments must not silently remain current after the model-facing
    assessment policy changes. The returned hash identifies that policy version.

    Args:
        None. Uses verbatim prompts loaded by this process and the typed schema.
    Returns:
        Stable policy digest preventing earlier global judgments from silently
        surviving a changed prompt/schema. No file reads or state mutation.
    """
    return fingerprint([SYSTEM_PROMPT, DIGEST_PROMPT, EvidenceDigest.model_json_schema()])


def assessment_context(session):
    """Collect the question and answer requirements that stay fixed during batch assessment.

    The result includes goals, scope and human clarification but excludes mutable
    coverage judgments. A previous model assessment must not become evidence for its
    own replacement or continually change the cache key.

    Args:
        session: Planned run with an explicit answer contract and human guidance.
    Returns:
        Question, goals, scope, contract checks and question-paired guidance. Mutable
        coverage progress and preliminary study interpretations are excluded so a
        prior assessment cannot recursively become its own evidence or cache key.
    """
    guidance = research_context(session)
    guidance.pop('early_coverage_study', None)
    return dict(assessment_policy=policy_revision(), original_question=session.request.question, scope_in=session.plan.scope_in,
        scope_out=session.plan.scope_out, goals=[g.model_dump(mode='json') for g in session.plan.goals],
        criteria=[c.model_dump(mode='json') for c in contract_checks(session)], **guidance)


def compact_ledgers(ledgers):
    """Reduce repeated field detail in report inventories sent for completion assessment.

    Keep every report entry, its values and evidence references. Remove redundant detail
    for supported fields while retaining unresolved alternatives, so reducing request
    size does not hide a disagreement or missing field.

    Args:
        ledgers: Complete source-checked goal inventories from coverage_results.
    Returns:
        New inventories retaining all rows, values, candidate links, counts and
        unresolved field alternatives. Supported fields already have their values,
        row status and citations; no conflicts or incomplete observations are clipped.
    """
    return {gid: {**ledger, 'items': [{**item, 'field_status': {
        name: field for name, field in (item.get('field_status') or {}).items()
        if field['status'] != 'supported'}} for item in ledger['items']]}
        for gid, ledger in ledgers.items()}


def validate_digest(answer, batch, criteria):
    """Check that a model's batch summary accounts for all offered evidence IDs.

    A digest is a summary of one evidence batch used in a later whole-answer assessment.
    This validator checks complete candidate coverage and known criterion references,
    not whether the model summarized the evidence faithfully.

    Args:
        answer: Parsed EvidenceDigest, still a model interpretation.
        batch: Exact input descriptor containing its complete candidate roster.
        criteria: Set of contract criterion identifiers offered to the digest call.
    Returns:
        Sorted covered candidate IDs. Raises ValueError for missing, invented or
        wrong-criterion references. Does not certify summary fidelity or sufficiency.
    """
    covered = {cid for finding in answer.findings for cid in finding.candidate_ids}
    if covered != set(batch['candidate_ids']):
        raise ValueError('digest_candidate_id_mismatch')
    if any(set(f.criterion_ids) - criteria for f in answer.findings):
        raise ValueError('digest_criterion_id_mismatch')
    return sorted(covered)


def batch_schedule_key(session):
    """Describe saved evidence-batch progress so the scheduler can identify the next task.

    A change from pending to completed or failed batch work changes the task identity.
    Repeated calls without such progress must not create unlimited fresh retries.

    Args:
        session: Run with optional active batch IDs and saved digest receipts.
    Returns:
        Small stable identity rows. Changed receipt states create the next work
        unit, but aggregate attempts/history do not manufacture unlimited retries.
    """
    receipts = session.coverage.get('evidence_batches', {})
    return [[bid, receipts.get(bid, {}).get('status'), receipts.get(bid, {}).get('attempts', 0)]
        for bid in session.coverage.get('batch_progress', {}).get('batch_ids', [])]


def update_batch_progress(session, batches, revision):
    """Count current complete, unfinished and failed batches without trusting stale totals.

    Args:
        session: Mutable run whose coverage journal receives progress only.
        batches: Current descriptors after source checks and context partitioning.
        revision: Exact evidence/contract revision for this assessment cycle.
    Returns:
        Progress dictionary saved in coverage. Only fully validated receipts with
        matching output hashes count as complete; no completion judgment is made.
    """
    receipts = session.coverage.setdefault('evidence_batches', {})
    complete, failed, covered = 0, 0, 0
    for batch in batches:
        receipt = receipts.get(batch['batch_id'])
        if reusable_batch(batch, receipt) and receipt.get('output_sha256') == fingerprint(receipt['assessment']):
            complete += 1
            covered += len(batch['candidate_ids'])
        elif receipt and (receipt.get('status') in {'failed', 'uncertain', 'reserved'}):
            failed += 1
    progress = dict(mode='incremental', evidence_revision=revision,
        batch_ids=[b['batch_id'] for b in batches], total=len(batches), completed=complete,
        pending=len(batches)-complete-failed, failed=failed,
        candidate_total=sum(len(b['candidate_ids']) for b in batches), candidates_reviewed=covered)
    session.coverage['batch_progress'] = progress
    return progress


def queue_searches(session, answer):
    """Retain valid blocker leads independently of a rejected completion judgment.

    Args:
        session: Mutable run containing permitted goals and prior search receipts.
        answer: Parsed global assessment with proposed follow-up queries.
    Returns:
        True if all proposed goal references are known; otherwise False without
        altering the queue. Accepted leads are deduplicated and attempted queries
        excluded. Search permission and budgets remain enforced by the scheduler.
    """
    goals = {g.goal_id for g in session.plan.goals}
    if any(set(q.goal_ids)-goals for q in answer.next_searches):
        return False
    if not session.request.web_search:
        return True
    attempted = {' '.join(q['query'].split()).casefold() for q in session.search.get('queries', [])}
    queued = {}
    for query in [*[q.model_dump(mode='json') for q in answer.next_searches],
                  *session.coverage.get('next_searches', [])]:
        key = ' '.join(query['query'].split()).casefold()
        if key not in attempted:
            queued.setdefault(key, query)
    session.coverage['next_searches'] = list(queued.values())
    return True


def run_digest(session, output_root, batch, batches, revision, entry, key_reader, monitor):
    """Ask the model to summarize one evidence batch and save the checked outcome.

    Reserve the request before sending it, retain failure information and recheck source
    integrity after the response. An earlier request with an unknown outcome is not
    automatically replayed. The summary will later feed whole-answer assessment.

    Args:
        session: Mutable research run with shared model budget and coverage journal.
        output_root: Directory containing run subdirectories and verified sources.
        batch: One new or explicitly retryable current input descriptor.
        batches: Complete current batch plan for progress reporting.
        revision: Evidence/contract revision preceding this invocation.
        entry: Mutable coverage-history row receiving the call outcome.
        key_reader: Credential callback used only by the ordinary executor.
        monitor: Optional cancellation and progress observer.
    Returns:
        None. Makes at most one logical model call, journals reservation before I/O,
        rechecks evidence afterward and retains a validated digest or explicit failure.
        Transport retries consume the existing allowance; ambiguous outcomes do not retry.
    """
    receipts = session.coverage.setdefault('evidence_batches', {})
    attempts = receipts.get(batch['batch_id'], {}).get('attempts', 0) + 1
    call_id = 'digest_' + batch['batch_id'][3:43] + '_' + str(attempts)
    receipt = dict(batch_id=batch['batch_id'], status='reserved', attempts=attempts,
        covered_candidate_ids=[], assessment=None, error_code=None, call_id=call_id)
    receipts[batch['batch_id']] = receipt
    save_session(session, output_root)
    answer, call = invoke_structured(session, output_root, call_id=call_id,
        task='summarize_evidence', prompt_version='evidence-digest-v1',
        messages=payload_messages(DIGEST_PROMPT, batch['payload']), schema=EvidenceDigest,
        key_reader=key_reader, monitor=monitor)
    error = call.get('error_code')
    if answer is not None:
        try:
            covered = validate_digest(answer, batch, {c.criterion_id for c in contract_checks(session)})
            _, current = checked_candidates(session, output_root)
            current_batches = plan_evidence_batches(session, current, assessment_context(session),
                DIGEST_PROMPT, max_characters=BATCH_CHARACTERS)
            if batch['batch_id'] not in {b['batch_id'] for b in current_batches}:
                raise ValueError('evidence_changed_during_batch')
        except ValueError as exc:
            error = str(exc)
        else:
            receipt.update(assessment=answer.model_dump(mode='json'), covered_candidate_ids=covered)
            receipt['output_sha256'] = fingerprint(receipt['assessment'])
    receipt.update(status='failed' if error or answer is None else 'completed',
        error_code=error or ('digest_missing_answer' if answer is None else None))
    entry.update(status='batch_reviewed' if receipt['status']=='completed' else 'not_assessed',
        call_id=call_id, batch_id=batch['batch_id'], error_code=receipt['error_code'])
    update_batch_progress(session, batches, revision)


def assess_contract(state, output_root, key_reader, monitor=None):
    """Advance the model assessment of evidence against the saved answer requirements.

    For inputs that fit, assess all evidence directly. For larger inputs, advance one
    batch summary or the final judgment over all current summaries and report entries.
    This is the workflow step's implementation, not a declaration of completion merely
    because one summary request succeeded.

    Args:
        state: Production graph state containing the typed research session.
        output_root: Run-artifact root used to recheck source hashes and quotations.
        key_reader: Existing model credential callback, not persisted in state.
        monitor: Optional progress and cooperative cancellation interface.
    Returns:
        Session update. Uses one shared-budget model call at most, saves all work
        and gaps, and never marks unfinished/failed/oversized aggregation complete.
        The global payload distinguishes acquired/unread work from examined evidence.
        Small populations use direct evidence; large ones use every current digest
        and complete result inventories. Semantic fidelity remains a model judgment.
    """
    from projectclarity.coverage.stopping import evidence_revision
    session = state['session']
    session.visited_nodes.append('assess_research_gaps')
    save_coverage(session, output_root)
    coverage = session.coverage
    round_id = len(coverage['history']) + 1
    revision = evidence_revision(session)
    entry = dict(round=round_id, status='not_assessed', error_code=None,
        evidence_revision=revision, inventory_truncated=False)
    coverage['history'].append(entry)
    coverage.setdefault('next_searches', [])
    coverage.setdefault('criteria_progress', [])
    coverage['readiness_progress'] = []
    record, valid = checked_candidates(session, output_root)
    ledgers = coverage_results(session, valid)
    context = assessment_context(session)
    summaries = [dict(candidate_id=c.candidate_id, source_id=c.source_id,
        statement=c.proposal.statement, goal_ids=c.proposal.goal_ids,
        status=c.adjudication_status, roles=c.proposal.research_roles,
        qualifications=c.proposal.qualifications, quotes=[e.quote for e in c.evidence])
        for c in valid.values()]
    from projectclarity.answer_schema.requirements import work_inventory_context
    payload = dict(**context, acquired_work=work_inventory_context(session), result_inventory=compact_ledgers(ledgers), candidates=summaries,
        sources={sid: source_context(session, sid) for sid in dict.fromkeys(c.source_id for c in valid.values())},
        candidate_total=len(record.candidates), intact_anchored_total=len(valid), offered_total=len(valid),
        inventory_truncated=False, context_inventory=dict(total=len(valid), offered=len(valid), deferred=0),
        candidate_status_counts=dict(Counter(c.adjudication_status for c in valid.values())),
        current_scope_estimates=coverage.get('scope_estimates', []),
        search_allowed=session.request.web_search,
        previous_searches=[q['query'] for q in session.search.get('queries', [])],
        search_outcomes=[{k: q.get(k) for k in ('query','status','error_code','result_count')}
            for q in session.search.get('queries', [])])
    entry.update(candidate_total=len(record.candidates), offered_candidate_ids=list(valid),
        deferred_candidate_ids=[], context_inventory=payload['context_inventory'],
        direct_input_characters=len(json.dumps(payload_messages(SYSTEM_PROMPT, payload), ensure_ascii=False)))
    try:
        if entry['direct_input_characters'] > MAX_COVERAGE_CHARACTERS:
            entry['input_mode'] = 'incremental'
            batches = plan_evidence_batches(session, valid, context, DIGEST_PROMPT,
                max_characters=BATCH_CHARACTERS)
            receipts = coverage.setdefault('evidence_batches', {})
            # A persisted reservation has an ambiguous outcome after interruption.
            # Its content-addressed call is not submitted again, even on restart.
            for batch in batches:
                receipt = receipts.get(batch['batch_id'], {})
                if receipt.get('status') == 'reserved':
                    receipt.update(status='uncertain', error_code='interrupted_outcome_unknown')
            progress = update_batch_progress(session, batches, revision)
            pending = [b for b in batches if not receipts.get(b['batch_id']) or (
                receipts[b['batch_id']].get('status') == 'failed'
                and receipts[b['batch_id']].get('error_code') in RETRYABLE_DIGEST
                and receipts[b['batch_id']].get('attempts', 0) < 2)]
            if pending:
                run_digest(session, output_root, pending[0], batches, revision, entry, key_reader, monitor)
                coverage['assessment_status'] = 'reviewing'
                save_coverage(session, output_root)
                return {'session': session}
            if progress['completed'] != progress['total']:
                raise ValueError('evidence_batches_incomplete')
            payload['candidates'] = []
            payload['evidence_batches'] = [dict(batch_id=b['batch_id'],
                **receipts[b['batch_id']]['assessment']) for b in batches]
            payload['evidence_registry'] = [dict(candidate_id=c.candidate_id,
                source_id=c.source_id, status=c.adjudication_status, goal_ids=c.proposal.goal_ids)
                for c in valid.values()]
            entry['batch_ids'] = progress['batch_ids']
            entry['batch_output_revision'] = fingerprint([receipts[bid]['output_sha256'] for bid in progress['batch_ids']])
            if len(json.dumps(payload_messages(SYSTEM_PROMPT, payload), ensure_ascii=False)) > MAX_COVERAGE_CHARACTERS:
                raise ValueError('aggregate_context_blocked')
        else:
            entry['input_mode'] = 'direct'
            coverage['batch_progress'] = dict(mode='direct', evidence_revision=revision,
                batch_ids=[], total=0, completed=0, pending=0, failed=0,
                candidate_total=len(valid), candidates_reviewed=0)
        answer, receipt = invoke_structured(session, output_root, call_id=f'coverage_{round_id}',
            task='assess_research_gaps', prompt_version='answer-contract-coverage-v1',
            messages=payload_messages(SYSTEM_PROMPT, payload), schema=CoverageAssessment,
            key_reader=key_reader, monitor=monitor)
        entry['call_id'] = receipt['call_id']
        if answer is None:
            entry['error_code'] = receipt.get('error_code') or 'coverage_missing_answer'
        else:
            entry['proposal'] = answer.model_dump(mode='json')
            entry['search_proposals_accepted'] = queue_searches(session, answer)
            _, current = checked_candidates(session, output_root)
            if evidence_revision(session) != revision or set(current) != set(valid):
                raise ValueError('evidence_changed_during_assessment')
            checked = validate_assessment(answer, session, current,
                result_ledgers=coverage_results(session, current))
            entry.update(status='assessed', criteria=checked)
            coverage.update(criteria_progress=checked, last_successful_round=round_id)
            revise_scope(coverage, answer, session, current, round_id)
    except ValueError as exc:
        entry['error_code'] = str(exc)
    coverage['assessment_status'] = entry['status']
    if entry['error_code'] and session.status != 'cancelled':
        session.status = 'extraction_partial'
        session.error = 'Answer-contract assessment incomplete; evidence and completed batches are retained.'
    save_coverage(session, output_root)
    return {'session': session}
