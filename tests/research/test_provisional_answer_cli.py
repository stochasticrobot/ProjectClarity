"""Check that the research command prints and saves findings without another model call.

Controlled external replies drive the normal workflow, then tests inspect the
terminal answer and saved report. Unreviewed or contrary claims must not become
positive findings, and changed source text must invalidate saved support during
inspection. Budget exhaustion still permits reporting what was already collected.
The tests do not measure live answer quality or generated prose.
"""

import contextlib
import hashlib
import io
import json
import runpy
import unittest
from unittest.mock import patch

from tests.discovery import test_branch_progress_cli as branches
from tests.retrieval.retrieval_test_fixtures import public_dns


class ProvisionalAnswerCLITests(unittest.TestCase):
    """Exercise real extraction, adjudication, source checking and terminal output."""

    def setUp(self):
        """Compose the existing transport harness without replacing application nodes.

        Args:
            self: Test case owning the isolated run and network-blocking fixtures.

        Returns:
            None. Register temporary-file and HTTP-pool cleanup for every test.
        """
        self.branch = branches.BranchProgressCLITests()
        self.branch.setUp()
        self.addCleanup(self.branch.doCleanups)
        self.cli = self.branch.flow.cli

    def run_answer(self, *flags, review=True, details=False):
        """Execute the production research entry point and retain stdout for assertions.

        Args:
            self: Test case supplying page-specific provider responses.
            flags: Additional real terminal entry point flags, including budget and crawl controls.
            review: Enable semantic review; False tests pending-claim presentation.
            details: Select the terminal interface's full-plan display instead of its default summary.

        Returns:
            Exit code, stdout, run directory and saved session. All source/network
            boundaries are synthetic; workflow, evidence and reporting remain real.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            code, out, err = self.cli.run_cli(self.branch.provider, '--extract',
                *(['--review-claims'] if review else []), '--max-requests', '6',
                '--quiet-progress', *flags, plan_only=False, details=details, source_dns=public_dns)
        directory, session = self.cli.saved_session()
        self.assertIn('# Current answer (provisional)', out, err)
        return code, out, directory, session

    def inspect_answer(self, run_id, *flags):
        """Invoke the read-only answer subcommand with live networking prohibited.

        Args:
            self: Test case holding its isolated output root.
            run_id: Saved run to inspect, never resume.
            flags: Optional answer command flags such as --details.

        Returns:
            Captured terminal answer after a successful real __main__ invocation.
        """
        output = io.StringIO()
        with (patch('sys.argv', ['projectclarity', 'answer', run_id, '--output-dir', str(self.cli.output), *flags]),
              patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')),
              contextlib.redirect_stdout(output)):
            with self.assertRaises(SystemExit) as result:
                runpy.run_module('projectclarity', run_name='__main__')
        self.assertEqual(result.exception.code, 0)
        return output.getvalue()

    def test_default_prints_substantive_answer_and_saves_evidence_without_extra_calls(self):
        """Check normal terminal interface findings, citations and artifacts rather than helper metadata.

        Args:
            self: Synthetic source with one supported answer statement.

        Returns:
            None. Require exactly planning/extraction/review calls and preserved spans.
        """
        statement = 'Dr Example studies model privacy at a Canadian university.'
        self.branch.page('research', claim=statement)
        code, out, directory, session = self.run_answer()
        self.assertEqual(code, 0)
        self.assertIn(statement, out)
        self.assertIn('[S1_P1_C1]', out)
        self.assertIn('https://example.org/research', out)
        self.assertEqual(session['usage']['requests_attempted'], 3)
        self.assertEqual(len(self.cli.requests), 3)
        answer = json.loads((directory / 'answer.json').read_text())
        self.assertEqual(answer['findings'][0]['citations'][0]['evidence'][0]['quote'], statement)
        self.assertIn(statement, (directory / 'answer.md').read_text())

    def test_budget_stop_and_details_still_print_answer(self):
        """Require findings after all requests are spent, including the details path.

        Args:
            self: Two useful pages followed by an unfinished observed child.

        Returns:
            None. Preserve partial exit status and prohibit a reporting model call.
        """
        self.branch.page('research', ['a'], claim='First supported answer.')
        self.branch.page('a', ['b'], claim='Second supported answer.')
        self.branch.page('b')
        code, out, directory, session = self.run_answer('--follow-links', details=True)
        self.assertEqual(code, 1)
        self.assertEqual(session['workflow']['stop_reason'], 'budget_exhausted')
        self.assertIn('First supported answer.', out)
        self.assertIn('Second supported answer.', out)
        self.assertIn('model_request_limit', out)
        self.assertEqual(session['usage']['requests_attempted'], 6)
        self.assertEqual(len(self.cli.requests), 6)
        self.assertTrue((directory / 'answer.md').is_file())
        output = io.StringIO()
        with (patch('sys.argv', ['projectclarity', 'resume', session['session_id'],
                  '--output-dir', str(self.cli.output), '--quiet-progress']),
              patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'offline-answer-test-key'}),
              patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')),
              contextlib.redirect_stdout(output), contextlib.redirect_stderr(output)):
            with self.assertRaises(SystemExit) as resumed:
                runpy.run_module('projectclarity', run_name='__main__')
        self.assertEqual(resumed.exception.code, 1)
        self.assertIn('# Current answer (provisional)', output.getvalue())
        self.assertIn('Second supported answer.', output.getvalue())
        self.assertEqual(json.loads((directory / 'session.json').read_text())['usage']['requests_attempted'], 6)

    def test_unreviewed_claim_remains_a_lead_and_inspection_is_read_only(self):
        """Prevent anchored-but-unreviewed text from becoming a supported answer.

        Args:
            self: Run with extraction enabled and review deliberately disabled.

        Returns:
            None. Inspection prints the pending lead only with details, makes no
            requests and leaves every original run file byte-identical.
        """
        self.branch.page('research', claim='An unreviewed hypothesis.')
        _, out, directory, session = self.run_answer(review=False)
        self.assertNotIn('An unreviewed hypothesis.', out)
        self.assertIn('No reviewed, goal-linked answer claims', out)
        answer = json.loads((directory / 'answer.json').read_text())
        self.assertEqual(len(answer['leads']), 1)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
        inspected = self.inspect_answer(session['session_id'], '--details')
        self.assertIn('An unreviewed hypothesis.', inspected)
        self.assertIn('Review: pending', inspected)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_changed_source_is_excluded_on_saved_answer_inspection(self):
        """Recheck evidence instead of trusting a stale saved answer artifact.

        Args:
            self: Completed run whose normalized source is subsequently altered.

        Returns:
            None. The answer command excludes its previously supported assertion.
        """
        self.branch.page('research', claim='Previously supported assertion.')
        _, _, directory, session = self.run_answer()
        (directory / session['documents'][0]['text_path']).write_text('Changed evidence.')
        inspected = self.inspect_answer(session['session_id'])
        self.assertNotIn('Previously supported assertion.', inspected)
        self.assertIn('Excluded 1 previously anchored claims', inspected)

    def test_counterevidence_is_visible_but_not_a_positive_finding(self):
        """Keep disqualifying evidence separate even when its review says supported.

        Args:
            self: Source explicitly countering an eligibility assumption.

        Returns:
            None. Counterevidence prints by default and the positive list stays empty.
        """
        self.branch.page('research', claim='The researcher moved outside Canada.', role='counterevidence')
        _, out, directory, _ = self.run_answer()
        self.assertIn('Counterevidence and exclusions', out)
        self.assertIn('The researcher moved outside Canada.', out)
        answer = json.loads((directory / 'answer.json').read_text())
        self.assertEqual(answer['findings'], [])
        self.assertEqual(len(answer['counterevidence']), 1)
