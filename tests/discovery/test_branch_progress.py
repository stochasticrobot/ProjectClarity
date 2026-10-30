"""Check when a linked-source path is eligible for further exploration.

Controlled request and source histories test older-run defaults and alternate
paths to the same destination. A blocked first parent must not hide a usable
second route. These are direct policy checks, not live browsing or model tests.
"""
import unittest

from projectclarity.discovery.branch_progress import branch_progress
from projectclarity.discovery.observed_link_inventory import inventory_links
from projectclarity.research_schemas import Budget, ResearchRequest, RetrievedDocument, URLSource
from tests.research.research_test_fixtures import example_session


class BranchProgressTests(unittest.TestCase):
    """Check legacy compatibility and route-local exclusion with minimal saved receipts."""

    def test_legacy_requests_do_not_silently_enable_pruning(self):
        """Keep old runs' policy when the new field is absent.

        Args:
            self: Test owner validating defaults and invalid thresholds.

        Returns:
            None. Assert legacy disabled behavior and rejection of negative/zero API caps.
        """
        request = ResearchRequest(question='Identify useful research')
        self.assertIsNone(request.max_unproductive_depth)
        for bad in (0, -1, 101):
            with self.subTest(limit=bad), self.assertRaises(ValueError):
                ResearchRequest(question='Identify useful research', max_unproductive_depth=bad)
        self.assertFalse(branch_progress(example_session())['enabled'])

    def test_unassessed_first_parent_does_not_hide_second_seed_link(self):
        """A paused parent must not mark a destination seen before a usable sibling offers it.

        Args:
            self: Fixture with an unresolved child listed before a second seed document.

        Returns:
            None. Assert the shared unseen target remains selectable through the seed.
        """
        session = example_session(plan_only=False)
        session.request = ResearchRequest(question=session.request.question, follow_links=True,
            review_claims=True, extract_knowledge=True, plan_only=False,
            budget=Budget(max_requests=20), max_link_depth=None, max_unproductive_depth=2)
        session.sources.extend([
            URLSource(source_id='S2', url='https://example.org/child', discovered_from_link='S1_L1', depth=1),
            URLSource(source_id='S3', url='https://example.org/seed')])
        for sid, path, href in [('S1', '/research', '/child'), ('S2', '/child', '/target'), ('S3', '/seed', '/target')]:
            session.documents.append(RetrievedDocument(source_id=sid, source_kind='url', status='retrieved',
                original_location='https://example.org' + path, final_url='https://example.org' + path,
                attempted_at=session.created_at, text_sha256=sid, links=[{'href': href, 'label': 'Observed lead'}]))
        links = inventory_links(session)
        child = next(l for l in links if l['link_id'] == 'S2_L1')
        seed = next(l for l in links if l['link_id'] == 'S3_L1')
        self.assertEqual(child['exclusion_reason'], 'branch_analysis_pending')
        self.assertTrue(seed['eligible'])
        self.assertIsNone(seed['exclusion_reason'])
