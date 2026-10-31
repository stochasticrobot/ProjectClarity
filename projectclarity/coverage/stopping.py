"""Check whether the run is allowed to stop because its answer requirements are met.

A model saying "complete" is not sufficient on its own. These helpers check that
the assessment refers to the current evidence and requirements, that source checks
and required reviews succeeded, and that unresolved report entries or missing
batch results do not invalidate the judgment. A hash of the evidence and answer
requirements identifies assessments that became outdated after more work.

The scheduler uses completion_ready only when early stopping was enabled. Displays
use completion_status to explain the same conditions. Budget exhaustion is handled
separately and does not mean the question has been answered. These checks perform
no model requests and cannot independently prove the model's factual judgments.
"""
import hashlib
import json
from projectclarity.result_identity import ACCOUNTING_VERSION
from projectclarity.answer_schema.completion import contract_checks, completion_criteria, report_blockers


def evidence_revision(session):
    """Fingerprint evidence and completion requirements, excluding numerical priors.

    Args:
        session: Run with a plan, extraction, documents, reviews and scope-study checks.
    Returns:
        Digest invalidated by changed evidence, criteria, readiness requirements or
        result-accounting rules, report-mapping policy/repair ownership and the recorded report policy. Old grouping-based
        judgments cannot silently pass.
        Benchmark-validation policy changes also invalidate legacy judgments.
        Budget/cancellation annotations on never-attempted passages are operational
        bookkeeping, not new evidence, and do not invalidate the final assessment.
        Initial/study numerical estimates never become stopping evidence. Explicit
        contracts also bind active assessment policy, plan scope and human guidance;
        study suggestions do not become additional mandatory requirements.
    """
    extraction = session.extraction
    if extraction:
        extraction = {**extraction, 'attempts': [
            {**a, 'error_code': None} if a['status'] == 'not_attempted' else a
            for a in extraction.get('attempts', [])]}
    from projectclarity.claim_review.report_mapping import MAPPING_VERSION
    from projectclarity.coverage.population import POPULATION_POLICY
    payload = {'mapping_policy': MAPPING_VERSION,
        'population_policy': POPULATION_POLICY,
        'mapping_repairs': session.workflow.get('report_mapping_repairs', {}),
        'accounting_version': ACCOUNTING_VERSION,
        'report_policy': session.workflow.get('controller_policy', 'legacy'),
        'claim_support_policy': session.workflow.get('claim_support_policy', 'legacy_field_support'),
        'criteria': [c.model_dump(mode='json') for c in session.plan.completion_criteria],
        'readiness': session.coverage_study.get('readiness'),
        'extraction': extraction, 'reviews': session.claim_reviews,
        'documents': [d.model_dump(mode='json') for d in session.documents]}
    if session.answer_schema:
        from projectclarity.answer_schema.inventory import AGGREGATION_VERSION
        payload['report_aggregation_version'] = AGGREGATION_VERSION
        payload['answer_schema'] = session.answer_schema
    if session.plan:
        from projectclarity.coverage.contract_assessment import policy_revision
        if contract_checks(session):
            payload.pop('readiness', None)
        payload.update(assessment_policy=policy_revision(), plan=session.plan.model_dump(mode='json', exclude={'scope_estimates'}),
            human_guidance=session.review_decision, question=session.request.question,
            sources=[source.model_dump(mode='json') for source in session.sources])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def completion_status(session, output_root, valid=None):
    """Explain the same evidence gates consumed by the production scheduler.

    Args:
        session: Typed run with explicit stopping policy and optional coverage judgments.
        output_root: Run root used to recheck saved source hashes and quotations.
        valid: Optional freshly source-checked candidate mapping, for API reuse only.
    Returns:
        Enabled/ready flags, criterion progress, shared mapping ledger and plain-language
        blockers. Unresolved mappings on supported facts block completion even
        when a semantic coverage judgment claims success. Progress
        measures checked conditions, never population percentage. All primary and
        legacy scope-study criteria must pass together on fresh, untruncated, supported
        evidence. Model grouping and sufficiency remain judgments, not truth proof.
        Explicit contracts additionally require complete report fields and a complete,
        fresh, integrity-checked batch manifest when summaries were used. Study
        suggestions stay advisory for those contracts. No calls or saved-state changes.
    """
    from projectclarity.coverage.readiness import readiness_criteria
    coverage = session.coverage
    history = coverage.get('history', [])
    last = history[-1] if history else {}
    blockers, checks = [], []
    explicit = contract_checks(session)
    expected = completion_criteria(session) if session.plan else []
    supplemental = readiness_criteria(session) if session.plan else []
    expected += supplemental
    fresh = bool(session.plan and coverage.get('assessment_status') == 'assessed'
                 and last.get('evidence_revision') == evidence_revision(session))
    if not fresh:
        blockers.append('Coverage needs a fresh assessment of the current evidence.')
    if last.get('inventory_truncated'):
        blockers.append('The last coverage assessment could not examine all available evidence.')
    if not expected:
        blockers.append('No completion criteria have been defined.')
    study = session.coverage_study
    if not explicit and session.request.coverage_study and study.get('status') not in {'assessed', 'skipped', 'partial'}:
        blockers.append('The requested scope study has not completed successfully.')
    if not explicit and study.get('rejected_checks'):
        blockers.append('The scope study still has unresolved requirements.')
    if supplemental and (last.get('readiness_error') or
            coverage.get('readiness_evidence_revision') != last.get('evidence_revision') or
            coverage.get('readiness_assessment_round') != last.get('round')):
        blockers.append('The scope-study checks have not been assessed with this evidence.')
    rows = coverage.get('criteria_progress', []) + ([] if explicit else coverage.get('readiness_progress', []))
    by_id = {c['criterion_id']: c for c in rows}
    if len(rows) != len(by_id) or set(by_id) != {c.criterion_id for c in expected}:
        blockers.append('Some completion checks are missing or do not match the plan.')
    if valid is None:
        valid = {}
        if (fresh or explicit) and session.extraction:
            from projectclarity.nodes.assess_research_gaps.node import checked_candidates
            _, valid = checked_candidates(session, output_root)
    if any(c.get('evidence_status') == 'anchored' and c.get('adjudication_status') in
           {'pending', 'needs_review'} for c in (session.extraction or {}).get('candidates', [])):
        blockers.append('Some collected claims still need review.')
    from projectclarity.coverage.result_inventory import coverage_results
    ledgers = coverage_results(session, valid) if session.plan and session.extraction else {}
    from projectclarity.claim_review.report_mapping import mapping_ledger
    mappings = mapping_ledger(session.model_dump(mode='json'), set(valid))
    if mappings['unresolved']:
        blockers.append(f"{mappings['unresolved']} supported claim-to-report mappings remain unresolved.")
    if explicit:
        blockers.extend(report_blockers(session, ledgers))
        if not session.plan or [c.model_dump() for c in session.plan.completion_criteria] != [c.model_dump() for c in expected]:
            blockers.append('The plan no longer matches the accepted answer requirements.')
        anchored = {c['candidate_id'] for c in (session.extraction or {}).get('candidates', [])
            if c.get('evidence_status') == 'anchored'}
        if anchored - set(valid):
            blockers.append('Some collected evidence no longer matches its saved source.')
        if fresh and not last.get('bounded_manifest') and set(last.get('offered_candidate_ids', [])) != set(valid):
            blockers.append('The global assessment did not account for the current evidence collection.')
        blockers.extend(batch_completion_blockers(session, last, valid))
    if last.get('bounded_manifest'):
        from projectclarity.coverage.bounded_assessment import completion_blockers
        blockers.extend(completion_blockers(session, last, valid))
    elif last.get('input_mode') in {'direct', 'bounded_tree'}:
        blockers.append('Bounded coverage has no final manifest.')
    for criterion in expected:
        row = by_id.get(criterion.criterion_id, {})
        declared = next((c for c in explicit if c.criterion_id == criterion.criterion_id), None)
        if declared and row.get('result_goal_id') != declared.result_goal_id:
            blockers.append(f'{criterion.criterion_id}: the assessed unit differs from the answer contract.')
        refs = set(row.get('benchmark_candidate_ids', []) + row.get('evidence_candidate_ids', []))
        items = row.get('items', [])
        for item in items:
            refs.update(item.get('candidate_ids', []))
        supported = bool(refs) and all(cid in valid and valid[cid].adjudication_status == 'supported' for cid in refs)
        target = row.get('target_count')
        ledger = ledgers.get(row.get('result_goal_id'))
        item_count = ledger['supported'] if ledger else len(items)
        matched = (criterion.kind != 'enumeration' or
                   (target is not None and item_count == target and
                    (criterion.target_basis != 'research' or bool(row.get('benchmark_candidate_ids')))))
        if ledger:
            refs.update(cid for item in ledger['items'] for cid in item['candidate_ids']
                        if cid in valid and valid[cid].adjudication_status == 'supported')
            supported = bool(refs) and all(cid in valid and valid[cid].adjudication_status == 'supported' for cid in refs)
            current_ids = {item['result_id'] for item in ledger['items'] if item['status'] == 'supported'}
            matched = bool(matched and ledger['supported'] == target
                and not any(ledger[k] for k in ('pending', 'disputed', 'identity_pending'))
                and not (ledger['count_basis'] == 'answer-schema-v1' and ledger['incomplete'])
                and current_ids == set(row.get('mapped_result_ids', [])))
        passed = bool(fresh and not last.get('inventory_truncated') and supported and matched
                      and row.get('status') == 'model_assessed_satisfied'
                      and not row.get('blocking_reasons') and not row.get('gaps'))
        checks.append(dict(id=criterion.criterion_id, description=criterion.description,
            goal_ids=criterion.goal_ids, passed=passed, status=row.get('status', 'not_assessed'),
            observed_count=row.get('observed_count'), target_count=target,
            count_basis=row.get('count_basis', 'model_assessed_items'), evidence_supported=supported,
            gaps=row.get('gaps', [])))
    passed = sum(c['passed'] for c in checks)
    if passed < len(checks):
        blockers.append(f'{len(checks) - passed} of {len(checks)} completion checks are not satisfied.')
    batch = coverage.get('batch_progress', {})
    return dict(enabled=session.request.stop_when_satisfied, ready=not blockers,
                will_stop=session.request.stop_when_satisfied and not blockers,
                fresh=fresh, passed=passed, total=len(checks), checks=checks, blockers=blockers,
                mapping_ledger=mappings, contract_owned=bool(explicit),
                batch_progress={k: batch.get(k) for k in ('mode', 'total', 'completed', 'pending',
                    'failed', 'candidate_total', 'candidates_reviewed')} if batch else None,
                next_action=session.workflow.get('next'))


