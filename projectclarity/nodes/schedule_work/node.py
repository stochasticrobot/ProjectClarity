"""Production graph adapter for schedule work."""
import time
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.nodes.schedule_work.workflow_control import schedule_work


def schedule(state, output_root, monitor, runtime):
    """Refresh the shared acquisition clock before the central scheduling decision.

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

