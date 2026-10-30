"""Save complete model-provider failure details in private diagnostic files.

Before a failed request is classified or retried, the shared executor calls this
writer to retain the exception message, linked cause messages and response body,
including non-JSON errors. Known credentials are redacted and the redaction is
labeled; HTTP headers are not copied. This preserves diagnostic information without
putting private payloads in ordinary progress events. A failed write propagates
instead of silently claiming the failure record was saved.
"""
import json
import re
import anthropic

from projectclarity.run_monitor import MonitoringError


def save_provider_error(directory, attempt_id, error, secret=None):
    """Save a complete error receipt before retry/failure policy can discard context.

    Args:
        directory: Existing private run directory, not the shared output root.
        attempt_id: Application-generated model attempt identifier such as M1.
        error: Caught exception; each unique cause/context message is retained.
        secret: Configured credential to redact along with known key patterns.
    Returns:
        Run-relative receipt path, or None when no provider SDK error exists in
        the chain (for example local answer validation). Writes an owner-only JSON file without message
        truncation; raises MonitoringError on unsafe paths or failed persistence.
        Operational diagnostics should expose the path, not the response contents.
    """
    # Lazy import reuses the existing redaction/private writer without coupling
    # provider initialization to the planning module imported by trace helpers.
    from projectclarity.extraction.extraction_call_tracing import redact_text, write_private_text
    if not re.fullmatch(r'M[1-9][0-9]*', attempt_id):
        raise MonitoringError('Invalid model-attempt error receipt identifier.')
    folder = directory / 'provider_errors'
    path = folder / f'{attempt_id}.json'
    if folder.is_symlink() or path.is_symlink():
        raise MonitoringError('Private provider-error paths must not be symlinks.')
    chain, seen = [], set()
    current = error
    provider_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        provider_failure = provider_failure or isinstance(current, anthropic.APIError)
        record = dict(exception_type=type(current).__name__, message=str(current))
        response = getattr(current, 'response', None)
        if response is not None:
            record['http_status'] = getattr(response, 'status_code', None)
            record['response_body'] = response.text
        chain.append(record)
        current = current.__cause__ or current.__context__
    if not provider_failure:
        return None
    encoded, redacted = redact_text(json.dumps(dict(schema_version='1.0',
        attempt_id=attempt_id, errors=chain), ensure_ascii=False), secret)
    receipt = json.loads(encoded)
    receipt['credential_redaction_applied'] = redacted
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
        write_private_text(path, json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    except OSError as exc:
        raise MonitoringError('Cannot preserve the provider error receipt.') from exc
    return f'provider_errors/{attempt_id}.json'
