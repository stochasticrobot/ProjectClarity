"""Check repeated link-following, shared destinations and domain restrictions.

The public research command processes synthetic linked pages through the actual
graph, URL policy, parser and saved source records. Tests inspect successive hops,
intersecting paths, restrictions and allowance handling; controlled external
responses make the scenarios repeatable. These checks establish traversal
mechanics, not whether a live model selects every useful link.
"""
import json
import unittest

from tests.claim_review import test_review_and_discovery_integration as review_tests
from tests.retrieval.retrieval_test_fixtures import public_dns
from tests.extraction.extraction_test_fixtures import SOURCE_TEXT
from projectclarity.discovery.url_policy import load_url_configuration, url_denial, CONFIG_DIRECTORY
from projectclarity.research_schemas import URLPolicy


class RecursiveExplorationTests(unittest.TestCase):
    """A diamond-shaped frontier must join once, continue deeper and terminate on loops."""

    def setUp(self):
        """Supply held-out cross-site branches and model HTTP without replacing orchestration.

        Args:
            self: Test owner of an isolated terminal interface, source transport and provider fixture.

        Returns:
            None. Prepare root → two branches → shared destination → leaf → root loop.
            Source text and model replies are synthetic; no real network is permitted.
        """
        self.base = review_tests.ReviewDiscoveryIntegrationTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.overrides['FollowupPlan'] = self.choose_links
        self.rounds = []
        self.pages = {
            ('example.org', '/research'): ['https://alpha.example/start', 'https://beta.example/start'],
            ('alpha.example', '/start'): ['https://join.example/shared#one'],
            ('beta.example', '/start'): ['https://join.example/shared#two'],
            ('join.example', '/shared'): ['https://leaf.example/end'],
            ('leaf.example', '/end'): ['https://example.org/research#loop'],
        }
        for (host, path), links in self.pages.items():
            self.base.routes[(host, '/robots.txt')] = (200, {'Content-Type': 'text/plain'}, b'User-agent: *\nAllow: /\n')
            html = '<main><p>' + SOURCE_TEXT + '</p>' + ''.join(f'<a href="{url}">Follow research</a>' for url in links) + '</main>'
            self.base.routes[(host, path)] = (200, {'Content-Type': 'text/html'}, html.encode())

    def choose_links(self, body):
        """Choose only offered source IDs so the real frontier owns deduplication and policy.

        Args:
            self: Fixture recording every actual selector payload.
            body: Serialized provider request from the production structured-output adapter.

        Returns:
            A provider-shaped response selecting the current permitted batch with reasons.
        """
        payload = json.loads(body['messages'][0]['content'])
        self.rounds.append(payload)
        answer = {'gaps': ['Follow research leads.'], 'selections': [
            {'link_id': link['link_id'], 'goal_ids': ['G1'], 'reason': 'Explore this observed research direction.'}
            for link in payload['links'][:payload['max_selections']]], 'stop_reason': 'Selected available frontier.'}
        return self.base.cli.response(text=json.dumps(answer))

    def run_pipeline(self, *flags):
        """Invoke default recursive/open-site behavior using the real terminal entry point and graph.

        Args:
            self: Fixture with production-compatible source/model transports.
            *flags: Additional operator limits or policy switches for this run.

        Returns:
            Saved session and stderr after asserting successful command completion; no
            planning, selection, retrieval or storage functions are mocked.
        """
        code, _, err = self.base.cli.run_cli(self.base.provider, '--extract', '--review-claims',
            '--follow-links', '--max-requests', '60', *flags, plan_only=False,
            direct_entry=True, source_dns=public_dns, details=False)
        self.assertEqual(code, 0, err)
        _, session = self.base.cli.saved_session()
        return session, err

    def test_default_recursive_cross_site_diamond_reuses_join_and_stops_on_loop(self):
        """Prove production reaches depth three while both parent paths retain the shared destination.

        Args:
            self: Default open-origin recursive fixture with a cyclic terminal link.

        Returns:
            None. Assert one fetch/extraction of the join, both navigation connections,
            three successive selections, stable history and an explicit exhausted frontier.
        """
        session, err = self.run_pipeline()
        self.assertEqual([s['depth'] for s in session['sources']], [0, 1, 1, 2, 3])
        self.assertEqual(len(session['documents']), 5)
        self.assertEqual(sum(r[1] == '/shared' for r in self.base.http.requests), 1)
        self.assertEqual(len(session['extraction']['attempts']), 5)
        shared = next(s['source_id'] for s in session['sources'] if '/shared' in s['url'])
        paths = [p for p in session['discovery']['connections'] if p['target_source_id'] == shared]
        self.assertEqual({p['parent_source_id'] for p in paths}, {'S2', 'S3'})
        self.assertEqual(len(self.rounds), 3)
        self.assertEqual(len(session['discovery']['history']), 3)
        self.assertEqual(session['discovery']['stop_reason'], 'no_eligible_links')
        self.assertIn('Shared destination S4: reached from S2, S3', err)
        self.assertNotIn('one hop', err)

    def test_explicit_depth_and_same_origin_parameters_bound_exploration(self):
        """Keep stricter operator modes available without making them implicit defaults.

        Args:
            self: Fixture allowing cross-site root links but limiting the next depth.

        Returns:
            None. Assert depth-one mode stops after the two children and never fetches the join.
        """
        session, _ = self.run_pipeline('--max-link-depth', '1')
        self.assertEqual(len(session['sources']), 3)
        self.assertEqual(session['discovery']['stop_reason'], 'link_depth_limit')
        self.assertFalse(any(r[1] == '/shared' for r in self.base.http.requests))

    def test_recursive_route_can_exceed_old_fifty_graph_step_limit(self):
        """Prove the real graph can execute deeper exploration rather than hitting its old step cap.

        Args:
            self: Production fixture changed to a seven-hop chain terminating without new links.

        Returns:
            None. Assert eight retrieved sources, depth seven and more than fifty executed
            graph steps without disabling the request/source/network backstops. Disable
            progress pruning explicitly because this fixture repeats the same claim.
        """
        paths = ['/research', *[f'/level-{i}' for i in range(1, 8)]]
        for index, path in enumerate(paths):
            link = f'<a href="{paths[index + 1]}">Next</a>' if index + 1 < len(paths) else ''
            self.base.routes[('example.org', path)] = (200, {'Content-Type': 'text/html'},
                ('<main><p>' + SOURCE_TEXT + '</p>' + link + '</main>').encode())
        session, _ = self.run_pipeline('--max-unproductive-depth', '0')
        self.assertEqual(len(session['documents']), 8)
        self.assertEqual(session['sources'][-1]['depth'], 7)
        self.assertGreater(len(session['visited_nodes']), 50)

    def test_same_origin_opt_in_blocks_cross_site_branches(self):
        """Honor exact-origin confinement only when the operator selects it.

        Args:
            self: Fixture whose root links point exclusively to different public origins.

        Returns:
            None. Assert no new sources or selector calls and recorded scope exclusions.
        """
        session, _ = self.run_pipeline('--same-origin-links')
        self.assertEqual(len(session['sources']), 1)
        self.assertFalse(self.rounds)
        self.assertTrue(all(l['exclusion_reason'] == 'outside_seed_origins' for l in session['discovery']['links']))

    def test_blacklist_applies_to_selection_and_redirect_before_network(self):
        """A permitted URL cannot redirect through a denied domain to bypass operator rules.

        Args:
            self: Fixture with a blocked branch destination and an allowed alias redirecting there.

        Returns:
            None. Assert denied direct links never enter selection and redirect targets never
            receive robots/document requests; other permitted work remains recorded.
        """
        blacklist = self.base.cli.directory / 'deny.txt'
        blacklist.write_text('join.example\n')
        self.base.routes[('alpha.example', '/start')] = (302, {'Location': 'https://join.example/shared'}, b'')
        code, _, err = self.base.cli.run_cli(self.base.provider, '--extract', '--review-claims', '--follow-links',
            '--max-requests', '60', '--blacklist', str(blacklist), plan_only=False, source_dns=public_dns, details=False)
        self.assertEqual(code, 1, err)
        _, session = self.base.cli.saved_session()
        self.assertTrue(any(d.get('error_code') == 'blacklisted_domain' for d in session['documents']))
        self.assertFalse(any(r[0] == 'join.example' for r in self.base.http.requests))
        self.assertTrue(any(l['exclusion_reason'] == 'blacklisted_domain' for l in session['discovery']['links']))

    def test_catalogue_is_not_an_implicit_allow_only_boundary(self):
        """Separate useful defaults from restrictive whitelist-only access and deny precedence.

        Args:
            self: Test owner loading the tracked configuration without visiting any listed site.

        Returns:
            None. Assert open browsing default, provider defaults, precise subdomain matching
            and deny-wins behavior when a domain appears on both lists.
        """
        policy, catalogue = load_url_configuration(CONFIG_DIRECTORY / 'blacklist.txt', CONFIG_DIRECTORY / 'whitelist.json')
        self.assertIsNone(url_denial('https://independent.example/research', policy))
        self.assertEqual(url_denial('https://www.pornhub.com/', policy), 'blacklisted_domain')
        self.assertIsNone(url_denial('https://notpornhub.com/', policy))
        self.assertEqual(catalogue['default_search_provider'], 'duckduckgo')
        restrictive = URLPolicy(blocked_domains=['deny.example'], allowed_domains=['example'], whitelist_only=True)
        self.assertEqual(url_denial('https://deny.example/', restrictive), 'blacklisted_domain')
        self.assertIsNone(url_denial('https://allowed.example/', restrictive))
        self.assertEqual(url_denial('https://elsewhere.test/', restrictive), 'outside_whitelist')
