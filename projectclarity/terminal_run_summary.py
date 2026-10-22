"""Give a person a short outcome and useful next commands after research stops.

The saved plan, candidate records and graph remain detailed. This default CLI
summary keeps them from burying the live explanation under hundreds of lines.
It distinguishes downloaded sources, proposed claims, reviewed support and
unfinished work. --details can still show the original full inspection output.
This file only formats existing state; it performs no I/O, calls or approvals.
"""
from collections import Counter
import shlex

from projectclarity.terminal_run_progress import explain_reason, short_text


def render_run_summary(session, output_root):
    """Summarize actual receipts and statuses, never equating a finished run with proven truth."""
    directory = output_root / session.session_id
    lines = ["\nResearch summary", f"Run: {session.session_id}",
        "Outcome: " + session.status.replace("_", " "),
        "Question: " + short_text(session.request.question, 300)]
    if session.plan:
        lines.append("Question category: " + session.plan.question_category)
        lines.append(f"Plan: {len(session.plan.goals)} research goals. Read {directory / 'plan.md'}")
    if session.documents:
        lines.append(f"Documents: {sum(d.status == 'retrieved' for d in session.documents)}/{len(session.sources)} retrieved.")
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
    lines.append(f"Model requests: {session.usage.requests_attempted}/{session.request.budget.max_requests}.")
    extra = "" if str(output_root) == ".projectclarity/runs" else " --output-dir " + shlex.quote(str(output_root))
    lines.extend([f"All run files: {directory}", f"View graph: python -m projectclarity graph {session.session_id}{extra}"])
    if extraction:
        lines.append(f"Inspect extraction: python -m projectclarity inspect-extraction {session.session_id}{extra}")
    if session.processing_calls:
        lines.append(f"Inspect review/discovery: python -m projectclarity inspect-calls {session.session_id}{extra}")
    return "\n".join(lines)
