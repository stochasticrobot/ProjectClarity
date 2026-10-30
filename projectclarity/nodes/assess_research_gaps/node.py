"""Assess whether collected evidence answers the research plan and record what is missing.

The workflow calls this step to update coverage: the status of the plan's evidence
requirements, supported result counts and suggested searches for unresolved gaps.
For designed answers it delegates to the assessment of the saved report requirements;
other runs use the plan criteria directly. Source hashes and quotation spans are
checked so edited saved text cannot retain an earlier successful assessment.

Python validates references, count consistency and incomplete-input conditions.
The model still judges meaning and evidence sufficiency. A completed assessment
is not independent proof of truth or exhaustive research, and an unknown total
stays unknown rather than producing a completion percentage.
"""
import json
from collections import Counter

from projectclarity.coverage.coverage_contracts import CoverageAssessment
from projectclarity.coverage.scope_estimation import initialize_scope, revise_scope
from projectclarity.discovery.inventory_context import pack_inventory, payload_messages
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.prompt_context import research_context
from projectclarity.coverage.goal_register import goal_progress_summary
from projectclarity.coverage.readiness import readiness_criteria
from projectclarity.coverage.result_inventory import coverage_results, align_result_items
from projectclarity.providers.structured_model_call_execution import MAX_COVERAGE_CHARACTERS
from projectclarity.session_storage import save_session, write_text
from projectclarity.source_context import source_context
from projectclarity.answer_schema.completion import contract_checks, completion_criteria


from projectclarity.prompt_loader import load_prompt

SYSTEM_PROMPT = load_prompt('assess_research_gaps.txt')


def save_coverage(session, output_root):
    """Refresh literal work counts and save criteria/history without spending model requests.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        None. Initialize scope history if absent, refresh counters and write coverage.json
        and session.json; do nothing when no plan exists. Original priors are retained.
        New study requirements are projected separately without mutating plan criteria;
        evidence requirements stay in their own field without concatenation or truncation.
    """
    if not session.plan:
        return
    coverage = session.coverage
    coverage.setdefault("schema_version", "1.0")
    coverage.setdefault("assessment_status", "not_assessed")
    coverage.setdefault("history", [])
    initialize_scope(coverage, session.plan)
    coverage["criteria"] = [c.model_dump(mode="json") for c in
        (contract_checks(session) or session.plan.completion_criteria)]
    requirements = {c['check_id']: c['evidence_required']
        for c in session.coverage_study.get('readiness', {}).get('coverage_checks', [])}
    coverage['readiness_criteria'] = [{**c.model_dump(mode='json'),
        'evidence_required': requirements.get(c.criterion_id, [])} for c in readiness_criteria(session)]
    extraction = session.extraction or {}
    candidates = extraction.get("candidates", [])
    attempts = extraction.get("attempts", [])
    coverage["work_counts"] = {
        "sources_registered": len(session.sources),
        "sources_retrieved": sum(d.status == "retrieved" for d in session.documents),
        "passages_total": len(attempts),
        "passages_completed": sum(a["status"] == "completed" for a in attempts),
        "candidate_claims": len(candidates),
        "anchored_claims": sum(c["evidence_status"] == "anchored" for c in candidates),
        "model_supported_claims": sum(c.get("adjudication_status") == "supported" for c in candidates),
    }
    coverage["execution_status"] = session.status
    coverage["limits_reached"] = sorted({x.get("error_code") for x in attempts + session.processing_calls
        if x.get("error_code") in {"request_limit", "incomplete_model_output"}})
    if 'model_request_limit' in session.workflow.get('stop_details', []):
        coverage['limits_reached'] = sorted(set(coverage['limits_reached']) | {'request_limit'})
    coverage["search_stop_reason"] = session.search.get("stop_reason")
    write_text(output_root / session.session_id / "coverage.json", json.dumps(coverage, indent=2) + "\n")
    save_session(session, output_root)


