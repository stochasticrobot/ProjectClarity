"""Verify auxiliary capture limits through production CLI and the actual model boundary."""
import json
import unittest

from projectclarity.knowledge.capture_budget import capture_requests
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.knowledge.contracts import KnowledgeBatch
from projectclarity.research_schemas import ResearchSession
from tests.knowledge import test_knowledge_pipeline as knowledge_fixtures


class CaptureObjectiveBudgetTests(unittest.TestCase):
    """Auxiliary exhaustion must preserve research capacity and pending capture work."""

    def test_new_cli_defaults_do_not_enable_capture(self):
        """Exercise --extract without either capture opt-in or the legacy disable flag.

        Args:
            self: Real CLI fixture with a local document and synthetic model transport.

        Returns:
            None. Assert only planning/extraction calls, no capture store, and an explicit
            zero auxiliary allowance in the saved request.
        """
        fixture = knowledge_fixtures.KnowledgePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        tasks = []

        def respond(request):
            """Return only permitted research responses and reject accidental capture.

            Args:
                request: Actual provider SDK request emitted by the production graph.

            Returns:
                A fixture plan or empty extraction; unexpected tasks fail the test.
            """
            title = json.loads(request.content)['output_config']['format']['schema']['title']
            tasks.append(title)
            if title == 'ResearchPlan':
                return fixture.cli.response()
            self.assertEqual(title, 'ExtractionBatch')
            return fixture.cli.response(text=json.dumps({'entities': [], 'claims': []}))

        code, _, err = fixture.cli.run_cli(respond, '--document', str(fixture.document),
            '--extract', '--max-requests', '10', include_urls=False, plan_only=False,
            knowledge=None, details=False)
        _, saved = fixture.cli.saved_session()
        self.assertEqual(code, 0, err)
        self.assertEqual(tasks, ['ResearchPlan', 'ExtractionBatch'])
        self.assertEqual(saved['request']['knowledge_max_requests'], 0)
        self.assertFalse(saved['request']['accumulate_knowledge'])
        self.assertFalse((fixture.cli.output / 'knowledge.sqlite3').exists())
        self.assertEqual(saved['request']['budget']['max_output_tokens'], 8192)

    def test_one_capture_call_does_not_consume_research_allowance_or_fail_research(self):
        """Stop optional broad capture at its cap and continue answer extraction normally.

        Args:
            self: Production CLI fixture with a long document and synthetic model replies.

        Returns:
            None. Assert independent cap, retained windows, subsequent research calls,
            separate objective status and provider-boundary enforcement.
        """
        fixture = knowledge_fixtures.KnowledgePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.document.write_text(fixture.text + 'Supporting context without new named subjects.\n' * 170)
        _, saved, out, _ = fixture.run_capture('--max-requests', '20', '--knowledge-max-requests', '1')
        self.assertEqual(saved['knowledge']['requests_attempted'], 1)
        self.assertEqual(saved['knowledge']['pause_reason'], 'knowledge_request_limit')
        self.assertEqual(saved['knowledge']['objective_status'], 'incomplete')
        self.assertTrue(any(w['status'] == 'pending' for w in saved['knowledge']['windows']))
        self.assertEqual(saved['status'], 'candidates_saved')
        self.assertGreater(saved['usage']['requests_attempted'], 2)
        self.assertLess(saved['usage']['requests_attempted'], 20)
        self.assertIn('broad capture: 1/1', out)
        session = ResearchSession.model_validate(saved)
        before = session.usage.requests_attempted

        def forbidden_key():
            """Fail if an exhausted auxiliary request reaches credential resolution.

            Args:
                None.

            Returns:
                Never returns: this path must be blocked before model construction.
            """
            self.fail('Exhausted auxiliary budget reached provider boundary')

        answer, receipt = invoke_structured(session, fixture.cli.output, call_id='capture_after_cap',
            task='accumulate_knowledge', prompt_version='test', messages=[], schema=KnowledgeBatch,
            key_reader=forbidden_key)
        self.assertIsNone(answer)
        self.assertEqual(receipt['error_code'], 'knowledge_request_limit')
        self.assertEqual(session.usage.requests_attempted, before)
        self.assertEqual(capture_requests(session), 1)
