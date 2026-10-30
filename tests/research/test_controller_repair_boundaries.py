"""Check saved task reservations, duplicate-source work and final output ordering.

The real command-line workflow runs with simulated provider, search and page
responses. Tests inspect reservations before passage requests, interrupt and
resume duplicate-content work, and inspect exported files when the finished event
is emitted. They do not substitute successful metadata for actual request records.
"""
import hashlib
import json
import unittest
from unittest.mock import patch

from projectclarity.extraction.content_identity import content_aliases
from projectclarity.research_schemas import ResearchSession
from projectclarity.run_monitor import RunMonitor
from tests.research.adversarial_source_scenarios import SourceScenario, repeated_content_reads


class ControllerRepairBoundaryTests(unittest.TestCase):
    """Guard against repairs that merely conceal paid work or postpone file races."""

    def test_finished_event_publishes_complete_stable_exports(self):
        """Inspect files synchronously at terminal publication, not after a sleep.

        Args:
            self: Assertion owner with a real CLI graph invocation.
        Returns:
            None; finished status requires complete answer/coverage/graph exports,
            and returning through CLI presentation cannot subsequently change files.
        """
        scenario = SourceScenario(self, count=8, arrivals=0)
        original = RunMonitor.finish
        snapshots = []

        def observe_finish(monitor, status, *, error_type=None):
            """Check finalization at the real publication boundary before returning.

            Args:
                monitor: Real run observer holding its worker lock.
                status: Execution outcome supplied by production finalization.
                error_type: Optional original safe failure category.
            Returns:
                None. Calls the original monitor and records all run-file hashes.
            """
            for name in ['answer.json', 'answer.md', 'coverage.json', 'workflow_graph.txt']:
                self.assertTrue((monitor.directory / name).is_file(), name)
            self.assertFalse(monitor.snapshot['finished'])
            original(monitor, status, error_type=error_type)
            snapshots.append({str(p.relative_to(monitor.directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in monitor.directory.rglob('*') if p.is_file()})

        with patch.object(RunMonitor, 'finish', observe_finish):
            _, directory, _, _ = scenario.run(budget=40, search=False)
        self.assertEqual(len(snapshots), 1)
        after = {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(snapshots[0], after)

    def test_content_alias_survives_resume_without_replaying_uncertain_work(self):
        """Retain one extraction owner when a duplicate exists across an interruption.

        Args:
            self: Fixture owner interrupting an actual reserved canonical passage.
        Returns:
            None; aliases survive CLI resume, uncertain calls are not replayed,
            and neither the alias nor recovery resets budgets or invents evidence.
        """
        scenario = SourceScenario(self, count=80, arrivals=0)
        scenario.duplicate()
        scenario.case.crash_pid = 'P3'
        code, _, before, _ = scenario.run(budget=60, search=False)
        self.assertEqual(code, 77)
        self.assertEqual(before['extraction']['source_aliases']['S3']['source_id'], 'S1')
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            _, after = scenario.case.flow.resume(before['session_id'])
        self.assertEqual(after['extraction']['source_aliases'], before['extraction']['source_aliases'])
        self.assertEqual(scenario.case.events.count(('ExtractionBatch', 'S1', 'P3')), 1)
        self.assertLessEqual(after['usage']['requests_attempted'], 60)
        self.assertGreaterEqual(after['usage']['requests_attempted'], before['usage']['requests_attempted'])
        self.assertEqual(repeated_content_reads(after), {})
        self.assertFalse(any(c['source_id'] == 'S3' for c in after['extraction']['candidates']))
        self.assertEqual(len(after['sources']), 3)

    def test_alias_binding_rejects_changed_versions_and_chains(self):
        """Prevent persisted alias metadata from masking new or missing evidence.

        Args:
            self: Fixture owner inspecting actual production alias receipts.
        Returns:
            None; only direct matching acquired versions resolve, without evidence
            duplication or trusting an old alias after a document version changes.
        """
        scenario = SourceScenario(self, count=8, arrivals=0)
        scenario.duplicate()
        _, _, saved, _ = scenario.run(budget=40, search=False)
        session = ResearchSession.model_validate(saved)
        self.assertEqual(content_aliases(session), {'S3': 'S1'})
        alias_doc = next(d for d in session.documents if d.source_id == 'S3')
        alias_doc.text_sha256 = '0' * 64
        self.assertEqual(content_aliases(session), {})
        alias_doc.text_sha256 = session.extraction['source_aliases']['S3']['text_sha256']
        session.extraction['source_aliases']['S1'] = dict(source_id='S3', text_sha256=alias_doc.text_sha256)
        self.assertEqual(content_aliases(session), {})

    def test_paid_passages_are_inside_reserved_controller_waves(self):
        """Compare wire-dispatched batch IDs with the persisted active grants.

        Args:
            self: Fixture owner recording calls at the external provider boundary.
        Returns:
            None; every paid passage belongs to an explicit wave and waves respect
            configured concurrency. No grant is inferred afterward from outcomes.
        """
        scenario = SourceScenario(self, count=40, arrivals=0)
        provider = scenario.case.provider
        calls = []

        def check_grant(request):
            """Read the durable reservation immediately before the synthetic response.

            Args:
                request: Actual SDK request emitted by the production executor.
            Returns:
                Original fixture response after verifying extraction authorization.
            """
            wire = json.loads(request.content)
            if wire['output_config']['format']['schema']['title'] == 'ExtractionBatch':
                payload = json.loads(wire['messages'][0]['content'])
                saved = scenario.case.cli.saved_session()[1]
                active = saved['workflow']['tasks'][saved['workflow']['active']]
                bid = payload['source_id'] + '_' + payload['passage']['passage_id']
                self.assertIn(bid, active.get('batch_ids', []))
                self.assertLessEqual(len(active['batch_ids']), scenario.concurrency)
                calls.append(bid)
            return provider(request)

        scenario.case.flow.provider = check_grant
        scenario.run(budget=60, search=False)
        self.assertGreater(len(calls), 3)
