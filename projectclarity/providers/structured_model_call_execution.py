"""Execute one capture, review, source-selection or coverage call under the shared allowance.

Nodes own prompts and domain checks. This helper owns the repeated mechanics:
reserve a request before sending, retain malformed answers for debugging, total
reported usage, and emit content-free progress. No automatic retry or fallback
occurs. A response passing its JSON schema is NOT yet authorized application data.

Private llm_calls/*.json files contain application messages and answers, not HTTP
headers, hidden reasoning or provider exception bodies. They are mode 0600. A
call receipt is also saved in the session so budget/failure gaps remain visible.
Cancellation is cooperative; an in-flight blocking call may still incur usage.
"""
import hashlib
import json
import re
import time

from projectclarity.extraction.extraction_call_tracing import capture_answer, redact_text, write_private_text
from projectclarity.providers.structured_model_provider import create_structured_model
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.run_monitor import MonitoringError
from projectclarity.session_storage import save_session
from projectclarity.knowledge.capture_budget import capture_remaining

MAX_MESSAGE_CHARACTERS = 48_000


def save_call_trace(directory, trace, secret):
    """Save the caller's processing-call trace with credential redaction so failed calls
    remain inspectable without writing the configured key.

    The caller selects the diagnostic fields; this writer serializes the supplied
    dictionary rather than filtering it through an additional field allowlist.

    Args:
        directory: Local filesystem directory for this one saved research run.
        trace: Processing-call trace dictionary containing selected inputs, output and
            receipt fields.
        secret: Optional exact API key to redact in addition to recognizable credential
            patterns; None adds no exact-key replacement.

    Returns:
        None. Write the bounded, redacted private processing-call trace.
    """
    folder = directory / "llm_calls"
    path = folder / (trace["call_id"] + ".json")
    if folder.is_symlink() or path.is_symlink():
        raise MonitoringError("Private call paths must not be symlinks.")
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
        encoded, redacted = redact_text(json.dumps(trace, ensure_ascii=False, indent=2), secret)
        safe = json.loads(encoded)
        safe["credential_redaction_applied"] = redacted
        write_private_text(path, json.dumps(safe, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        raise MonitoringError("Cannot save a private model-call trace.") from None


def invoke_structured(session, output_root, *, call_id, task, prompt_version, messages, schema, key_reader, monitor=None):
    """Execute one structured model request under the run's shared request allowance so review,
    knowledge capture, search selection and coverage obey the same overall ceiling.
    Capture also obeys its explicitly configured auxiliary allowance, checked before
    credentials or model construction. Failed capture calls consume that allowance.

    Reserve the attempt, capture private inputs and bounded output, validate adapter
    completion, and persist usage and a safe receipt. Failure returns no parsed answer
    rather than allowing later nodes to treat a malformed or interrupted response as usable
    knowledge.

    Args:
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        output_root: Local filesystem directory containing research-run subdirectories; not
            a URL or an individual run folder.
        call_id: Application call identifier selecting one saved model attempt, not a
            filesystem path.
        task: Workflow task name used to identify the model call in receipts and displays.
        prompt_version: Version label identifying the application prompt used for this model
            attempt.
        messages: Ordered (role, text) application messages sent to the model; may contain
            private research content.
        schema: Pydantic response-model class defining the structured answer expected from
            the provider.
        key_reader: Required zero-argument callable returning the model API key when a
            request needs it; the calling node supplies its credential reader.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.

    Returns:
        A pair of parsed schema-model answer (or None on failure) and a receipt dictionary
        describing the attempt, usage and safe error code.
    """
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", call_id):
        raise ValueError("Invalid internal call ID")
    receipt = {"call_id": call_id, "task": task, "status": "not_attempted", "error_code": None,
        "trace_path": None, "input_tokens": None, "output_tokens": None, "total_tokens": None}
    session.processing_calls.append(receipt)
    started, trace, secret, parsed = time.monotonic(), None, None, None
    try:
        if monitor:
            monitor.check_cancel()
        if session.usage.requests_attempted >= session.request.budget.max_requests:
            receipt["error_code"] = "request_limit"
            return None, receipt
        if task == 'accumulate_knowledge' and capture_remaining(session) <= 0:
            receipt['error_code'] = 'knowledge_request_limit'
            return None, receipt
        serialized = json.dumps(messages, ensure_ascii=False)
        if len(serialized) > MAX_MESSAGE_CHARACTERS:
            receipt["error_code"] = "input_too_large"
            return None, receipt
        secret = key_reader()
        trace = {"schema_version": "1.0", "call_id": call_id, "task": task,
            "prompt_version": prompt_version, "run_id": session.session_id,
            "messages": [{"role": role, "content": text} for role, text in messages],
            "input_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
            "output_schema": schema.model_json_schema(), "requested_model": session.request.model,
            "max_output_tokens": session.request.budget.max_output_tokens,
            "max_retries": 0, "timeout_seconds": 60, "effort": "low",
            "response_text": None, "non_text_blocks_omitted": False,
            "response_text_truncated": False, "status": "prepared"}
        receipt["trace_path"] = f"llm_calls/{call_id}.json"
        save_call_trace(output_root / session.session_id, trace, secret)
        if secret and secret in serialized:
            receipt["error_code"] = "credential_in_input"
            return None, receipt
        model = create_structured_model(secret, session, schema)
        if monitor:
            monitor.check_cancel()
        session.usage.requests_attempted += 1
        receipt["status"] = "started"
        save_session(session, output_root)
        if monitor:
            monitor.observe("model_call_started", stage=task, batch_id=call_id,
                llm_requests=session.usage.requests_attempted, trace_path=receipt["trace_path"])
        if monitor:
            monitor.model_input(task, call_id, messages, schema, secret)
        response = model.invoke(messages)
        raw = response["raw"]
        if monitor:
            monitor.model_output(task, call_id, raw, secret)
        capture_answer(trace, raw)
        for field in ("input_tokens", "output_tokens", "total_tokens"):
            value = (raw.usage_metadata or {}).get(field)
            receipt[field] = value
            prior = getattr(session.usage, field)
            setattr(session.usage, field, prior + value if prior is not None and value is not None else None)
        receipt["stop_reason"] = raw.response_metadata.get("stop_reason")
        receipt["response_model"] = raw.response_metadata.get("model_name")
        session.usage.stop_reason = receipt["stop_reason"]
        session.usage.response_model = receipt["response_model"]
        if receipt["stop_reason"] != "end_turn":
            receipt["error_code"] = "incomplete_model_output"
        elif response.get("parsing_error") is not None or response.get("parsed") is None:
            receipt["error_code"] = "invalid_output_schema"
        else:
            parsed = schema.model_validate(response["parsed"])
            if secret and secret in parsed.model_dump_json():
                parsed = None
                receipt["error_code"] = "credential_in_response"
        receipt["status"] = "completed" if parsed is not None else "failed"
        if monitor:
            monitor.check_cancel()
    except (RunCancelled, KeyboardInterrupt, EOFError):
        receipt["error_code"] = "cancelled"
        session.status = "cancelled"
        session.error = "Cancelled; saved work retained. In-flight model usage may be unknown."
        parsed = None
    except MonitoringError:
        raise
    except Exception:
        receipt["error_code"] = "provider_or_adapter_error"
        parsed = None
    finally:
        if receipt["status"] == "started":
            receipt["status"] = "failed"
            session.usage.input_tokens = session.usage.output_tokens = session.usage.total_tokens = None
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if trace is not None:
            trace.update(receipt)
            trace["parsed_result"] = parsed.model_dump(mode="json") if parsed is not None else None
            save_call_trace(output_root / session.session_id, trace, secret)
        save_session(session, output_root)
        if monitor:
            monitor.observe("model_call_finished", stage=task, batch_id=call_id,
                call_status=receipt["status"], error_code=receipt["error_code"],
                input_tokens=receipt["input_tokens"], output_tokens=receipt["output_tokens"],
                call_seconds=receipt["elapsed_seconds"], trace_path=receipt["trace_path"])
    return parsed, receipt
