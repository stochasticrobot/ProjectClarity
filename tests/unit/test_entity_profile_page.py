"""Browser-check actual report scripts with synthetic API replies and no research calls.

Set PROJECTCLARITY_TEST_BROWSER to an installed Chromium executable to opt in.
Without it this development-browser test is skipped explicitly, not counted as a
rendering proof. The application HTML/CSS/JS are real; API responses are fixtures.
"""
import json
import os
from pathlib import Path
import unittest


@unittest.skipUnless(os.environ.get('PROJECTCLARITY_TEST_BROWSER'), 'Set PROJECTCLARITY_TEST_BROWSER for Chromium rendering checks')
class EntityProfilePageTests(unittest.TestCase):
    """Verify profile sections, literal rendering and navigation to original evidence."""
    def test_actual_page_renders_profile_unknowns_and_source_links(self):
        """Load the delivered page scripts at desktop and narrow widths.

        Args: None.
        Returns: None; verifies no directory controls, retained unknown/conflicting
            sections, support-category labels, escaped text and working source links. Intercepts every network
            request; no application mutations, provider calls or browser panel opens.
        """
        from playwright.sync_api import sync_playwright, expect
        root = Path(__file__).resolve().parents[2] / 'src/projectclarity/ux'
        report = {'presentation': 'entity_profile', 'research_question': 'Profile Alex Lee',
            'report_plan': {'report_type': 'entity_profile', 'reason': 'One named subject.'},
            'profile': {'subject': 'Alex Lee <script>bad()</script>', 'subject_kind': 'person', 'subject_scope': 'Toronto physicist',
                'sections': [{'requirement_id': 'R1', 'title': 'Research', 'status': 'conflicting',
                    'findings': [{'text': 'Conflicting descriptions', 'status': 'conflicting', 'fact_ids': ['F1']}]},
                    {'requirement_id': 'R2', 'title': 'Education', 'status': 'unknown', 'findings': []}],
                'identity_uncertainty': [{'fact_id': 'F2', 'status': 'ambiguous', 'reason': 'Namesake affiliation unresolved.', 'failure_categories': ['ambiguous_name']}]},
            'summary': [{'text': 'Source describes research.', 'status': 'supported', 'fact_ids': ['F1']}],
            'entries': [], 'requirements': [{'id': 'R1', 'name': 'Research'}, {'id': 'R2', 'name': 'Education'}],
            'gaps': ['Education not established.'], 'exclusions': [{'fact_id': 'F2', 'reason': 'Identity ambiguous.'}],
            'coverage': {'run': 'completed', 'selection': 'completed', 'triage': 'completed', 'investigation': 'completed',
                         'unreviewed_triage': 0, 'unreviewed_investigation': 0},
            'facts': [{'id': f'F{i}', 'fact': {'statement': 'Original source statement.'},
                       'evidence': [{'source_url': 'https://example.org/source', 'quote': 'Exact source excerpt.', 'pages': []}]} for i in (1, 2)]}
        state = {'session_id': 'fixture', 'status': 'report_completed', 'current_round': 1, 'research_rounds': 1,
                 'research_question': 'Profile Alex Lee', 'report': {'status': 'completed', 'report_type': 'entity_profile',
                     'started_at': 'fixture', 'facts_total': 2, 'facts_accounted': 2, 'sections_total': 2,
                     'supported_sections': 0, 'unknown_sections': 1}}
        errors = []
        mutations = []

        def serve(route):
            """Serve repository assets and controlled read-only API results.

            Args: route: Playwright request intercepted before any network access.
            Returns: None; fulfills fixtures, aborting unknown requests and mutations.
            """
            path = route.request.url.split('http://clarity.test', 1)[-1]
            if route.request.method != 'GET':
                mutations.append(path); route.abort(); return
            if path == '/report/fixture':
                route.fulfill(path=str(root / 'pages/research_report.html'), content_type='text/html')
            elif path.startswith('/assets/'):
                name = path.rsplit('/', 1)[-1]
                route.fulfill(path=str(root / ('styles' if name.endswith('.css') else 'scripts') / name),
                              content_type='text/css' if name.endswith('.css') else 'application/javascript')
            elif path == '/api/setup/fixture':
                route.fulfill(json=state)
            elif path == '/api/setup/fixture/report-data':
                route.fulfill(json=report)
            elif path == '/api/setup/fixture/usage':
                route.fulfill(json={'totals': {}, 'stages': {}, 'calls': []})
            else:
                route.fulfill(status=404, json={'detail': 'Fixture has no such resource.'})

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ['PROJECTCLARITY_TEST_BROWSER'], headless=True)
            try:
                page = browser.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('**/*', serve)
                page.goto('http://clarity.test/report/fixture')
                expect(page.locator('#filters')).to_be_hidden()
                expect(page.locator('#entries')).to_contain_text('Alex Lee <script>bad()</script>')
                expect(page.locator('#entries')).to_contain_text('Evidence status: unknown')
                expect(page.locator('#entries')).to_contain_text('Evidence status: conflicting')
                expect(page.locator('#entries')).to_contain_text('Namesake affiliation unresolved.')
                expect(page.locator('#entries')).to_contain_text('Support issues: ambiguous name')
                page.locator('#entries a[href="#source-F1"]').first.click()
                expect(page.locator('#source-details')).to_have_attribute('open', '')
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(page.locator('#entries')).to_contain_text('Education')
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                self.assertEqual(errors, []); self.assertEqual(mutations, [])
            finally:
                browser.close()
