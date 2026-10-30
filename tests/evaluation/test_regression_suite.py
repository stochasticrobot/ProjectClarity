"""Check that the offline test runner reports failures and missing work accurately.

Artificial test outcomes exercise the gate's counts, skips, expected failures,
source-change detection and lifecycle tracking. A discovered test must actually
finish before it can pass. Offline success must leave live provider and research
quality checks marked not run. No real research is started.
"""
import io
import unittest

from evaluation.regression_suite import LedgerResult, summarize


class RegressionGateTests(unittest.TestCase):
    """Test evidence accounting independently of the application under test."""

    def test_complete_offline_pass_never_promotes_live_acceptance(self):
        """Keep synthetic mechanics separate from real provider acceptance.

        Args:
            self: Assertion owner.
        Returns:
            None; verifies only the offline gate may become green.
        """
        result = summarize(['a'], {'a': {'status': 'passed'}})
        self.assertTrue(result['offline_passed'])
        self.assertFalse(result['release_ready'])
        self.assertTrue(all(row['status'] == 'not_run' for row in result['live_gates'].values()))

    def test_nonpassing_classifications_block_gate(self):
        """Reject hidden, incomplete and explicitly failing test outcomes alike.

        Args:
            self: Assertion owner iterating every supported non-pass category.
        Returns:
            None; skips and expected failures are release blockers, not passes.
        """
        for status in ['failed', 'error', 'skipped', 'expected_failure', 'unexpected_success', 'not_run']:
            with self.subTest(status=status):
                self.assertFalse(summarize(['a'], {'a': {'status': status}})['offline_passed'])

    def test_missing_extra_empty_and_stale_evidence_block_gate(self):
        """Reject partial populations and results from changing source code.

        Args:
            self: Assertion owner.
        Returns:
            None; every incomplete evidence form leaves the gate blocked.
        """
        cases = [([], {} , {}), (['a'], {}, {}),
                 (['a'], {'a': {'status': 'passed'}, 'b': {'status': 'passed'}}, {}),
                 (['a'], {'a': {'status': 'passed'}}, {'changed_files': ['changed.py']}),
                 (['a'], {'a': {'status': 'passed'}}, {'missing_modules': ['critical.test']})]
        for ids, outcomes, options in cases:
            with self.subTest(ids=ids, options=options):
                self.assertFalse(summarize(ids, outcomes, **options)['offline_passed'])

    def test_lifecycle_ledger_does_not_assume_discovered_cases_ran(self):
        """Distinguish entered tests from finished tests and never-run inventory.

        Args:
            self: Assertion owner using a test case without executing its body.
        Returns:
            None; only actual lifecycle callbacks populate execution records.
        """
        result = LedgerResult(io.StringIO(), True, 0)
        case = unittest.FunctionTestCase(lambda: None)
        self.assertEqual(result.finished_ids, set())
        result.startTest(case)
        self.assertEqual(result.started_ids, {case.id()})
        self.assertEqual(result.finished_ids, set())
        result.stopTest(case)
        self.assertEqual(result.finished_ids, {case.id()})
