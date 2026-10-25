"""Execute explicit free-engine fallback with persisted, bounded cooldown recovery."""
import time

from projectclarity.discovery.search_engines import cool_engine, engine_order, engine_state, search_readiness
from projectclarity.discovery.search_provider import SearchProviderError
from projectclarity.retrieval.retrieval_limits import RunCancelled


def execute_search(session, receipt, search_call, monitor=None, web=None, persist=None):
    """Try available engines in order, returning control instead of sleeping through cooldowns.

    Args:
        session: Run with immutable engine order, limits and durable provider health.
        receipt: Logical query receipt; attempts accumulate across deferred rounds.
        search_call: Single-engine adapter or transport substitute; never auto-search.
        monitor: Optional cancellation/progress monitor.
        web: Optional shared acquisition runtime bounding elapsed time, bytes and HTTP.
        persist: Optional callback saving reservations, outcomes and cooldowns.

    Returns:
        Normalized leads from the first nonempty success, or an explicitly empty
        result if at least one engine confirmed emptiness. Raise SearchProviderError
        on total failure or cooldown; caller retains the query for bounded recovery.
        Cancellation propagates. Multi-engine failures immediately advance to the
        next engine; only legacy single-engine transient errors retry immediately.
    """
    attempts = receipt.setdefault('attempts', [])
    providers = engine_order(session.request)
    last_error, empty_provider = None, None
    for provider in providers:
        health = engine_state(session, provider)
        if health.get('exhausted') or health.get('ready_at', 0) > time.time():
            continue
        retries = session.request.search_retries if len(providers) == 1 else 0
        for index in range(retries + 1):
            gap = session.request.search_min_interval_seconds * (2 ** index)
            due = session.search.get('last_provider_attempt_finished', 0) + gap
            while time.time() < due:
                check_search_budget(monitor, web)
                time.sleep(min(0.1, max(0, due - time.time())))
            check_search_budget(monitor, web)
            attempt = {'attempt': len(attempts) + 1, 'provider': provider,
                'status': 'started', 'started_at': time.time()}
            attempts.append(attempt)
            receipt['provider'] = provider
            if persist:
                persist()
            if monitor:
                monitor.observe('search_engine_started', query_id=receipt.get('query_id'), search_provider=provider)
            try:
                results = search_call(receipt['query'], session.request.search_results_per_query, monitor,
                    **({'provider': provider} if provider != 'duckduckgo' else {}))
                if not isinstance(results, list) or any(not isinstance(r, dict) or
                        not isinstance(r.get('url'), str) for r in results):
                    raise SearchProviderError('invalid_search_response')
                attempt.update(status='completed', result_count=len(results))
                session.search.setdefault('engines', {}).setdefault(provider, {}).update(
                    ready_at=0, last_success_at=time.time())
                if results:
                    return results
                empty_provider = provider
                break
            except SearchProviderError as exc:
                if str(exc) == 'fetch_budget_exhausted':
                    raise
                last_error = exc
                attempt.update(status='failed', error_code=str(exc), diagnostics=exc.diagnostics)
                if str(exc) in {'search_timeout', 'search_service_unavailable'} and index < retries:
                    continue
                health = cool_engine(session, provider, exc)
                attempt['cooldown_until'] = health['ready_at']
                attempt['engine_exhausted'] = health['exhausted']
                break
            except (RunCancelled, KeyboardInterrupt, EOFError):
                attempt.update(status='cancelled', error_code='cancelled')
                raise
            finally:
                session.search['last_provider_attempt_finished'] = time.time()
                attempt['finished_at'] = time.time()
                if persist:
                    persist()
                if monitor:
                    monitor.observe('search_engine_finished', query_id=receipt.get('query_id'),
                        search_provider=provider, call_status=attempt['status'],
                        error_code=attempt.get('error_code'), cooldown_until=attempt.get('cooldown_until'),
                        engine_exhausted=attempt.get('engine_exhausted', False))
    if empty_provider:
        receipt['provider'] = empty_provider
        return []
    ready_at, terminal = search_readiness(session)
    if not terminal:
        receipt['ready_at'] = ready_at
    raise last_error or SearchProviderError(terminal or 'search_cooldown')


def check_search_budget(monitor, web):
    """Guard every engine attempt and pacing wait using the shared run resources.

    Args:
        monitor: Optional cooperative cancellation interface.
        web: Optional retrieval runtime with the existing time/HTTP/byte ceilings.

    Returns:
        None if allowed; raise cancellation or SearchProviderError before any new
        attempt when a ceiling is exhausted. No budget is increased or reset.
    """
    if monitor:
        monitor.check_cancel()
    if web and (time.monotonic() >= web.budget.started + web.budget.limits.max_run_seconds
            or web.budget.http_requests >= web.budget.limits.max_http_requests
            or web.budget.bytes_read >= web.budget.limits.max_total_bytes):
        raise SearchProviderError('fetch_budget_exhausted')
