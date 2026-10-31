"""Check that source discovery cannot bypass reserved reading work or duplicate paid reads.

Deterministic source scenarios run through the public command, graph and SDK with
simulated external responses. Tests vary source arrival and extraction concurrency,
then inspect saved passage attempts and request records for continued reading,
explicit reservations and reuse of identical acquired text. Assertions state
required behavior; the test-run result, not this introduction, says whether it passes.
Do not hide a failure by marking the case skipped or expectedFailure.
"""
import json
import unittest

from tests.research.adversarial_source_scenarios import (
    SourceScenario, extraction_costs, repeated_content_reads,
)


class ReleaseInvariantTests(unittest.TestCase):
    """Keep source growth from bypassing the controller's reading decisions."""

    def check_continuation(self, concurrency, arrivals, seed):
        """Require acquired productive work to advance between discovery rounds.

        Args:
            self: Assertion owner creating an independent production run.
            concurrency: Real model worker setting.
            arrivals: Number of additional scope sources returned per query.
            seed: Source-row ordering, independent of scheduler policy.
        Returns:
            None; fails on a stalled long source while more discovery happens.
            Preconditions prove multiple rounds and remaining work actually exist.
            Explicit Small packing keeps enough passages for sustained admissions.
            The 240-call test allowance funds three adaptive reading/discovery
            cycles; it changes no production default or saved-run allowance.
        """
        scenario = SourceScenario(self, concurrency=concurrency, arrivals=arrivals, seed=seed)
        _, _, saved, _ = scenario.run(budget=240, profile='small')
        self.assertLessEqual(saved['usage']['requests_attempted'], 240)
        rounds = {s['search']['round']: s for s in scenario.snapshots}
        counts = []
        for snapshot in rounds.values():
            rows = [a for a in snapshot['extraction']['attempts']
                    if a['source_id'] == scenario.case.primary]
            counts.append((sum(a['status'] == 'completed' for a in rows),
                           sum(a['status'] == 'not_attempted' for a in rows)))
        self.assertGreaterEqual(len(counts), 3, 'Fixture must exercise sustained admissions.')
        self.assertGreater(counts[0][0], 0, 'Primary source was never successfully read.')
        stalled = [(before, after) for before, after in zip(counts, counts[1:])
                   if before[1] > 0 and after[0] <= before[0]]
        self.assertEqual(stalled, [],
            f'Productive continuation stalled: concurrency={concurrency}, arrivals={arrivals}, '
            f'round_counts={counts}, extraction_costs={extraction_costs(saved)}')

    def test_continuation_with_serial_burst_admissions(self):
        """Check long-source progress under four new sources per search query.

        Args:
            self: Production fixture owner.
        Returns:
            None; source growth must not consume all serial continuation turns.
        """
        self.check_continuation(1, 4, 109)

    def test_continuation_with_three_workers_and_burst_admissions(self):
        """Exercise the concurrency and source-arrival combination missed previously.

        Args:
            self: Production fixture owner.
        Returns:
            None; extra workers must not hide extraction inside preparation.
        """
        self.check_continuation(3, 4, 211)

    def test_continuation_with_three_workers_and_gradual_admissions(self):
        """Compare slower arrivals on a different deterministic source ordering.

        Args:
            self: Production fixture owner.
        Returns:
            None; progress must survive gradual as well as burst discovery.
        """
        self.check_continuation(3, 1, 307)

    def test_metadata_preparation_never_spends_model_requests(self):
        """Make inventory-only preparation a real execution invariant, not a label.

        Args:
            self: Production fixture owner reconciling actual request receipts.
        Returns:
            None; all paid reads must have an explicit controller passage grant.
        """
        scenario = SourceScenario(self, count=80, arrivals=0)
        _, _, saved, _ = scenario.run(budget=35, search=False)
        costs = extraction_costs(saved)
        actual = sum(a['task'] == 'extract_knowledge' for a in saved['model_attempts'])
        self.assertGreater(actual, 0)
        self.assertEqual(sum(costs.values()), actual, 'Ledger oracle did not account for every read.')
        self.assertEqual(costs.get('preparation', 0), 0,
                         f'Metadata preparation dispatched paid extraction: {costs}')

    def test_identical_content_does_not_receive_duplicate_paid_reads(self):
        """Reproduce two case-distinct URLs serving exactly the same document.

        Args:
            self: Fixture owner; does not assume URLs are case-insensitive.
        Returns:
            None; provenance may retain both URLs but identical acquired passages
            must not incur another extraction simply because the URL differs.
        """
        scenario = SourceScenario(self, count=80, arrivals=0)
        scenario.duplicate()
        _, _, saved, _ = scenario.run(budget=60, search=False)
        documents = [d for d in saved['documents'] if d['status'] == 'retrieved']
        hashes = [d['text_sha256'] for d in documents]
        self.assertLess(len(set(hashes)), len(hashes), 'Duplicate bytes must actually be acquired.')
        self.assertEqual(repeated_content_reads(saved), {},
                         'Distinct source IDs caused repeated paid reads of identical passages.')

    def test_case_variant_with_changed_content_is_not_suppressed(self):
        """Prevent a deduplication fix from conflating case-sensitive destinations.

        Args:
            self: Fixture owner serving different values at the two URLs.
        Returns:
            None; both versions are processed and unresolved single-value alternatives remain
            visible rather than manufactured into an agreed complete report.
        """
        scenario = SourceScenario(self, count=20, arrivals=0)
        scenario.duplicate(changed=True)
        _, directory, saved, _ = scenario.run(budget=60, search=False)
        versions = {d['source_id']: d['text_sha256'] for d in saved['documents']
                    if d['status'] == 'retrieved' and d['source_id'] in {'S1', 'S3'}}
        self.assertEqual(len(set(versions.values())), 2)
        read = {a['source_id'] for a in saved['extraction']['attempts'] if a['status'] == 'completed'}
        self.assertTrue(set(versions) <= read)
        report = json.loads((directory / 'answer.json').read_text())['progress']['goals'][0]['results']
        self.assertGreater(report['pending'], 0)
        self.assertTrue(any(len(row['field_status']['value']['supported_values']) > 1
                            for row in report['records']))
        self.assertNotEqual(saved['workflow'].get('stop_reason'), 'criteria_satisfied')
