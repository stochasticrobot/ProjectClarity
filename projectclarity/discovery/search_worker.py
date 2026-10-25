"""Small free-search subprocess: normalize leads only; never retrieve result pages.

The parent supplies a hard wall-clock deadline and removes credential variables.
One named backend avoids hidden metasearch fallback. Backend HTML/rate-limit
failures retain bounded diagnostics, not silently replaced by another provider.
"""
import json
import sys
from projectclarity.discovery.search_transport import observe_engine
from projectclarity.discovery.search_engines import SUPPORTED_SEARCH_PROVIDERS
from projectclarity.discovery.search_provider import SearchProviderError


def main():
    """Read one request and execute its explicit DDGS engine or Wikipedia API.

    Clamp returned records and text, and emit a bounded JSON response for the supervising
    process. Return bounded redacted failure details instead of discarding the cause;
    never switch to an unrequested fallback provider.

    Args:
        None.

    Returns:
        Exit code 0 with a bounded JSON result list, or 1 with private error metadata on
        stdout. Explicit empty-result markers return an empty list successfully;
        challenges, HTTP errors and unrecognized empty responses retain distinct
        codes. The parent separates diagnostics from content-free event codes.
    """
    observations = []
    try:
        from ddgs import DDGS
        from ddgs.engines import ENGINES
        request = json.loads(sys.stdin.read(4096))
        query, limit = request["query"], request["max_results"]
        provider = request.get('provider', 'duckduckgo')
        if provider not in SUPPORTED_SEARCH_PROVIDERS:
            raise ValueError('Unsupported search provider')
        if not isinstance(query, str) or not 1 <= len(query) <= 300 or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('Invalid query or result limit')
        if provider == 'wikipedia':
            from projectclarity.discovery.wikipedia_search import search_wikipedia
            print(json.dumps(search_wikipedia(query, limit)))
            return 0
        # DDGS silently falls back to auto if a requested engine is unavailable.
        # Refuse that path so configuration remains an actual provider boundary.
        if provider not in ENGINES['text']:
            raise ValueError('Configured search engine is unavailable; automatic fallback refused')
        DDGS.threads = 1
        with observe_engine(provider, observations), DDGS(timeout=8) as search:
            results = search.text(query, backend=provider, max_results=limit)
        result = [{"url": str(r.get("href", ""))[:2048], "title": str(r.get("title", ""))[:300],
                   "snippet": str(r.get("body", ""))[:700]} for r in results[:limit]]
        print(json.dumps(result))
        return 0
    except Exception as exc:
        from projectclarity.extraction.extraction_call_tracing import redact_text
        name = type(exc).__name__
        code = str(exc) if isinstance(exc, SearchProviderError) else 'search_timeout' if name in {'TimeoutException', 'TimeoutError', 'ReadTimeoutError', 'ConnectTimeoutError'} else 'search_rate_limited' if name == 'RatelimitException' else 'search_provider_failed'
        observed = exc.diagnostics if isinstance(exc, SearchProviderError) else observations[-1] if observations else {}
        if observed.get('code'):
            code = observed['code']
        elif observed.get('response_kind') == 'explicit_empty' and str(exc) == 'No results found.':
            print('[]')
            return 0
        elif observations and str(exc) == 'No results found.':
            code = 'search_response_unrecognized'
        print(json.dumps({'error': {'code': code, 'type': name,
            'message': redact_text(str(exc)[:2000])[0],
            **{k: observed[k] for k in ('http_status', 'response_kind', 'retry_after_seconds') if k in observed}}}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
