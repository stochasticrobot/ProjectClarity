"""Display the saved inputs and outputs of structured research-model calls.

The inspect-calls command can list attempts or show a selected call's messages,
response schema, answer text and failure information. These private files let a
developer diagnose answer design, review, selection or coverage without paying
for another request. They are application-level records, not a complete HTTP dump
or ordinary progress log. Inspection does not read an API key, change evidence or
replay the original model request.
"""
import json
import re

from projectclarity.extraction.extraction_call_tracing import redact_text
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.run_status_commands import run_directory


def inspect_calls(output_root, run_id, call_id=None, *, attempt_id=None):
    """Return a bounded stored call or receipt summary, refusing arbitrary paths/symlinks.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        run_id: Saved run identifier naming one subdirectory under output_root.
        call_id: Optional processing-call ID; None lists all saved processing-call receipts.
        attempt_id: Optional M-number selecting safe transport diagnostics for any model
            stage. Mutually exclusive with call_id; reads no prompt or provider body.

    Returns:
        Readable saved-call text for the selected attempt or a run-wide receipt summary; no
        provider request is made.
    """
    directory = run_directory(output_root, run_id)
    if attempt_id is not None:
        if call_id is not None or not re.fullmatch(r'M[1-9][0-9]{0,5}', attempt_id):
            raise ResearchError('Choose one valid model attempt ID, such as M1, without --call.')
        session = json.loads((directory / 'session.json').read_text())
        attempt = next((a for a in session.get('model_attempts', []) if a['attempt_id'] == attempt_id), None)
        if attempt is None:
            raise ResearchError('Model attempt unavailable; older runs may lack diagnostics.')
        return redact_text(json.dumps(attempt, indent=2, ensure_ascii=True))[0]
    if call_id is None:
        session = json.loads((directory / "session.json").read_text())
        lines = ["Review/discovery calls (no new requests):"]
        for call in session.get("processing_calls", []):
            lines.append(f"  {call['call_id']}: {call['status']}; error={call['error_code'] or '-'}; "
                f"tokens={call.get('input_tokens')}/{call.get('output_tokens')}; trace={call['trace_path'] or '-'}")
        if session.get('model_attempts'):
            lines.append('Model transport attempts (response_received does not mean validated evidence):')
            for attempt in session['model_attempts']:
                diagnostics = attempt.get('diagnostics') or {}
                lines.append(f"  {attempt['attempt_id']}: {attempt['task']}/{attempt['call_id']} "
                    f"attempt {attempt['attempt_number']}; {attempt['status']}; "
                    f"error={diagnostics.get('category', '-')}; HTTP={diagnostics.get('http_status')}; "
                    f"recovery={attempt.get('recovery_stop', '-')}")
        return redact_text("\n".join(lines))[0]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", call_id):
        raise ResearchError("Invalid call ID.")
    folder = directory / "llm_calls"
    path = folder / f"{call_id}.json"
    if folder.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ResearchError("Call trace unavailable or unsafe to read.")
    trace = json.loads(path.read_text())
    if trace.get("run_id") != run_id or trace.get("call_id") != call_id:
        raise ResearchError("Call trace does not match the requested run/call.")
    return redact_text(json.dumps(trace, ensure_ascii=False, indent=2))[0]
