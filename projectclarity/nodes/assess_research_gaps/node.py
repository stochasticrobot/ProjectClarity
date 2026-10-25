"""Persist observable progress and assess plan criteria through the real graph.

Saved coverage separates operational completion from model-assessed answer
sufficiency. Source hashes/quote spans are rechecked after assessment. Unknown
denominators stay null. References, count consistency and truncation gates are
Python checks; semantic sufficiency and item grouping remain model judgments.
No database publication, report generation or automatic truth certification.
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
from projectclarity.session_storage import save_session, write_text


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
    """
    if not session.plan:
        return
    coverage = session.coverage
    coverage.setdefault("schema_version", "1.0")
    coverage.setdefault("assessment_status", "not_assessed")
    coverage.setdefault("history", [])
    initialize_scope(coverage, session.plan)
    coverage["criteria"] = [c.model_dump(mode="json") for c in session.plan.completion_criteria]
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


def validate_assessment(answer, session, offered, *, truncated=False):
    """Validate the coverage model's references and arithmetic so unusable evidence or
    inconsistent counts cannot masquerade as completed research.

    Check criterion/goal/candidate IDs, proposed item keys and denominator rules. Downgrade
    satisfied judgments when evidence is truncated, absent, conflicting or accompanied by
    gaps. Cross-goal references are retained with explicit relevance warnings that block
    satisfaction, not treated as corrupt evidence. Semantic sufficiency and identity
    grouping remain model judgments, not Python-verified facts.

    Args:
        answer: Parsed CoverageAssessment proposed by the model.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        offered: Dictionary mapping the candidate IDs actually offered to still-intact
            CandidateClaim objects.
        truncated: Whether eligible evidence was omitted from the model inventory; True
            blocks a satisfied coverage result.

    Returns:
        A list of criterion-progress dictionaries with observed counts, checked statuses and
        blocking reasons and cross-goal candidate IDs. Unknown/changed evidence and
        inconsistent targets still raise ValueError; goal-tag overlap is not factual proof.
    """
    criteria = {c.criterion_id: c for c in session.plan.completion_criteria}
    ids = [a.criterion_id for a in answer.assessments]
    if len(ids) != len(set(ids)) or set(ids) != set(criteria):
        raise ValueError("criterion_id_mismatch")
    goals = {g.goal_id for g in session.plan.goals}
    if any(set(s.goal_ids) - goals for s in answer.next_searches):
        raise ValueError("unknown_search_goal")
    checked = []
    for assessment in answer.assessments:
        criterion = criteria[assessment.criterion_id]
        references = set(assessment.benchmark_candidate_ids + assessment.evidence_candidate_ids)
        for item in assessment.items:
            references.update(item.candidate_ids)
        if references - offered.keys():
            raise ValueError("unknown_or_changed_evidence")
        cross_goal = sorted(c for c in references if not set(offered[c].proposal.goal_ids) & set(criterion.goal_ids))
        keys = [" ".join(i.key.casefold().split()) for i in assessment.items]
        if any(not k for k in keys) or len(keys) != len(set(keys)):
            raise ValueError("duplicate_or_empty_item_key")
        target = assessment.target_count
        if criterion.target_basis == "user_request" and target != criterion.target_count:
            raise ValueError("changed_requested_target")
        if criterion.target_basis == "research" and target is not None and not assessment.benchmark_candidate_ids:
            raise ValueError("unsupported_denominator")
        if criterion.target_basis == "not_applicable" and target is not None:
            raise ValueError("unexpected_denominator")
        blocking = ['cross_goal_relevance_unconfirmed'] if cross_goal else []
        if assessment.judgment == "satisfied":
            if truncated:
                blocking.append("assessment_inventory_truncated")
            if not references:
                blocking.append("no_evidence")
            if assessment.gaps:
                blocking.append("unresolved_gaps")
            if (criterion.kind == "enumeration" and target is None) or (target is not None and len(keys) != target):
                blocking.append("unknown_or_unmatched_target")
            if any(offered[c].adjudication_status in {"contradicted", "out_of_scope", "invalid_evidence"} for c in references):
                blocking.append("conflicting_or_out_of_scope_evidence")
        checked.append({**assessment.model_dump(mode="json"), "observed_count": len(keys),
            "cross_goal_candidate_ids": cross_goal,
            "count_basis": "model_grouped_items_not_canonical_entities",
            "status": "partial" if blocking else "model_assessed_satisfied" if assessment.judgment == "satisfied" else assessment.judgment,
            "blocking_reasons": blocking})
    return checked


def assess_research_gaps(state, output_root, key_reader=read_api_key, monitor=None):
    """Assess progress against the plan's completion criteria and identify evidence gaps that
    can guide another search round.

    Offer intact anchored candidates in source-balanced, least-exposed-first context,
    with no fixed first-30 cutoff. Disclose deferred evidence if the real adapter
    allowance binds. Keep statements and all supporting quotations whole; disclose
    search permission and provide dated, question-paired guidance. Recheck saved
    evidence after the call, validate IDs and count
    consistency, and preserve each assessment in history. An unknown researched denominator
    stays unknown; model-grouped items are not resolved entities and a satisfied judgment is
    not independently verified truth. Independently validate proposed search goals and
    retain those leads even when evidence/count validation rejects the assessment. Keep
    unexecuted proposals queued across assessments; exploration-cycle runs put the
    latest proposals first. A lead is not accepted evidence.

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
    session.visited_nodes.append("assess_research_gaps")
    save_coverage(session, output_root)
    coverage = session.coverage
    round_id = len(coverage["history"]) + 1
    entry = {"round": round_id, "status": "not_assessed", "error_code": None}
    coverage["history"].append(entry)
    coverage.setdefault("next_searches", [])
    coverage["criteria_progress"] = []  # Prior judgments remain in history, not current progress.
    if not session.plan.completion_criteria:
        entry["error_code"] = "legacy_plan_without_criteria"
    else:
        record, valid = checked_candidates(session, output_root)
        summaries = [{"candidate_id": c.candidate_id, "source_id": c.source_id,
            "statement": c.proposal.statement,
            "goal_ids": c.proposal.goal_ids, "status": c.adjudication_status,
            "roles": c.proposal.research_roles,
            "qualifications": c.proposal.qualifications,
            "quotes": [e.quote for e in c.evidence]} for c in valid.values()]
        payload = {"original_question": session.request.question, "scope_in": session.plan.scope_in,
            "scope_out": session.plan.scope_out, "criteria": coverage["criteria"],
            "goals": [g.model_dump(mode="json") for g in session.plan.goals],
            **research_context(session),
            'cumulative_goal_progress': goal_progress_summary(session),
            "current_scope_estimates": coverage["scope_estimates"],
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
                id_key='candidate_id', source_key='source_id', exposures=exposures)
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
        task="assess_research_gaps", prompt_version="research-coverage-v6",
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
            try:
                checked = validate_assessment(answer, session,
                    {cid: c for cid, c in offered.items() if cid in still_valid}, truncated=payload["inventory_truncated"])
            except ValueError as exc:
                entry["error_code"] = str(exc)
            else:
                entry["status"] = "assessed"
                entry["criteria"] = checked
                coverage["criteria_progress"] = checked
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
