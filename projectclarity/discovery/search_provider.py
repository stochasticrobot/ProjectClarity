"""Replaceable free-search adapter using one explicit DDGS or Wikipedia backend.

Queries go to the search engine, not a model or database. A short-lived worker
gets no API-key environment and a 20-second wall-clock ceiling. The provider's
internal HTTP traffic is NOT counted as source-retrieval traffic. The calling node
enforces acquisition time and any explicit query cap. Results are leads, never evidence.
"""
import json
import os
import subprocess
import sys
import time


class SearchProviderError(Exception):
    """Stable operational code plus bounded diagnostics for private search receipts."""

    def __init__(self, code, diagnostics=None):
        """Separate safe event codes from provider detail so failures remain diagnosable.

        Args:
            self: Exception carrying one failed search attempt.
            code: Application-owned error code safe for operational events.
            diagnostics: Optional bounded, redacted provider error dictionary for private files.

        Returns:
            None. Initialize the exception; str(self) exposes only the stable code.
        """
        super().__init__(code)
        self.diagnostics = diagnostics or {}


def search_web(query, max_results, monitor=None, *, provider="duckduckgo"):
    """Run the configured free search adapter in a killable subprocess so search has a deadline
    and does not inherit model credentials.

    Validate bounded worker output before exposing leads to source selection. Provider-
    internal HTTP traffic is separate from the document-retrieval HTTP allowance; this
    function does not fetch result pages.

    Args:
        query: Search-query text to send to the configured web-search provider.
        max_results: Maximum number of result records requested for this query.
        monitor: Optional RunMonitor for progress events and cooperative cancellation; None
            disables those hooks.
        provider: Explicit supported DDGS or Wikipedia backend. Fallback belongs to
            the caller; this worker never switches engines.

    Returns:
        A list of bounded dictionaries containing result URL, title and snippet. Provider
        failures raise SearchProviderError; cancellation propagates.
    """
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT"}}
    process = subprocess.Popen([sys.executable, "-m", "projectclarity.discovery.search_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=environment)
    started = time.monotonic()
    request = json.dumps({"query": query, "max_results": max_results, "provider": provider})
    try:
        while True:
            if monitor:
                monitor.check_cancel()
            if time.monotonic() - started >= 20:
                raise SearchProviderError("search_timeout")
            try:
                stdout, _ = process.communicate(input=request, timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                request = None
        if len(stdout) > 100000:
            raise SearchProviderError("search_provider_failed")
        if process.returncode:
            try:
                error = json.loads(stdout)['error']
                if error['code'] not in {'search_timeout', 'search_rate_limited', 'search_provider_failed',
                        'search_provider_blocked', 'search_service_unavailable', 'search_http_error',
                        'search_response_unrecognized'}:
                    raise ValueError
                from projectclarity.extraction.extraction_call_tracing import redact_text
                diagnostics = {key: redact_text(str(error.get(key, ''))[:2000])[0] for key in ('type', 'message')}
                diagnostics.update({key: error[key] for key in ('http_status', 'response_kind', 'retry_after_seconds') if key in error})
            except (ValueError, KeyError, TypeError):
                raise SearchProviderError('search_provider_failed', {'type': 'worker_exit', 'message': 'Worker returned no usable diagnostic'}) from None
            raise SearchProviderError(error['code'], diagnostics)
        try:
            response = json.loads(stdout)
            if not isinstance(response, list) or len(response) > max_results:
                raise ValueError
            return response
        except (ValueError, TypeError):
            raise SearchProviderError("invalid_search_response") from None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
