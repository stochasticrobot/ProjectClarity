"""Inspect saved review, selection and coverage calls without contacting a provider.

The summary lists outcomes and local trace paths. --call prints the captured
application messages, schema and answer, including failed JSON replies. These are
private research artifacts, not content-free operational logs or an HTTP dump.
This inspector reads no API key and does not repeat the original model call.
"""
import json
import re

from projectclarity.extraction.extraction_call_tracing import redact_text
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.run_status_commands import run_directory


def inspect_calls(output_root, run_id, call_id=None):
    """Return a bounded stored call or receipt summary, refusing arbitrary paths/symlinks.

    Args:
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        run_id: Saved run identifier naming one subdirectory under output_root.
        call_id: Optional processing-call ID; None lists all saved processing-call receipts.

    Returns:
        Readable saved-call text for the selected attempt or a run-wide receipt summary; no
        provider request is made.
    """
    directory = run_directory(output_root, run_id)
    if call_id is None:
        session = json.loads((directory / "session.json").read_text())
        lines = ["Review/discovery calls (no new requests):"]
        for call in session.get("processing_calls", []):
            lines.append(f"  {call['call_id']}: {call['status']}; error={call['error_code'] or '-'}; "
                f"tokens={call.get('input_tokens')}/{call.get('output_tokens')}; trace={call['trace_path'] or '-'}")
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
