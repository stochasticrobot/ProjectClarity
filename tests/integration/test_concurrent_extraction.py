"""Prove real extraction graph overlap, bounded dispatch and lossless stopping.

Only document fetching and model replies are simulated. Barriers establish actual
simultaneous calls rather than relying on elapsed-time speed claims.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock, get_ident
from uuid import uuid4
from unittest.mock import patch
import unittest
from fastapi.testclient import TestClient
from projectclarity.documents.document_pipeline import DocumentPipeline
from projectclarity.ux.web_app import create_app
from projectclarity.workflow.research_sessions import Sessions
from tests.integration.test_document_pipeline import ExtractCaller


class Reader:
    """Supply eight independently identified documents without network access."""

    def fetch(self, path):
        """Return a complete text document for the supplied fixture URL.

        Args: path: Test source URL with a numeric final component.
        Returns: URL, content headers and UTF8 evidence, with no redirects.
        """
        return path, {'content-type': 'text/plain'}, f'Company {path.rsplit("/",1)[1]} builds AI tools.'.encode(), []


class ConcurrentCaller(ExtractCaller):
    """Measure in-flight calls, optionally requesting stop or producing repairs."""

    def __init__(self, limit, stop, cancel=False, repair=False, fail=False):
        """Create a first-wave barrier matching the expected worker count.

        Args: limit: Expected simultaneous slots; stop: Shared Event; cancel:
            Stop after first wave starts; repair: Produce one rejected proposal;
            fail: Fail one initial request while others remain valid.
        Returns: None; test counters are protected independently of production.
        """
        super().__init__()
        self.lock = Lock(); self.barrier = Barrier(limit, timeout=5)
        self.limit, self.stop, self.cancel, self.repair, self.fail = limit, stop, cancel, repair, fail
        self.active = self.peak = self.started = 0

    def __call__(self, stage, request, sid):
        """Block the first wave until all configured slots are genuinely occupied.

        Args: stage: Unique label; request: Actual production payload; sid: Owner.
        Returns: Grounded extraction or withdrawal; may fail one request. Finally
            always decrements active count, including exceptions and cancellation.
        """
        with self.lock:
            self.active += 1; self.started += 1
            number = self.started; self.peak = max(self.peak, self.active)
        try:
            if number <= self.limit:
                self.barrier.wait()
                if self.cancel:
                    self.stop.set()
            if self.fail and number == 1:
                raise RuntimeError('Deliberate fixture failure')
            if stage.startswith('extract-repair-'):
                with self.lock:
                    self.calls.append((stage, request, sid))
                return json.dumps({'entities': [], 'decisions': [
                    {'id':'P2', 'action':'withdraw', 'reason':'Not in source.', 'fact':None}]})
            with self.lock:
                result = json.loads(super().__call__(stage, request, sid))
            if self.repair:
                invalid = dict(result['facts'][0], quote='This text is absent.')
                result['facts'].append(invalid)
            return json.dumps(result)
        finally:
            with self.lock:
                self.active -= 1


class ConcurrencyTests(unittest.TestCase):
    """Exercise the production document graph and public setting contract."""

    def run_case(self, limit=4, cancel=False, repair=False, fail=False):
        """Run acquisition/extraction and inspect serialized results after draining.

        Args: limit: Saved slot limit; cancel: Stop first wave; repair: Include
            rejected facts; fail: Make one initial model request fail.
        Returns: None; asserts overlap, cap, unique receipts, counters, ordering,
            single-thread publication and preservation of finished calls on stop.
        """
        with TemporaryDirectory() as tmp:
            stop = Event(); caller = ConcurrentCaller(limit, stop, cancel, repair, fail)
            state = {'session_id':str(uuid4()), 'research_question':'AI companies',
                     'information_needed':[], 'extraction_concurrency':limit}
            writers = set()
            def publish(snapshot):
                """Check serialized state snapshots on the coordinator thread.

                Args: snapshot: Real mutable session state at publication.
                Returns: None; JSON serialization and nonnegative counts must work.
                """
                writers.add(get_ident()); json.dumps(snapshot)
                self.assertGreaterEqual(snapshot['run']['facts_awaiting_repair'], 0)
            runner = DocumentPipeline(Path(tmp), state, publish, caller, stop,
                paths=[f'https://fixture.test/{i}' for i in range(8)], reader=Reader())
            runner.execute()
            self.assertEqual(caller.peak, limit)
            self.assertEqual(len(writers), 1)
            run = state['run']; count = limit if cancel else 8
            self.assertEqual(run['chunks_finished'], count)
            self.assertEqual(run['facts'], count - int(fail))
            self.assertEqual(run['model_calls'], caller.started)
            self.assertEqual(run['facts_awaiting_repair'], 0)
            self.assertEqual(run['status'], 'cancelled' if cancel else 'partial' if fail else 'completed')
            self.assertEqual(len(list(runner.root.glob('document-*/chunk-*.json'))), count)
            knowledge = json.loads((runner.root/'knowledge.json').read_text())
            ids = [row['document_id'] for row in knowledge['chunks']]
            self.assertEqual(ids, sorted(set(ids)))
            self.assertEqual(len(ids), count-int(fail))
            if repair:
                self.assertEqual(run['facts_withdrawn'], count)
                self.assertEqual(caller.started, 2*count)

    def test_four_overlapping_calls_and_repairs_share_limit(self):
        """Verify extraction and repairs together never exceed four slots.

        Args: None.
        Returns: None; runs the actual document graph with valid grounded outputs.
        """
        self.run_case(repair=True)

    def test_stop_drains_inflight_without_scheduling_remaining_chunks(self):
        """Save first-wave results but never dispatch the second wave after stop.

        Args: None.
        Returns: None; cancellation preserves every in-flight successful receipt.
        """
        self.run_case(cancel=True)

    def test_single_slot_and_failed_chunk(self):
        """Honor a user-selected serial limit and isolate a provider failure.

        Args: None.
        Returns: None; other chunks still finish and enter the knowledge index.
        """
        self.run_case(limit=1, fail=True)

    def test_api_persists_limit_and_rejects_invalid_values(self):
        """Check the public question-setting contract without definition model calls.

        Args: None.
        Returns: None; validates default six, strict bounds and immutable reuse
            of an existing session ID. No model work is performed by this check.
        """
        with TemporaryDirectory() as tmp:
            sessions = Sessions(Path(tmp), ExtractCaller())
            try:
                with patch.object(sessions, 'work'), TestClient(create_app(sessions)) as client:
                    for value in (0, 17, 1.5, True, '4'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'extraction_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    for value in (0, 17, 1.5, True, '6'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'selection_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    for value in (0, 17, 1.5, True, '6'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'triage_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    for value in (0, 17, 1.5, True, '6'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'investigation_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    for value in (0, 17, 1.5, True, '6'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'link_ranking_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    for value in (0, 17, 1.5, True, '6'):
                        response = client.post('/api/setup', json={'session_id':str(uuid4()),
                            'question':'Research', 'report_selection_concurrency':value})
                        self.assertEqual(response.status_code, 422)
                    sid = str(uuid4())
                    response = client.post('/api/setup', json={'session_id':sid, 'question':'Research'})
                    self.assertEqual(response.json()['extraction_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['extraction_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['selection_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['triage_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['investigation_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['link_ranking_concurrency'], 6)
                    self.assertEqual(sessions.read(sid)['report_selection_concurrency'], 6)
                    replay = client.post('/api/setup', json={'session_id':sid, 'question':'Research',
                        'extraction_concurrency':2})
                    self.assertEqual(replay.json()['extraction_concurrency'], 6)
            finally:
                sessions.close()
