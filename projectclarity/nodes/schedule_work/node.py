"""Update retrieval usage before asking the scheduler what to do next.

LangGraph calls schedule between research steps. The wrapper obtains the shared
retrieval runtime, which holds the active clock and download counters, and copies
its latest usage into the session before delegating to workflow_control.schedule_work.
If work resumed from saved usage, it restores that runtime first. No page is fetched
here. The delegated scheduler selects and saves the next task or stop decision.
"""
import time
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.nodes.schedule_work.workflow_control import schedule_work


def schedule(state, output_root, monitor, runtime):
    """Refresh elapsed retrieval time and download usage before selecting the next task.

    Reuse or restore the live retrieval runtime, then delegate task selection and
    persistence to the shared scheduler. This wrapper does not fetch a source.

    Args:
        state: Typed graph state after one complete work unit.
        output_root: Directory containing saved research runs.
        monitor: Optional progress and cancellation monitor.
        runtime: Invocation-local shared acquisition clients; never checkpointed.

    Returns:
        Persisted scheduler update; no request is sent by clock refresh.
    """
    if state['session'].retrieval_usage and 'web' not in runtime:
        retrieval_runtime(state['session'], runtime, monitor)
    if 'web' in runtime:
        state['session'].retrieval_usage = {**runtime['web'].budget.summary(), 'checkpoint_wall_time': time.time()}
    return schedule_work(state, output_root, monitor)

