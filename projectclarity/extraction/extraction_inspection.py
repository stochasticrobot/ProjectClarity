"""Explain saved extraction calls without rerunning research or reading an API key.

The CLI prints a compact per-passage summary after research. The separate
`inspect-extraction RUN_ID --call S1_P1` command shows what that call was asked
and what it returned, and saves a readable private inspection file. This module
is presentation/inspection only: it does not alter evidence or approve claims.

Older runs saved parsed replies and input fingerprints, not answer text. For
those, we may reconstruct application messages from the saved source/plan/prompt
ONLY when their fingerprint matches the original. That is labelled reconstruction;
the original model text and historical output schema cannot be recovered. If a
fingerprint cannot be reproduced, we say unavailable rather than invent history.
"""

import hashlib
import json
from pathlib import Path

from projectclarity.extraction.extraction_call_tracing import call_trace_path, redact_text, write_private_text
from projectclarity.extraction.extraction_contracts import ExtractionRecord
from projectclarity.extraction.knowledge_extraction_messages import extraction_messages
from projectclarity.extraction.saved_source_passages import EvidenceInputError, read_verified_text
from projectclarity.plan_renderer import terminal_text
from projectclarity.research_planner import ResearchError
from projectclarity.research_schemas import ResearchSession
from projectclarity.run_status_commands import run_directory


def one_line(value: str, limit=220) -> str:
    """Keep untrusted research text readable without terminal controls or endless lines."""
    value = " ".join(terminal_text(value).split())
    return value if len(value) <= limit else value[:limit] + "…"


def render_extraction_summary(session) -> str:
    """Summarize each passage outcome so an empty answer is not confused with failure.

    Preview at most six names, two claims and two qualifications per claim to
    keep the terminal readable; saved calls retain the rest. These are model
    proposals, not a semantic-review verdict or an assessment of research coverage.
    """
    if session.extraction is None:
        return "Extraction did not run."
    record = ExtractionRecord.model_validate(session.extraction)
    lines = ["Extraction results — model proposals, NOT verified answers:"]
    for attempt in record.attempts:
        entities = attempt.result.entities if attempt.result else []
        claims = attempt.result.claims if attempt.result else []
        tokens = f"{attempt.input_tokens if attempt.input_tokens is not None else '?'}/{attempt.output_tokens if attempt.output_tokens is not None else '?'}"
        lines.append(f"  {attempt.batch_id}: {attempt.status}; {len(entities)} entities, {len(claims)} claims; "
                     f"tokens in/out={tokens}; {attempt.elapsed_seconds}s; stop={attempt.stop_reason or '-'}")
        if attempt.error_code:
            lines.append(f"    Problem: {attempt.error_code}")
        if attempt.status == "completed" and not entities and not claims:
            lines.append("    Model returned empty lists. This is not proof that no relevant research exists.")
        if entities:
            lines.append("    Entities: " + "; ".join(one_line(entity.name, 80) for entity in entities[:6]))
            if len(entities) > 6:
                lines.append(f"    … {len(entities) - 6} more entities in the inspection file.")
        if entities and not claims:
            lines.append("    Names were extracted, but no claim was proposed.")
        for claim in claims[:2]:
            lines.append("    Claim: " + one_line(claim.statement))
            for qualification in claim.qualifications[:2]:
                lines.append("      Qualification: " + one_line(qualification))
        if len(claims) > 2:
            lines.append(f"    … {len(claims) - 2} more claims in the inspection file.")
    for error in record.source_errors:
        lines.append(f"  {error['source_id']}: source preparation/validation problem: {error['error_code']}")
    lines.append(f"Inspect prompts and replies: python -m projectclarity inspect-extraction {session.session_id}")
    return redact_text("\n".join(lines))[0]


def historical_call(directory: Path, session, attempt) -> dict:
    """Reconstruct only hash-matching messages; never manufacture an old raw response."""
    trace = {"origin": "historical", "messages": None, "input_status": "unavailable",
             "response_text": None, "parsed_result": attempt.result.model_dump(mode="json") if attempt.result else None}
    try:
        config = json.loads((directory / "extraction_config.json").read_text())
        document = next(doc for doc in session.documents if doc.source_id == attempt.source_id)
        if document.text_sha256 != attempt.text_sha256:
            return trace
        text = read_verified_text(directory, document)
        messages = extraction_messages(session, attempt, text)
        messages[0] = ("system", config["system_prompt"])
        digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()
        if digest == attempt.input_sha256:
            trace["messages"] = [{"role": role, "content": content} for role, content in messages]
            trace["input_status"] = "reconstructed; fingerprint matches the original recorded input"
    except (OSError, ValueError, KeyError, StopIteration, EvidenceInputError):
        pass
    return trace


