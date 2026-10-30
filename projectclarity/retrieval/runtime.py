"""Share one download budget and web reader throughout a workflow invocation.

Search and source retrieval must count toward the same byte, request and elapsed-
time limits. retrieval_runtime creates or reuses their live counters and reader,
restoring saved usage when research resumes. A callback saves charged acquisition
usage before a network request can leave the process. These runtime objects stay
outside LangGraph's serialized state; only their usage records are saved.
"""
import time
from projectclarity.retrieval.retrieval_limits import RetrievalBudget, RetrievalLimits
from projectclarity.retrieval.public_url_fetcher import PublicWebReader
from projectclarity.session_storage import save_session


def retrieval_runtime(session, runtime, monitor):
    """Share acquisition counters and a clock starting at the first search or retrieval.

    Args:
        session: Typed session with saved counters and immutable retrieval allowances.
        runtime: Invocation-local dictionary retaining the shared web reader, never checkpointed.
        monitor: Optional monitor supplying cancellation and content-free budget events.

    Returns:
        The existing or newly created PublicWebReader. Construction makes no network call;
        resumed invocations carry forward saved elapsed time, HTTP attempts and bytes.
    """
    if 'web' not in runtime:
        prior = session.retrieval_usage
        budget = RetrievalBudget(RetrievalLimits(**session.request.fetch_limits.model_dump()),
            bytes_read=prior.get('bytes_read', 0), http_requests=prior.get('http_requests', 0),
            started=time.monotonic() - prior.get('elapsed_seconds', 0),
            check_control=monitor.check_cancel if monitor else None,
            on_event=monitor.observe if monitor else None)
        runtime['web'] = PublicWebReader(budget)
    web = runtime['web']
    def persist_acquisition(event, **details):
        """Persist charged network usage before a request can leave this process.

        Args:
            event: Budget event distinguishing reservation, bytes and progress.
            **details: Safe operational metrics, not URLs, bodies or credentials.

        Returns:
            None. Save charges atomically for crash recovery and forward monitoring.
        """
        session.retrieval_usage = {**web.budget.summary(), 'checkpoint_wall_time': time.time()}
        session.workflow['blocked_origins'] = sorted(web.blocked_origins)
        if event in {'request_reserved', 'bytes_read'} and runtime.get('output_root'):
            save_session(session, runtime['output_root'])
        if monitor:
            monitor.observe(event, **details)
    web.budget.on_event = persist_acquisition
    web.blocked_origins.update(session.workflow.get('blocked_origins', []))
    return web

