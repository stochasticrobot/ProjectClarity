"""Production graph adapter for wait for retry."""
import time
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.retrieval.retrieval_limits import RunCancelled


def wait_for_retry(state, output_root, monitor, runtime):
    """Wait briefly for a due retry only when no independent work is runnable.

    Args:
        state: Typed state containing an absolute retry readiness timestamp.
        output_root: Run storage directory; retained for the node service interface.
        monitor: Optional progress and cancellation monitor.
        runtime: Invocation-local shared acquisition clients; never checkpointed.

    Returns:
        Unchanged session; cancellation and elapsed acquisition limits are
        checked every quarter second through the shared budget during a wait
        of at most thirty seconds. Independent work is always scheduled first.
    """
    from projectclarity.retrieval.retrieval_limits import RetrievalError
    session = state['session']
    session.visited_nodes.append('wait_for_retry')
    web = retrieval_runtime(session, runtime, monitor)
    try:
        web.budget.wait(max(0, min(30, session.workflow['wait_until'] - time.time())))
    except RunCancelled:
        session.status = 'cancelled'
    except RetrievalError:
        pass  # Refreshed counters make the next scheduler decision explicit.
    return {'session': session}

