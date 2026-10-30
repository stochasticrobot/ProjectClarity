"""Record which search providers may be tried and when failed providers may retry.

Search execution uses an explicit ordered list of engines rather than allowing a
library to silently choose one. A failure can put an engine into a cooldown: it
cannot be tried again until a saved time. Other enabled engines can remain usable.
These helpers read and update provider-health records in the session; they do not
send queries or sleep while waiting. Saved attempt limits also prevent endless
retry cycles across restarts.
"""
import time

SUPPORTED_SEARCH_PROVIDERS = {'duckduckgo', 'brave', 'google', 'yahoo', 'mojeek', 'startpage', 'wikipedia'}
PROVIDER_HOSTS = {name: name + '.com' for name in SUPPORTED_SEARCH_PROVIDERS}
PROVIDER_HOSTS.update(wikipedia='en.wikipedia.org', brave='search.brave.com')


def engine_order(request):
    """Resolve the explicitly enabled order without invoking library auto-selection.

    Args:
        request: Saved research request containing a primary engine and fallback list.

    Returns:
        Unique engine names in operator order; old requests retain their single engine.
    """
    return list(dict.fromkeys([request.search_provider, *request.search_fallbacks]))


def engine_state(session, provider):
    """Read saved health without resetting failures, cooldowns, or legacy breakers.

    Args:
        session: Research run whose search ledger owns provider health.
        provider: Explicit engine name being considered.

    Returns:
        Health dictionary. Old run-wide blocked markers remain terminal for their
        original provider; this function does not mutate the journal.
    """
    legacy = session.search.get('provider_blocked')
    if legacy and legacy.get('provider') == provider:
        return {'exhausted': True, 'last_error': legacy['reason']}
    return session.search.get('engines', {}).get(provider, {})


def search_readiness(session):
    """Find the earliest permitted search attempt across independent engine states.

    Args:
        session: Run with provider order and absolute persisted retry times.

    Returns:
        Pair of (ready_at, terminal_reason). A future ready_at is a cooldown,
        not a permanent block. If every engine is exhausted, return a reason.
    """
    states = [engine_state(session, p) for p in engine_order(session.request)]
    active = [s for s in states if not s.get('exhausted')]
    if active:
        return min(s.get('ready_at', 0) for s in active), None
    legacy = session.search.get('provider_blocked')
    return 0, legacy['reason'] if legacy else 'search_engines_exhausted'


def cool_engine(session, provider, error):
    """Persist a failed engine's cooldown while leaving other engines available.

    Args:
        session: Mutable run journal; caller persists after this update.
        provider: Failed explicit backend.
        error: SearchProviderError with bounded diagnostics and optional Retry-After.

    Returns:
        Updated health dictionary. Cooldown-enabled runs have a lifetime failure bound;
        successful probes never reset it. A zero cooldown preserves legacy terminal
        behavior for blocks. No provider request or sleep occurs here.
    """
    state = session.search.setdefault('engines', {}).setdefault(provider, {})
    failures = state.get('failures', 0) + 1
    delay = max(session.request.search_cooldown_seconds * 2 ** (failures - 1),
        error.diagnostics.get('retry_after_seconds', 0) or 0)
    state.update(failures=failures, last_error=str(error), diagnostics=error.diagnostics,
        ready_at=time.time() + delay,
        exhausted=failures > session.request.search_recovery_rounds or delay == 0)
    if session.request.search_cooldown_seconds == 0 and len(engine_order(session.request)) == 1:
        if str(error) in {'search_provider_blocked', 'search_rate_limited'}:
            session.search['provider_blocked'] = {'provider': provider, 'reason': str(error),
                'diagnostics': error.diagnostics}
        else:
            # Old requests could continue different queries after a local failure.
            # Preserve that permission without replaying the failed logical query.
            state.update(exhausted=False, ready_at=0)
    return state
