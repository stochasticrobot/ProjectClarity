"""Build the evidence graph alongside reporting, without delaying or changing it.

A separate worker reads a fixed snapshot of the saved extraction records and
updates Neo4j. Its progress is saved separately from research progress so the two
workers cannot overwrite each other's results. No model calls are made here.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from threading import Lock
from uuid import uuid4


class ProjectionJobs:
    """Own independent graph jobs and their private progress files."""

    def __init__(self, root, collections):
        """Prepare a graph-only worker and mark interrupted projections explicitly.

        Args:
            root: Existing application session directory.
            collections: Repository that projects accepted records into Neo4j.
        Returns:
            None. Creates one executor, without querying Neo4j. Old running receipts
            become interrupted; restart never repeats research or model requests.
        """
        self.root = root
        self.collections = collections
        self.lock = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='knowledge-graph')
        self.futures = {}
        for path in root.glob('*/knowledge-graph.json'):
            status = json.loads(path.read_text())
            if status.get('status') in ('queued', 'running'):
                self.write(path.parent.name, {'status': 'interrupted',
                    'error': 'Graph preparation was interrupted. Refresh saved evidence to retry.'})

    def read(self, sid, fallback=None):
        """Read graph progress without altering the research or report state.

        Args:
            sid: Existing session UUID validated by the session service.
            fallback: Optional legacy embedded graph status for older sessions.
        Returns: Saved projection status, legacy status, or None if never scheduled.
            Invalid saved JSON or filesystem errors propagate instead of hiding loss.
        """
        path = self.root / sid / 'knowledge-graph.json'
        return json.loads(path.read_text()) if path.exists() else fallback

    def write(self, sid, status):
        """Atomically save graph-only progress without touching session.json.

        Args:
            sid: Owning existing session UUID.
            status: Sanitized progress/counts; never credentials or raw exceptions.
        Returns: None; writes an owner-only progress file. Filesystem errors propagate.
            Callers serialize updates for a session through the job registry.
        """
        path = self.root / sid / 'knowledge-graph.json'
        temporary = path.with_suffix('.tmp')
        with temporary.open('w') as stream:
            os.chmod(temporary, 0o600)
            json.dump(status, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)

    def submit(self, snapshot):
        """Queue a frozen evidence snapshot, reusing an already active job for it.

        Args:
            snapshot: Authoritative session state after collection, copied before
                reporting can mutate its own state. Source files remain unchanged.
        Returns: Future for graph counts/failure. Returns immediately after local
            bookkeeping, without waiting for Neo4j. Duplicate active requests share
            one future; a completed job can be explicitly refreshed from saved facts.
        """
        state = deepcopy(snapshot)
        sid = state['session_id']
        with self.lock:
            existing = self.futures.get(sid)
            if existing is not None and not existing.done():
                return existing
            job_id = str(uuid4())
            self.write(sid, {'status': 'queued', 'job_id': job_id})
            future = self.executor.submit(self.run, state, job_id)
            self.futures[sid] = future
            return future

    def run(self, snapshot, job_id):
        """Project saved facts independently and preserve a visible result on failure.

        Args:
            snapshot: Frozen research state with collection membership and sources.
            job_id: Unique identity of this scheduled projection attempt.
        Returns: Sanitized result with counts or an error. Updates only graph progress
            and Neo4j, never report inputs, research status or provider budgets.
        """
        sid = snapshot['session_id']
        self.write(sid, {'status': 'running', 'job_id': job_id})
        try:
            result = {'status': 'completed', 'job_id': job_id, **self.collections.sync(snapshot)}
        except Exception:
            result = {'status': 'failed', 'job_id': job_id,
                      'error': 'Graph update unavailable. Saved evidence and report generation are unaffected.'}
        self.write(sid, result)
        return result

    def close(self):
        """Finish accepted projection jobs before the database connection is closed.

        Args: None.
        Returns: None; waits for graph work only during application shutdown.
        """
        self.executor.shutdown(wait=True)