def checked_candidates(session, output_root):
    """Filter anchored candidates against current saved source hashes and quote spans so
    coverage does not rely on altered evidence.

    Unreviewed but intact candidates remain available as leads or context. Passing these
    checks establishes source integrity and quotation location, not semantic correctness.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        A pair: the full ExtractionRecord and a dictionary mapping candidate IDs to claims
        whose saved evidence still matches.
    """
    record = ExtractionRecord.model_validate(session.extraction)
    documents = {d.source_id: d for d in session.documents}
    texts = {}
    valid = {}
    for candidate in record.candidates:
        if candidate.evidence_status != "anchored" or not candidate.evidence:
            continue
        try:
            document = documents[candidate.source_id]
            if candidate.source_id not in texts:
                texts[candidate.source_id] = read_verified_text(output_root / session.session_id, document)
            text = texts[candidate.source_id]
            if document.text_sha256 != candidate.text_sha256 or any(
                    e.text_sha256 != document.text_sha256 or text[e.start:e.end] != e.quote for e in candidate.evidence):
                continue
        except (EvidenceInputError, KeyError):
            continue
        valid[candidate.candidate_id] = candidate
    return record, valid


def validate_assessment(answer, session, offered, *, truncated=False, criteria_override=None, result_ledgers=None):
    """Validate the coverage model's references and arithmetic so unusable evidence or
    inconsistent counts cannot masquerade as completed research.

    Check criterion/goal/candidate IDs, proposed item keys and denominator rules. Downgrade
    satisfied judgments when evidence is truncated, absent, conflicting or accompanied by
    gaps. Cross-goal references are retained with explicit relevance warnings that block
    satisfaction, not treated as corrupt evidence. Semantic sufficiency and identity
    grouping remain model judgments, not Python-verified facts. Supporting checks
    retain independent statuses; the final completion gate separately requires all
    report fields, so incomplete rows do not falsely negate a valid supporting check.

    Args:
        answer: Parsed CoverageAssessment proposed by the model.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        offered: Dictionary mapping the candidate IDs actually offered to still-intact
            CandidateClaim objects.
        truncated: Whether eligible evidence was omitted from the model inventory; True
            blocks a satisfied coverage result.
        criteria_override: Optional explicit criterion list for the separately tracked
            readiness checks; None validates the original plan criteria as before.
        result_ledgers: Complete source-checked structured inventory; optional for legacy callers.

    Returns:
        A list of criterion-progress dictionaries with observed counts, checked statuses and
        blocking reasons and cross-goal candidate IDs. Unknown/changed evidence and
        inconsistent targets still raise ValueError. Accepted auxiliary-goal mappings
        permit relevance to a requirement without changing its unit; overlap is not
        factual proof.
    """
    criteria = {c.criterion_id: c for c in (completion_criteria(session)
        if criteria_override is None else criteria_override)}
    declared = {c.criterion_id: c for c in contract_checks(session)}
    ids = [a.criterion_id for a in answer.assessments]
    if len(ids) != len(set(ids)) or set(ids) != set(criteria):
        raise ValueError("criterion_id_mismatch")
    goals = {g.goal_id for g in session.plan.goals}
    if any(set(s.goal_ids) - goals for s in answer.next_searches):
        raise ValueError("unknown_search_goal")
    checked = []
    for assessment in answer.assessments:
        criterion = criteria[assessment.criterion_id]
        check = declared.get(assessment.criterion_id)
        if check and assessment.result_goal_id != check.result_goal_id:
            raise ValueError('answer_contract_result_unit_mismatch')
        references = set(assessment.benchmark_candidate_ids + assessment.evidence_candidate_ids)
        for item in assessment.items:
            references.update(item.candidate_ids)
        if references - offered.keys():
            raise ValueError("unknown_or_changed_evidence")
        from projectclarity.answer_schema.requirements import requirement_goals
        allowed_goals = requirement_goals(session, criterion)
        cross_goal = sorted(c for c in references if not set(offered[c].proposal.goal_ids) & allowed_goals)
        keys = [" ".join(i.key.casefold().split()) for i in assessment.items]
        if any(not k for k in keys) or len(keys) != len(set(keys)):
            raise ValueError("duplicate_or_empty_item_key")
        if assessment.result_goal_id is not None and (criterion.kind != 'enumeration'
                or criterion.goal_ids != [assessment.result_goal_id]
                or assessment.result_goal_id not in (result_ledgers or {})):
            raise ValueError('result_goal_unit_mismatch')
        target = assessment.target_count
        if criterion.target_basis == "user_request" and target != criterion.target_count:
            raise ValueError("changed_requested_target")
        if criterion.target_basis == "research" and target is not None and not assessment.benchmark_candidate_ids:
            raise ValueError("unsupported_denominator")
        if criterion.target_basis == "not_applicable" and target is not None:
            raise ValueError("unexpected_denominator")
        blocking = ['cross_goal_relevance_unconfirmed'] if cross_goal else []
        gid, mapped = ((None, []) if check and check.result_goal_id is None else
            align_result_items(criterion, assessment, result_ledgers or {}))
        ledger = (result_ledgers or {}).get(gid)
        if ledger and assessment.result_goal_id is not None:
            for item in assessment.items:
                matches = [r for r in ledger['items']
                    if set(item.candidate_ids) & set(r['candidate_ids'])]
                if len(matches) != 1:
                    blocking.append('unrepresented_result_exception')
                    break
        if gid and len(set(mapped)) != len(mapped):
            blocking.append('duplicate_result_items')
        if ledger and assessment.judgment == 'satisfied':
            if (ledger['pending'] or ledger['disputed'] or ledger['identity_pending']
                    or ledger['count_basis'] == 'answer-schema-v1' and ledger['incomplete']):
                blocking.append('unresolved_result_records')
            if ledger['supported'] != target or len(set(mapped)) != ledger['supported']:
                blocking.append('result_inventory_not_reconciled')
        if assessment.judgment == "satisfied":
            if truncated:
                blocking.append("assessment_inventory_truncated")
            if not references:
                blocking.append("no_evidence")
            if assessment.gaps:
                blocking.append("unresolved_gaps")
            observed = ledger['supported'] if ledger else len(keys)
            if (criterion.kind == "enumeration" and target is None) or (target is not None and observed != target):
                blocking.append("unknown_or_unmatched_target")
            if any(offered[c].adjudication_status in {"contradicted", "out_of_scope", "invalid_evidence"} for c in references):
                blocking.append("conflicting_or_out_of_scope_evidence")
            if (criteria_override is not None or declared) and any(offered[c].adjudication_status != 'supported' for c in references):
                blocking.append('readiness_evidence_not_supported')
        checked.append({**assessment.model_dump(mode="json"), "observed_count": ledger['identified'] if ledger else len(keys),
            "supported_count": ledger['supported'] if ledger else None,
            "result_goal_id": gid, "mapped_result_ids": sorted(set(mapped)),
            "cross_goal_candidate_ids": cross_goal,
            "count_basis": ledger['count_basis'] if ledger else "model_grouped_items_not_canonical_entities",
            "status": "partial" if blocking else "model_assessed_satisfied" if assessment.judgment == "satisfied" else assessment.judgment,
            "blocking_reasons": blocking})
    return checked