def render_call(directory: Path, session, attempt) -> str:
    """Show source/goal context, complete available application prompts and returned text."""
    path = call_trace_path(directory, attempt.batch_id)
    if path.exists():
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ResearchError("Extraction trace is too large to inspect.")
        trace = json.loads(path.read_text(encoding="utf-8"))
        if trace.get("run_id") != session.session_id or trace.get("batch_id") != attempt.batch_id:
            raise ResearchError("Extraction trace does not match this run/call.")
    else:
        trace = historical_call(directory, session, attempt)
    lines = [f"=== {attempt.batch_id} ===", f"Outcome: {attempt.status}; error: {attempt.error_code or 'none'}",
        f"Source: {attempt.source_id}; normalized text characters {attempt.start}:{attempt.end}",
        f"Text SHA-256: {attempt.text_sha256}",
        f"Returned model: {attempt.response_model or 'unknown'}; stop: {attempt.stop_reason or 'unknown'}",
        f"Tokens in/out: {attempt.input_tokens}/{attempt.output_tokens}; elapsed: {attempt.elapsed_seconds}s"]
    if trace["origin"] == "captured":
        lines.append("Input: captured application messages. Output schema: see this call's JSON trace (not an HTTP/header dump).")
    else:
        lines.append("Input: " + trace["input_status"] + ". Historical output schema was not captured.")
    if trace.get("credential_redaction_applied"):
        lines.append("Credential redaction was applied; displayed text is not byte-for-byte original.")
    for message in trace.get("messages") or []:
        lines.extend(["", f"--- {message['role'].upper()} MESSAGE ---"])
        # Pretty-print the user JSON, separating the long source passage from
        # the goals so a person can see both without reading escaped newlines.
        if message["role"] == "human":
            payload = json.loads(message["content"])
            passage = payload.pop("passage", {})
            lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
            lines.extend(["", f"SOURCE PASSAGE {passage.get('passage_id', '?')}:", passage.get("text", "")])
        else:
            lines.append(message["content"])
    lines.extend(["", "--- MODEL ANSWER TEXT ---"])
    if trace.get("response_text") is not None:
        lines.append(trace["response_text"])
        if trace.get("response_text_truncated"):
            lines.append("[Capture truncated at its size limit.]")
        if trace.get("non_text_blocks_omitted"):
            lines.append("[Non-answer content blocks omitted.]")
    else:
        lines.append("Unavailable: no answer text was captured. The parsed result below is not the original text.")
    lines.extend(["", "--- PARSED RESULT ---", json.dumps(trace.get("parsed_result"), ensure_ascii=False, indent=2), ""])
    return terminal_text(redact_text("\n".join(lines))[0])


def inspect_extraction(output_root: Path, run_id: str, call_id: str | None = None) -> tuple[str, Path]:
    """Inspect existing records and return display text plus the saved report path.

    Without a call ID, print a summary but save details for every attempt. With
    an ID, print/save that call's details. Neither mode calls the provider or
    changes session/claim status. Inspection is not wholly read-only: its own
    owner-only report is created/replaced inside the run folder.
    """
    directory = run_directory(output_root, run_id)
    try:
        session = ResearchSession.model_validate_json((directory / "session.json").read_text())
        if session.session_id != run_id or session.extraction is None:
            raise ResearchError("This run has no matching extraction record.")
        record = ExtractionRecord.model_validate(session.extraction)
        attempts = [a for a in record.attempts if call_id is None or a.batch_id == call_id]
        if call_id is not None and not attempts:
            raise ResearchError("Unknown extraction call ID. Inspect the run summary for available IDs.")
        summary = render_extraction_summary(session)
        details = "\n".join(render_call(directory, session, attempt) for attempt in attempts)
        destination = directory / (f"extraction_inspection_{call_id}.txt" if call_id else "extraction_inspection.txt")
        write_private_text(destination, terminal_text(redact_text(summary + "\n\n" + details)[0]))
        output = details if call_id else summary
        return output, destination
    except (OSError, ValueError, KeyError, TypeError):
        raise ResearchError("Cannot inspect extraction files. Check saved records and permissions.") from None
