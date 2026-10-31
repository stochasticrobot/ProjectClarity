"""Prepare one explicit correction from a known completed, rejected response.

Task adapters choose recoverable errors and validate corrected meaning. This shared
mechanism reads immutable private traces, never dispatches requests or retries an
unknown outcome. Callers must reserve the correction durably before invoking the
ordinary budgeted executor. It is not automatic recovery for every model task.
"""
import json


def correction_data(directory, call_id, error_code, recoverable_errors, expected_messages=None):
    """Recover exact rejected output only when its complete outcome is known.

    Args:
        directory: Run directory owning private llm_calls receipts.
        call_id: Application-generated original call identifier.
        error_code: Task validation failure, possibly after successful parsing.
        recoverable_errors: Task-owned explicit allowlist of validation failures.
        expected_messages: Original task messages, when available; exact equality
            binds a saved failure to the current request before correction.
    Returns:
        Correction data and parsed rejected object, or None when the trace is
        missing, incomplete, interrupted or not eligible. No writes or model calls.
        The original response is never clipped or treated as accepted evidence.
    """
    if error_code not in recoverable_errors or not call_id or any(
            c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in call_id):
        return None
    try:
        trace = json.loads((directory / 'llm_calls' / (call_id + '.json')).read_text())
    except (OSError, ValueError):
        return None
    if (trace.get('call_id') != call_id or trace.get('stop_reason') != 'end_turn'
            or trace.get('status') not in {'completed', 'failed'}
            or trace.get('error_code') not in {None, 'invalid_output_schema'}
            or trace.get('response_text_truncated') or not trace.get('response_text')):
        return None
    if expected_messages is not None and trace.get('messages') != [
            dict(role=role, content=content) for role, content in expected_messages]:
        return None
    raw = trace['response_text']
    try:
        previous = json.loads(raw)
    except ValueError:
        previous = None
    return (dict(previous_call_id=call_id, previous_response=raw,
        validation_error=error_code, validation_issues=trace.get('validation_issues', [])),
        previous if isinstance(previous, dict) else {'requires_detail': True})
