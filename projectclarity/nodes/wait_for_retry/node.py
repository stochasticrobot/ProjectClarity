"""Wait for a scheduled acquisition retry without bypassing run limits.

The graph enters this node only after the scheduler selects a retry wait instead
of runnable work. It waits at most thirty seconds through the shared retrieval
budget, which checks cancellation and elapsed limits in short increments. A
cancellation changes the session status; another retrieval-limit exception is
left for the next scheduling decision. No source or model request is sent here."""
import time
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.retrieval.retrieval_limits import RunCancelled


def wait_for_retry(state, output_root, monitor, runtime):
    """Wait for an eligible retry while observing cancellation and elapsed limits.

    Args:
        state: Graph-state dictionary containing a typed session whose workflow has
            wait_until as a Unix timestamp in seconds. The session is mutated.
        output_root: Run-storage root retained for the node interface; unused here.
        monitor: Optional cancellation/progress observer passed to retrieval runtime.
        runtime: Mutable invocation-local client cache, initialized when necessary;
            clients and credentials are never included in checkpoint state.
    Returns:
        A state-update dictionary containing the same session object. Records this
        node visit and marks the session cancelled if a RunCancelled exception occurs.
        Waits between zero and thirty seconds; budget checks occur during the wait.
        Other RetrievalError limits are left for the next scheduler turn. No model
        call, source fetch or explicit session save is performed by this adapter."""
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

