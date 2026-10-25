"""Prove larger/deeper crawling through the CLI, not a helper-only simulation.

HTTP/DNS/model transports and pacing sleep are synthetic; graph scheduling,
selection validation, robots, parsing, budgets and saved receipts remain real.
This proves capacity, not model judgment, live throughput or research coverage.
"""
import unittest
import json
from collections import Counter
from urllib.parse import urlsplit
from unittest.mock import patch

from projectclarity.research_schemas import FetchLimits, ResearchRequest, ResearchSession
from tests.research import test_workflow_control_cli as workflow_tests


class CrawlCapacityCLITests(unittest.TestCase):
    """Exercise multiple website branches beyond the old page/HTTP/byte ceilings."""

    def setUp(self):
        """Create seven eight-page branches on two hosts, with cyclic return links.

        Args:
            self: Test owner retaining transport routes and isolated run files.

        Returns:
            None. Prepare 57 pages; comments charge bytes without extra passages.
        """
        self.flow = workflow_tests.WorkflowControlCLITests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        root_links = []
        for branch in range(7):
            host = 'example.org' if branch % 2 == 0 else 'deep.example'
            self.flow.routes[(host, '/robots.txt')] = (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')
            root_links.append(f'<a href="https://{host}/branch{branch}/1">Branch {branch}</a>')
            for depth in range(1, 9):
                target = f'https://{host}/branch{branch}/{depth + 1}' if depth < 8 else 'https://example.org/research'
                html = f'<main>Branch {branch}, depth {depth}.<a href="{target}">Continue investigation</a></main>'
                self.flow.routes[(host, f'/branch{branch}/{depth}')] = self.page(html)
        self.flow.routes[('example.org', '/research')] = self.page('<main>Research directory.' + ''.join(root_links) + '</main>')

    @staticmethod
    def page(html):
        """Pad raw HTML to test download accounting without inflating extracted text.

        Args:
            html: Visible fixture body containing observed links and unique text.

        Returns:
            A successful transport response with 640 KiB of comment padding.
        """
        return (200, {'Content-Type': 'text/html'}, html.encode() + b'<!--' + b'x' * (640 * 1024) + b'-->')

    def run_crawl(self, *flags):
        """Run real CLI traversal while skipping only wall-clock pacing sleeps.

        Args:
            self: Fixture with synthetic SDK/source responses and real application nodes.
            *flags: Operator overrides appended to the explicit 200-model-call ceiling.
                Branch-progress pruning is explicitly disabled for this capacity fixture,
                whose synthetic pages intentionally contain no useful claims.

        Returns:
            CLI exit code, run directory, saved state and captured stderr.
        """
        with patch('projectclarity.retrieval.retrieval_limits.time.sleep'):
            return self.flow.run_workflow('--max-requests', '200', '--max-unproductive-depth', '0', *flags)

    def test_default_capacity_crosses_all_old_limits_and_depth_two(self):
        """Require acquired documents at CLI capacity defaults with pruning disabled.

        Args:
            self: Seven-branch fixture with no source/HTTP/byte/depth overrides.

        Returns:
            None. Assert 57 distinct downloads, two hosts, depth eight, HTTP/byte
            use above old caps, loop deduplication and unchanged model allowance.
        """
        code, directory, saved, err = self.run_crawl()
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(len(saved['sources']), 57)
        self.assertEqual(len(saved['documents']), 57)
        self.assertTrue(all(d['status'] == 'retrieved' for d in saved['documents']))
        self.assertEqual(max(s['depth'] for s in saved['sources']), 8)
        self.assertEqual(len({urlsplit(s['url']).hostname for s in saved['sources']}), 2)
        usage = saved['retrieval_usage']
        self.assertEqual(usage['http_requests'], 59)
        self.assertGreater(usage['bytes_read'], 25 * 1024 * 1024)
        request = saved['request']
        self.assertEqual(request['max_sources'], 250)
        self.assertEqual(request['max_followup_sources'], 5)
        self.assertIsNone(request['max_link_depth'])
        self.assertEqual(request['fetch_limits']['max_http_requests'], 1000)
        self.assertEqual(request['fetch_limits']['max_total_bytes'], 250 * 1024 * 1024)
        self.assertEqual(request['budget']['max_requests'], 200)
        first = saved['discovery']['history'][0]
        self.assertEqual(len(first['accepted_ids']), 5)
        self.assertEqual(len(first['capacity_deferred_ids']), 2)
        counts = Counter((r[2]['headers']['Host'], r[1]) for r in self.flow.http.requests)
        self.assertTrue(all(count == 1 for count in counts.values()))
        self.assertIn('website hostnames: 2', err)
        self.assertIn('Pages/documents registered: 57/250', err)
        # Loading the actual saved run checks persistence, not just in-memory graph state.
        self.assertEqual(len(ResearchSession.model_validate_json((directory / 'session.json').read_text()).documents), 57)

    def test_explicit_page_alias_still_stops_registration(self):
        """Retain an operator's smaller cap despite larger defaults.

        Args:
            self: Same branching graph constrained through the new public alias.

        Returns:
            None. Assert ten registrations/downloads and a visible source-limit stop.
        """
        _, _, saved, _ = self.run_crawl('--max-pages', '10')
        self.assertEqual(len(saved['sources']), 10)
        self.assertEqual(sum(d['status'] == 'retrieved' for d in saved['documents']), 10)
        self.assertIn('source_limit', saved['workflow']['stop_details'])

    def test_legacy_defaults_and_expanded_operator_ceilings(self):
        """Preserve old API defaults while allowing larger validated CLI requests.

        Args:
            self: Test instance using schema validation without model or network calls.

        Returns:
            None. Assert compatibility and the newly supported operator ceilings.
        """
        self.assertEqual(FetchLimits().max_http_requests, 50)
        self.assertEqual(FetchLimits().max_total_bytes, 25 * 1024 * 1024)
        self.assertEqual(ResearchRequest(question='Research a topic').max_sources, 25)
        request = ResearchRequest(question='Research a topic', max_sources=1000,
            max_search_sources=1000, fetch_limits=FetchLimits(max_http_requests=10000,
                max_total_bytes=1024 * 1024 * 1024))
        self.assertEqual(request.max_sources, 1000)

    def test_valid_decline_does_not_reoffer_forever(self):
        """Distinguish full-batch deferral from an explicit model choice to stop.

        Args:
            self: Fixture whose transport declines all seven root leads.

        Returns:
            None. Assert exactly one selection call, one retrieved seed and no loop.
        """
        original = self.flow.provider
        selections = []

        def decline(request):
            """Return a valid empty selector response through the SDK boundary.

            Args:
                request: Serialized production SDK request; other tasks use the fixture.

            Returns:
                A synthetic provider response declining all offers, or ordinary response.
            """
            body = json.loads(request.content)
            if body['output_config']['format']['schema']['title'] == 'FollowupPlan':
                selections.append(body)
                return self.flow.cli.response(text=json.dumps({'gaps': [], 'selections': [],
                    'stop_reason': 'No further useful links in this synthetic response.'}))
            return original(request)

        self.flow.provider = decline
        code, _, saved, err = self.run_crawl()
        self.assertEqual(code, 0, err[-3000:])
        self.assertEqual(len(selections), 1)
        self.assertEqual(len(saved['sources']), 1)
        self.assertEqual(len(saved['documents']), 1)
