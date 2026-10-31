"""Keep a private, inspectable record of what each extraction call asked and returned.

For example, extraction_calls/S1_P1.json contains the system instructions, the
question/goals and source passage sent for S1/P1, the output schema supplied to
LangChain, and the returned answer text even when it fails JSON validation.
This fills a different role from events.jsonl: operational events contain small
counts and statuses, while these private files contain actual research content.

The extraction node calls start_trace() before invocation and finish_trace()
afterward. We use LangChain's existing include_raw response, not an HTTP proxy
or external tracing service. These are application messages/schema, NOT a dump
of the SDK's complete wire request, headers or hidden provider instructions.
Only answer text is captured; thinking/signatures and provider exception bodies
are excluded. Known API-key values/patterns are redacted, with redaction labelled.
Files are owner-readable/writable only and remain inside the ignored run folder.
"""

import json
import os
import re
from pathlib import Path

from projectclarity.extraction.extraction_contracts import ExtractionBatch
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.run_monitor import MonitoringError


def redact_text(text: str, secret: str | None = None) -> tuple[str, bool]:
    """Return redacted text plus a flag saying whether replacements occurred.

    Covers the supplied exact credential and recognizable Anthropic key literals, not
    arbitrary personal data, every provider's secrets or encoded credentials. The flag
    prevents presenting a modified capture as byte-for-byte original.

    Args:
        text: Text about to be persisted or displayed in a private call trace.
        secret: Optional exact API key to redact in addition to recognizable credential
            patterns; None adds no exact-key replacement.

    Returns:
        A pair of redacted text and a boolean indicating whether any replacement changed it.
    """
    original = text
    if secret:
        text = text.replace(secret, "[REDACTED_API_KEY]")
    text = re.sub(r"\bsk-ant-[A-Za-z0-9_-]{8,}", "[REDACTED_API_KEY]", text)
    return text, text != original


def write_private_text(path: Path, text: str) -> None:
    """Replace a file using owner-only temporary/output permissions on this Unix host.

    The caller must provide an existing parent directory. Replacement prevents a half-
    written final file, not a multi-file transaction or power-loss guarantee. This is
    distinct from normal snapshot storage, which uses process permissions.

    Args:
        path: Local destination file path for the private artifact.
        text: Complete UTF-8 text to write.

    Returns:
        None. Atomically replace the destination with owner-only text; the parent directory
        must already exist.
    """
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
    temporary.replace(path)


def call_trace_path(directory: Path, batch_id: str) -> Path:
    """Derive a fixed local filename; a call ID is never an arbitrary filesystem path.

    Args:
        directory: Local filesystem directory for this one saved research run.
        batch_id: Application-owned extraction batch/call identifier, validated before being
            used in a filename.

    Returns:
        A Path to the fixed private trace filename inside the run directory; invalid IDs
        raise ResearchError.
    """
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", batch_id):
        raise ResearchError("Invalid extraction call ID.")
    folder = directory / "extraction_calls"
    path = folder / f"{batch_id}.json"
    if folder.is_symlink() or path.is_symlink():
        raise ResearchError("Extraction trace paths must not be symlinks.")
    return path


