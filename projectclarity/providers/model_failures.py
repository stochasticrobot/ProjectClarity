"""Classify model-service exceptions for retry decisions and progress reporting.

The request executor needs to distinguish a temporary timeout from authentication,
certificate, validation or unknown failures. These helpers inspect exception types
and permitted fields to produce application-owned categories and retry eligibility.
They do not copy arbitrary error messages into normal progress events.

Complete provider messages and response bodies are retained separately in private
provider_errors records before classification. This module is not a replacement
for those diagnostics and does not itself send a retry request.
"""
import math
import re
import ssl
import time
from email.utils import parsedate_to_datetime

import anthropic


def describe_model_failure(error, secret=None):
    """Extract allowlisted diagnostics and retry eligibility from typed SDK errors.

    Args:
        error: Caught exception; arbitrary messages and response bodies are never read.
        secret: Optional configured credential, excluded even from a request identifier.

    Returns:
        JSON-safe category, safe exception label, status, request ID and retry delay.
        Only known transport/status failures qualify; TLS failures never auto-retry.
    """
    result = {'category': 'adapter_error', 'exception_type': 'unclassified',
        'retryable': False, 'http_status': None, 'request_id': None,
        'retry_after_seconds': None}
    chain, current = [], error
    for _ in range(8):
        if current is None or any(current is item for item in chain):
            break
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, ssl.SSLError) for item in chain):
        result.update(category='tls_error', exception_type='SSLError')
        return result
    known = next((item for item in chain if isinstance(item,
        (anthropic.APIConnectionError, anthropic.APIStatusError))), None)
    if isinstance(known, anthropic.APITimeoutError):
        result.update(category='timeout', exception_type='APITimeoutError', retryable=True)
    elif isinstance(known, anthropic.APIConnectionError):
        result.update(category='connection_error', exception_type='APIConnectionError', retryable=True)
    elif isinstance(known, anthropic.APIStatusError):
        status = known.status_code
        status = status if type(status) is int and 100 <= status <= 599 else None
        retryable = status in {408, 409, 429} or status is not None and status >= 500
        category = ('authentication_error' if status in {401, 403} else
            'rate_limit' if status == 429 else 'service_error' if status and status >= 500 else
            'request_error')
        result.update(category=category, exception_type='APIStatusError',
            http_status=status, retryable=retryable)
        identifier = getattr(known, 'request_id', None)
        if (isinstance(identifier, str) and re.fullmatch(r'req_[A-Za-z0-9_-]{1,160}', identifier)
                and (not secret or secret not in identifier)):
            result['request_id'] = identifier
        headers = known.response.headers
        try:
            if headers.get('retry-after-ms') is not None:
                delay = float(headers['retry-after-ms']) / 1000
            elif headers.get('retry-after') is not None:
                value = headers['retry-after']
                try:
                    delay = float(value)
                except ValueError:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
            else:
                delay = None
            if delay is not None and math.isfinite(delay):
                result['retry_after_seconds'] = max(0.0, delay)
        except (ValueError, TypeError, OverflowError):
            pass
    return result


class ModelCallFailure(RuntimeError):
    """Carry safe terminal attempt metadata without exposing an SDK exception."""

    def __init__(self, code, diagnostics, attempt_id=None):
        """Keep a failed logical call inspectable after bounded recovery ends.

        Args:
            self: Safe application exception passed to the owning research node.
            code: Application-owned outcome, such as request_limit or provider_or_adapter_error.
            diagnostics: Allowlisted failure dictionary, never provider response text.
            attempt_id: Optional identifier of the last reserved wire attempt.

        Returns:
            None. Initialize the safe code and structured diagnostic attributes.
        """
        super().__init__(code)
        self.code = code
        self.diagnostics = diagnostics
        self.attempt_id = attempt_id
