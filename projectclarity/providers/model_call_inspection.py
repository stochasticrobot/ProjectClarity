"""Inspect saved review/discovery calls without contacting a provider or reading a key.

The summary lists outcomes and local trace paths. --call prints the captured
application messages, schema and answer, including failed JSON replies. These are
private research artifacts, not content-free operational logs or an HTTP dump.
"""
import json
import re

from projectclarity.extraction.extraction_call_tracing import redact_text
from projectclarity.research_planner import ResearchError
from projectclarity.run_status_commands import run_directory


def inspect_calls(output_root, run_id, call_id=None):
    """Return a bounded stored call or receipt summary, refusing arbitrary paths/symlinks."""
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
