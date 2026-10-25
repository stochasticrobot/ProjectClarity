"""Give a person a short outcome and useful next commands after research stops.

The saved plan, candidate records and graph remain detailed. This default CLI
summary keeps them from burying the live explanation under hundreds of lines.
It distinguishes downloaded sources, proposed claims, reviewed support and
unfinished work. --details can still show the original full inspection output.
This file only formats existing state; it performs no I/O, calls or approvals.
"""
from collections import Counter
import shlex
from urllib.parse import urlsplit

from projectclarity.terminal_run_progress import explain_reason, short_text
from projectclarity.coverage.scope_estimation import scope_lines
from projectclarity.knowledge.capture_budget import capture_requests


def render_run_summary(session, output_root):
    """Summarize actual receipts and statuses, never equating a finished run with proven truth.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.

    Returns:
        Compact human-readable execution, evidence and coverage summary, distinguishing
        registered pages/documents from URL hostnames and branch-policy stops from
        research completeness, with commands for inspecting saved artifacts.
    """
    directory = output_root / session.session_id
    lines = ["\nResearch summary", f"Run: {session.session_id}",
        "Outcome: " + session.status.replace("_", " "),
        "Question: " + short_text(session.request.question, 300)]
    if session.workflow:
        lines.append('Workflow stop: ' + str(session.workflow.get('stop_reason') or 'running'))
        branches = session.workflow.get('branch_progress', {})
        if branches.get('enabled'):
            states = list(branches['sources'].values())
            lines.append(f"Branch progress: stop after {branches['limit']} consecutive unproductive pages; "
                f"{sum(s['expansion_block'] == 'branch_unproductive_limit' for s in states)} pages at the limit; "
                f"{sum(s['status'] in {'pending', 'unknown'} and not s['seed'] for s in states)} awaiting usable analysis. "
                "Policy stops are not proof of complete research.")
            if not branches.get('path_search_complete', True):
                lines.append(f"Path analysis bounded at {branches['path_states_generated']} generated states; unresolved routes are not pruned.")
        cycle = session.workflow.get('exploration_cycle')
        if cycle:
            lines.append(f"Search timing: explore up to {cycle['limit']} link levels, assess gaps, then search; "
                f"cycle {cycle['number']} ({cycle['phase']}). Empty/blocked frontiers may search earlier.")
        lines.append('Work queue: ' + ', '.join(f'{k}={v}' for k, v in session.workflow.get('queue_counts', {}).items()))
        if session.workflow.get('stop_details'):
            lines.append('Unfinished-work reasons: ' + ', '.join(session.workflow['stop_details']))
    if session.plan:
        lines.append("Question category: " + session.plan.question_category)
        lines.append(f"Plan: {len(session.plan.goals)} research goals. Read {directory / 'plan.md'}")
    if session.documents:
        lines.append(f"Documents: {sum(d.status == 'retrieved' for d in session.documents)}/{len(session.sources)} retrieved.")
    hosts = {urlsplit(str(s.url)).hostname for s in session.sources if getattr(s, 'url', None)}
    lines.append(f"Pages/documents registered: {len(session.sources)}/{session.request.max_sources}; "
        f"website hostnames: {len(hosts)} (not a website quota).")
    if session.search:
        query_cap = session.request.max_search_queries
        query_suffix = f"/{query_cap}" if query_cap is not None else " (no separate cap)"
        lines.append(f"Web search: {len(session.search.get('queries', []))}{query_suffix} query attempts; "
                     f"{len(session.search.get('results', []))} results retained; {len(session.search.get('selected_sources', []))} sources selected.")
        lines.append("Search stop: " + str(session.search.get("stop_reason") or "unknown"))
    extraction = session.extraction or {}
    attempts = extraction.get("attempts", [])
    candidates = extraction.get("candidates", [])
    if extraction:
        lines.append(f"Extraction: {sum(a['status'] == 'completed' for a in attempts)}/{len(attempts)} passages completed; {len(candidates)} candidate claims.")
        statuses = Counter(c.get("adjudication_status", "pending") for c in candidates)
        lines.append("Claim statuses: " + (", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(statuses.items())) or "no candidates"))
        lines.append("Claims are not published facts; model support is not a guarantee of truth.")
        problems = Counter(a["error_code"] for a in attempts if a.get("error_code"))
        for code, count in problems.items():
            lines.append(f"  Extraction issue ({count} passage(s)): {explain_reason(code)} [{code}]")
        review_errors = Counter(r["error_code"] for r in session.claim_reviews.values() if r.get("error_code"))
        for code, count in review_errors.items():
            lines.append(f"  Review issue ({count} claim(s)): {explain_reason(code)} [{code}]")
        lines.append(f"Read claims and evidence: {directory / 'candidates.md'}")
    if session.discovery:
        selected = {s["source_id"] for s in session.discovery.get("selected_sources", [])}
        retrieved = sum(d.source_id in selected and d.status == "retrieved" for d in session.documents)
        lines.append(f"Follow-up links: {len(selected)} selected; {retrieved} downloaded.")
        lines.append("Discovery: " + explain_reason(session.discovery.get("stop_reason")))
        lines.append(f"Read selection reasons: {directory / 'discovery.json'}")
    if session.coverage:
        register = session.coverage.get('goal_register', {})
        if register:
            lines.append(f"Cumulative goal register: {register['total_facts']} candidate facts, not distinct entities.")
            for goal in register['goals']:
                lines.append(f"  {goal['goal_id']}: {goal['discovered_facts']} discovered facts; "
                    f"{goal['evidence_anchored']} anchored; {goal['review_outcomes'].get('supported', 0)} model-supported.")
            lines.append(f"Read cumulative facts and status history: {directory / 'goal_register.json'}")
        lines.extend(scope_lines(session.coverage.get('scope_estimates', [])))
        revision = (session.coverage.get('scope_estimate_history') or [{}])[-1]
        if revision.get('status') in {'not_revised', 'invalid_revision'}:
            lines.append('Scope estimate retained: ' + revision.get('reason', 'revision unavailable'))
        lines.append("Research coverage: " + session.coverage.get("assessment_status", "not_assessed").replace("_", " ") + ". Execution completion is separate.")
        if session.coverage.get("assessment_status") == "assessed":
            for item in session.coverage.get("criteria_progress", []):
                target = item.get("target_count")
                count = f"; {item['observed_count']}/{target if target is not None else 'unknown'} model-grouped items" if item.get("items") or target is not None else ""
                lines.append(f"  {item['criterion_id']}: {item['status'].replace('_', ' ')}{count} (latest assessment only; not cumulative discovery).")
                lines.extend("    Gap: " + short_text(gap, 200) for gap in item.get("gaps", []))
        lines.append(f"Structured criteria and progress: {directory / 'coverage.json'}")
    if session.knowledge:
        windows = session.knowledge.get("windows", [])
        lines.append('Broad capture objective: ' + session.knowledge.get('objective_status',
            'incomplete' if session.knowledge.get('incomplete') else 'complete') +
            '; pause: ' + str(session.knowledge.get('pause_reason') or 'none') + '.')
        lines.append(f"Knowledge: {sum(w['status'] == 'captured' for w in windows)}/{len(windows)} capture windows completed (model-reported, not verified recall).")
        lines.append(f"Accumulated graph: {session.knowledge.get('database')}; change cursor {session.knowledge.get('cursor', 0)}.")
        lines.append("Graph records are source-anchored observations; identities across different sources remain unresolved.")
        for error, count in Counter(w['error'] for w in windows if w.get('error')).items():
            lines.append(f"  Capture issue ({count} window(s)): {error.replace('_', ' ')}.")
        rejected = sum(len(w.get('rejected', [])) for w in windows)
        if rejected:
            lines.append(f"  Rejected graph records: {rejected}; other independently anchored records were retained.")
    if session.source_value:
        bands = Counter(v.get("potential", "unknown") for v in session.source_value.values())
        lines.append("Source potential: " + ", ".join(f"{key}={count}" for key, count in sorted(bands.items())) + ". Heuristic richness, not truth or authority.")
        lines.append(f"Source signals and observed yield: {directory / 'source_value.json'}")
    lines.append(f"Model requests: {session.usage.requests_attempted}/{session.request.budget.max_requests}.")
    capture = capture_requests(session)
    cap = session.request.knowledge_max_requests
    lines.append(f"Research calls: {session.usage.requests_attempted - capture}; broad capture: "
        f"{capture}/{cap if cap is not None else 'legacy shared allowance'} (included in total).")
    extra = "" if str(output_root) == ".projectclarity/runs" else " --output-dir " + shlex.quote(str(output_root))
    lines.extend([f"All run files: {directory}", f"View graph: python -m projectclarity graph {session.session_id}{extra}"])
    if session.knowledge:
        lines.append(f"Read knowledge (not workflow steps): python -m projectclarity knowledge{extra}")
    if extraction:
        lines.append(f"Inspect extraction: python -m projectclarity inspect-extraction {session.session_id}{extra}")
    if session.processing_calls:
        lines.append(f"Inspect capture/review/discovery: python -m projectclarity inspect-calls {session.session_id}{extra}")
    return "\n".join(lines)