def save_trace(directory: Path, trace: dict, secret: str | None) -> None:
    """Save the caller's diagnostic trace with credential redaction so malformed replies
    remain inspectable without storing the configured key.

    The caller constructs the trace's allowed fields. This writer serializes that
    dictionary; it does not enforce a separate field allowlist.

    Args:
        directory: Local filesystem directory for this one saved research run.
        trace: In-memory extraction-call trace dictionary containing fields selected by
            the caller; do not include an unfiltered SDK object or exception.
        secret: Optional exact API key to redact in addition to recognizable credential
            patterns; None adds no exact-key replacement.

    Returns:
        None. Save the selected, credential-redacted trace fields in the run directory.
    """
    path = call_trace_path(directory, trace["batch_id"])
    encoded, redacted = redact_text(json.dumps(trace, ensure_ascii=False, indent=2), secret)
    safe = json.loads(encoded)
    safe["credential_redaction_applied"] = redacted
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        write_private_text(path, json.dumps(safe, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        raise MonitoringError("Cannot save the private extraction call trace.") from None


def start_trace(directory: Path, session, attempt, messages, secret: str) -> dict:
    """Save application input before invocation, with any credential redaction labelled.

    The trace includes the resolved extraction profile and its independent output cap.
    A prepared trace proves input was assembled, not that a request reached the provider.
    The node reserves the attempt separately; finish_trace adds the result. The recorded
    schema precedes LangChain's wire-format transformation.

    Args:
        directory: Local filesystem directory for this one saved research run.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        attempt: ExtractionAttempt holding the batch, source, passage and request metadata.
        messages: Ordered (role, text) application messages sent to the model; may contain
            private research content.
        secret: Optional exact API key to redact in addition to recognizable credential
            patterns; None adds no exact-key replacement.

    Returns:
        The mutable prepared trace dictionary, already saved before provider invocation.
    """
    trace = {
        "schema_version": "1.0", "origin": "captured", "run_id": session.session_id,
        "batch_id": attempt.batch_id, "source_id": attempt.source_id, "passage_id": attempt.passage_id,
        "input_sha256": attempt.input_sha256, "source_text_sha256": attempt.text_sha256,
        "messages": [{"role": role, "content": content} for role, content in messages],
        "output_schema": ExtractionBatch.model_json_schema(),
        "schema_boundary": "Pydantic schema supplied to LangChain, not the SDK-transformed wire schema",
        "requested_model": session.request.model, "max_output_tokens": session.request.extraction_settings.max_output_tokens,
        "extraction_settings": session.request.extraction_settings.model_dump(mode="json"),
        "effort": "low", "timeout_seconds": session.request.model_timeout_seconds,
        "max_retries": 0, "application_retries": session.request.model_retries,
        "status": "prepared", "response_text": None, "response_text_truncated": False,
        "non_text_blocks_omitted": False,
    }
    save_trace(directory, trace, secret)
    return trace


def capture_answer(trace: dict, raw) -> None:
    """Copy answer text into the in-memory trace, omitting non-text provider blocks.

    Long text is capped at the larger of 128,000 characters or sixteen characters
    per configured output token, with a visible truncation flag. This diagnostic
    bound grows with experimental output allowances; it is not a tokenizer. Multiple text blocks are
    joined with newlines, so this is selected answer text, not the raw HTTP body. save_trace
    performs redaction before the trace reaches disk.

    Args:
        trace: Mutable call-trace dictionary that will later be redacted and saved.
        raw: Provider AIMessage or compatible raw reply; only visible text content is
            copied.

    Returns:
        None. Mutate the trace with bounded visible answer text and truncation metadata.
    """
    content = raw.content
    if isinstance(content, str):
        answer = content
    else:
        blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "text"]
        answer = "\n".join(str(block.get("text", "")) for block in blocks)
        trace["non_text_blocks_omitted"] = len(blocks) != len(content)
    limit = max(128_000, 16 * (trace.get("max_output_tokens") or 0))
    trace["response_text"] = answer[:limit]
    trace["response_text_truncated"] = len(answer) > limit


def finish_trace(directory: Path, trace: dict, attempt, secret: str) -> None:
    """Add the final receipt, including parse failures and unknown usage, without exception
    bodies.

    Args:
        directory: Local filesystem directory for this one saved research run.
        trace: Mutable trace prepared before this extraction call.
        attempt: ExtractionAttempt containing the final call outcome, parse status and known
            token usage.
        secret: Optional exact API key to redact in addition to recognizable credential
            patterns; None adds no exact-key replacement.

    Returns:
        None. Merge final attempt status and usage into the trace and save it.
    """
    for field in ("status", "error_code", "response_model", "stop_reason", "input_tokens",
                  "output_tokens", "total_tokens", "elapsed_seconds", "diagnostics", "last_attempt_id"):
        trace[field] = getattr(attempt, field)
    trace["parsed_result"] = attempt.result.model_dump(mode="json") if attempt.result is not None else None
    save_trace(directory, trace, secret)