def batch_completion_blockers(session, last, valid):
    """Verify that a global judgment consumes every current, unaltered batch result.

    Args:
        session: Explicit-contract run with optional digest and progress journals.
        last: Latest coverage-history row, which must represent the global call.
        valid: Freshly source-checked candidate mapping for the current collection.
    Returns:
        Plain-language blockers for stale, missing, unfinished or altered batches.
        Direct assessments require no digest. This checks provenance/accounting,
        not whether a model summary preserved every semantic nuance.
    """
    from projectclarity.coverage.evidence_batches import fingerprint
    progress = session.coverage.get('batch_progress', {})
    if progress.get('mode') != 'incremental':
        return []
    ids = progress.get('batch_ids', [])
    receipts = session.coverage.get('evidence_batches', {})
    covered, hashes, completed = [], [], 0
    for bid in ids:
        row = receipts.get(bid, {})
        if (row.get('status') != 'completed' or row.get('error_code')
                or row.get('batch_id') != bid or row.get('assessment') is None
                or row.get('output_sha256') != fingerprint(row['assessment'])):
            continue
        completed += 1
        covered.extend(row.get('covered_candidate_ids', []))
        hashes.append(row['output_sha256'])
    if (not ids or completed != len(ids) or len(covered) != len(set(covered))
            or set(covered) != set(valid)):
        return [f'Evidence batch review is incomplete: {completed} of {len(ids)} current batches completed.']
    if (progress.get('evidence_revision') != evidence_revision(session)
            or last.get('batch_ids') != ids or last.get('batch_output_revision') != fingerprint(hashes)):
        return ['Completed evidence batches still need a current global answer assessment.']
    return []


def completion_ready(session, output_root):
    """Apply explicit permission before checking evidence-backed completion.

    Args:
        session: Run with optional automatic stopping enabled.
        output_root: Saved-run root for source-integrity checks.
    Returns:
        True only when every current completion gate passes. Disabled runs avoid
        source I/O and continue under normal resource limits.
    """
    return bool(session.request.stop_when_satisfied and completion_status(session, output_root)['ready'])