def assess_research_gaps(state, output_root, key_reader=read_api_key, monitor=None):
    """Assess progress against the plan's completion criteria and identify evidence gaps that
    can guide another search round.

    Offer intact anchored candidates in source-balanced, least-exposed-first context,
    with no fixed first-30 cutoff. Disclose deferred evidence if the real adapter
    allowance binds. Keep statements and all supporting quotations whole; disclose
    search permission and provide dated, question-paired guidance. Source records
    retain acquisition facts and source-stated metadata separately from claims.
    Recheck saved
    evidence after the call, validate IDs and count
    consistency, and preserve each assessment in history. An unknown researched denominator
    stays unknown; model-grouped items are not resolved entities and a satisfied judgment is
    not independently verified truth. Independently validate proposed search goals and
    retain those leads even when evidence/count validation rejects the assessment. Keep
    unexecuted proposals queued across assessments; exploration-cycle runs put the
    latest proposals first. A lead is not accepted evidence. Supplemental study checks
    share this model call but retain separate progress and validation errors; a bad
    supplemental reply cannot erase valid primary progress or create a stopping target.
    Explicit new answer contracts dispatch to the bounded-review implementation:
    their declared checks replace legacy study gates, large evidence is digested
    without truncation, and a separate global assessment retains the final decision.

    Args:
        state: Graph-state dictionary whose session entry is a typed ResearchSession for the
            current run.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        key_reader: Zero-argument callable returning the model API key when a request needs
            it; defaults to read_api_key.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A dictionary with one session entry containing the updated ResearchSession.
        LangGraph merges this update into its state; it is not an answer report or published
        knowledge.
    """
    session = state["session"]
    if contract_checks(session):
        from projectclarity.coverage.contract_assessment import assess_contract
        return assess_contract(state, output_root, key_reader, monitor)
    session.visited_nodes.append("assess_research_gaps")
    save_coverage(session, output_root)
    coverage = session.coverage
    round_id = len(coverage["history"]) + 1
    entry = {"round": round_id, "status": "not_assessed", "error_code": None}
    coverage["history"].append(entry)
    coverage.setdefault("next_searches", [])
    # Preserve the last successful values for display; failed attempts remain
    # explicit and cannot pass freshness/stopping gates.
    coverage.setdefault('criteria_progress', [])
    coverage.setdefault('readiness_progress', [])
    supplemental = readiness_criteria(session)
    if not session.plan.completion_criteria:
        entry["error_code"] = "legacy_plan_without_criteria"
    else:
        record, valid = checked_candidates(session, output_root)
        ledgers = coverage_results(session, valid)
        summaries = [{"candidate_id": c.candidate_id, "source_id": c.source_id,
            "statement": c.proposal.statement,
            "goal_ids": c.proposal.goal_ids, "status": c.adjudication_status,
            "roles": c.proposal.research_roles,
            "qualifications": c.proposal.qualifications,
            "quotes": [e.quote for e in c.evidence]} for c in valid.values()]
        payload = {"original_question": session.request.question, "scope_in": session.plan.scope_in,
            "scope_out": session.plan.scope_out, "criteria": coverage["criteria"] + coverage['readiness_criteria'],
            "goals": [g.model_dump(mode="json") for g in session.plan.goals],
            **research_context(session),
            'cumulative_goal_progress': goal_progress_summary(session),
            "current_scope_estimates": coverage["scope_estimates"],
            "result_inventory": ledgers,
            "sources": {sid: source_context(session, sid) for sid in
                dict.fromkeys(c.source_id for c in valid.values())},
            "candidate_total": len(record.candidates),
            "intact_anchored_total": len(valid), "offered_total": len(valid),
            "inventory_truncated": False, "work_counts": coverage["work_counts"],
            "search_allowed": session.request.web_search,
            "previous_searches": [q["query"] for q in session.search.get("queries", [])],
            "search_outcomes": [{k: q.get(k) for k in ('query', 'status', 'error_code', 'result_count')}
                for q in session.search.get('queries', [])]}
        exposures = Counter(cid for h in coverage['history'] if h.get('status') == 'assessed'
            for cid in h.get('offered_candidate_ids', []))
        try:
            payload, deferred = pack_inventory(SYSTEM_PROMPT, payload, 'candidates', summaries,
                id_key='candidate_id', source_key='source_id', exposures=exposures,
                max_characters=MAX_COVERAGE_CHARACTERS, task='assess_research_gaps')
        except ValueError:
            entry['error_code'] = 'coverage_context_blocked'
            coverage['scope_estimate_history'].append({'round': round_id, 'status': 'not_revised',
                'reason': 'Assessment context exceeded allowance; previous scope retained.', 'estimates': []})
            coverage['assessment_status'] = 'not_assessed'
            session.status = 'extraction_partial'
            session.error = 'Coverage context could not fit; evidence and previous scope estimates remain saved.'
            save_coverage(session, output_root)
            return {'session': session}
        offered = {row['candidate_id']: valid[row['candidate_id']] for row in payload['candidates']}
        payload.update(offered_total=len(offered), inventory_truncated=bool(deferred))
        entry.update({"offered_candidate_ids": list(offered), "candidate_total": len(record.candidates),
                      "inventory_truncated": payload["inventory_truncated"],
                      "deferred_candidate_ids": deferred, "context_inventory": payload['context_inventory']})
        answer, receipt = invoke_structured(session, output_root, call_id=f"coverage_{round_id}",
        task="assess_research_gaps", prompt_version="research-coverage-v8",
            messages=payload_messages(SYSTEM_PROMPT, payload),
            schema=CoverageAssessment, key_reader=key_reader, monitor=monitor)
        entry["call_id"] = receipt["call_id"]
        if answer is None:
            entry["error_code"] = receipt["error_code"]
        else:
            entry["proposal"] = answer.model_dump(mode="json")
            known_goals = {goal.goal_id for goal in session.plan.goals}
            entry['search_proposals_accepted'] = not any(
                set(proposal.goal_ids) - known_goals for proposal in answer.next_searches)
            if entry['search_proposals_accepted']:
                attempted = {' '.join(q['query'].split()).casefold() for q in session.search.get('queries', [])}
                queued = {}
                fresh = [s.model_dump(mode='json') for s in answer.next_searches]
                previous = coverage.get('next_searches', [])
                ordered = (fresh + previous if session.request.explore_before_search_depth and session.request.follow_links
                    else previous + fresh)
                for proposal in ordered:
                    key = ' '.join(proposal['query'].split()).casefold()
                    if key not in attempted:
                        queued.setdefault(key, proposal)
                coverage['next_searches'] = list(queued.values())
            _, still_valid = checked_candidates(session, output_root)
            current_offered = {cid: c for cid, c in offered.items() if cid in still_valid}
            current_ledgers = coverage_results(session, still_valid)
            primary_ids = {c.criterion_id for c in session.plan.completion_criteria}
            primary_answer = answer.model_copy(update={'assessments':
                [a for a in answer.assessments if a.criterion_id in primary_ids]}) if supplemental else answer
            try:
                checked = validate_assessment(primary_answer, session,
                    current_offered, truncated=payload["inventory_truncated"],
                    result_ledgers=current_ledgers)
            except ValueError as exc:
                entry["error_code"] = str(exc)
            else:
                entry["status"] = "assessed"
                from projectclarity.coverage.stopping import evidence_revision
                entry['evidence_revision'] = evidence_revision(session)
                entry["criteria"] = checked
                coverage["criteria_progress"] = checked
                coverage["last_successful_round"] = round_id
                if supplemental:
                    try:
                        readiness_answer = answer.model_copy(update={'assessments':
                            [a for a in answer.assessments if a.criterion_id not in primary_ids]})
                        progress = validate_assessment(readiness_answer, session, current_offered,
                            truncated=payload['inventory_truncated'], criteria_override=supplemental,
                            result_ledgers=current_ledgers)
                    except ValueError as exc:
                        entry['readiness_error'] = str(exc)
                    else:
                        entry['readiness'] = progress
                        coverage['readiness_progress'] = progress
                        coverage['readiness_assessment_round'] = round_id
                        coverage['readiness_evidence_revision'] = entry['evidence_revision']
                revise_scope(coverage, answer, session,
                    {cid: c for cid, c in offered.items() if cid in still_valid}, round_id)
        if entry['status'] != 'assessed':
            coverage['scope_estimate_history'].append({'round': round_id, 'status': 'not_revised',
                'reason': 'Coverage assessment failed; previous scope estimate retained.', 'estimates': []})
    coverage["assessment_status"] = entry["status"]
    if session.request.web_search:
        from projectclarity.nodes.web_search.node import search_stop_reason, save_search
        session.search["stop_reason"] = search_stop_reason(session) or "followup_queries_ready"
        save_search(session, output_root)
    if entry["error_code"] and session.status != "cancelled":
        session.status = "extraction_partial"
        session.error = "Coverage assessment incomplete; earlier evidence is retained. See coverage.json."
    save_coverage(session, output_root)
    return {"session": session}
