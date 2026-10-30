"""Verify real terminal entry point/graph/parser/SDK/store behavior with synthetic provider HTTP only.

These fixtures establish integration and evidence handling, not live-model recall.
The fixtures explicitly enable capture regardless of answer relevance and inspect
commits through a separate SQLite reader. Fixture ordering does not make capture
completion a prerequisite for answer extraction in every scheduling mode.
"""
import contextlib
import io
import json
import re
import sys
import unittest
from unittest.mock import patch

from projectclarity.nodes.accumulate_knowledge.node import capture_windows
from projectclarity.knowledge.graph_store import commit_observations, read_committed_additions, read_knowledge
from projectclarity.knowledge.source_value import source_potential
from tests.planning import test_planning_cli_integration as planning_tests


class KnowledgePipelineTests(unittest.TestCase):
    """Exercise explicitly enabled capture through the real production terminal entry point."""

    def setUp(self):
        """Create a held-out roster with no specialist claims to expose relevance filtering.

        Args:
            self: Test case owning the temporary terminal interface fixture and synthetic capture records.

        Returns:
            None. Register cleanup and prepare a local source plus task-shaped SDK replies.
        """
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.cli.plan['clarifications'] = []
        for step in self.cli.plan['strategy']:
            step['source_ids'] = ['S1']
        self.names = ['Mira Chen', 'Omar Diallo', 'Lina Duarte', 'Anika Rao']
        self.document = self.cli.directory / 'roster.txt'
        self.text = 'Cedar Institute\n' + ''.join(f'{name} is a visiting researcher at Cedar Institute.\n' for name in self.names)
        self.text += 'Cedar Institute is located in Halifax.\nLast updated August 2026\nLast updated August 2026\n'
        self.document.write_text(self.text)
        self.answer = {'entities': [{'entity_id': 'org', 'name': 'Cedar Institute', 'kind': 'organization', 'quote': 'Cedar Institute is located in Halifax.'}],
            'assertions': [], 'remaining': False}
        for index, name in enumerate(self.names):
            quote = f'{name} is a visiting researcher at Cedar Institute.'
            self.answer['entities'].append({'entity_id': f'P{index}', 'name': name, 'kind': 'person', 'quote': quote})
            self.answer['assertions'].append({'subject_id': f'P{index}', 'predicate': 'AFFILIATED_WITH', 'object_id': 'org',
                'value': None, 'role': 'visiting researcher', 'quote': quote})
        self.answer['assertions'].append({'subject_id': 'org', 'predicate': 'city', 'object_id': None,
            'value': 'Halifax', 'role': None, 'quote': 'Cedar Institute is located in Halifax.'})
        self.snapshots = []
        self.console_snapshots = []
        self.capture_payloads = []
        self.capture_override = None

    def provider(self, request):
        """Answer actual SDK requests while inspecting graph visibility at the next model task.

        Args:
            self: Fixture holding expected capture records and collected database/console
                snapshots taken before the next answer-extraction model response.
            request: Serialized HTTP request emitted by the real provider SDK.

        Returns:
            A synthetic HTTP response; unexpected schemas fail rather than using live services.
        """
        body = json.loads(request.content)
        title = body['output_config']['format']['schema']['title']
        if title == 'ResearchPlan':
            return self.cli.response()
        payload = json.loads(body['messages'][0]['content'])
        if title == 'KnowledgeBatch':
            self.capture_payloads.append(payload)
            self.assertNotIn('scope_in', payload)
            self.assertNotIn('scope_out', payload)
            self.assertNotIn('goals', payload)
            answer = self.capture_override(payload) if self.capture_override else self.answer
        elif title == 'ExtractionBatch':
            store = self.cli.output / 'knowledge.sqlite3'
            self.snapshots.append(read_knowledge(store) if store.exists() else {'counts': {'nodes': 0}, 'missing_store': True})
            self.console_snapshots.append(sys.stderr.getvalue())
            answer = {'entities': [], 'claims': []}
        else:
            raise AssertionError(title)
        return self.cli.response(text=json.dumps(answer))

    def run_capture(self, *options, expected=0):
        """Run the real terminal entry point with explicit graph-capture opt-in and no live credentials/network.

        Args:
            self: Fixture owning its local source, output root and fake HTTP responder.
            *options: Additional production terminal entry point flags, including explicit request limits.
            expected: Required terminal interface exit code, preventing saved partial artifacts from
                hiding a failed production invocation in persistence assertions.

        Returns:
            Exit code, newest saved-session dictionary, stdout and stderr; all artifacts
            remain temporary and unexpected exit codes fail the test.
        """
        code, stdout, stderr = self.cli.run_cli(self.provider, '--document', str(self.document), '--extract',
            *options, include_urls=False, plan_only=False, knowledge=True, details=False)
        self.assertEqual(code, expected, stderr)
        paths = list(self.cli.output.glob('*/session.json'))
        newest = max(paths, key=lambda p: p.stat().st_mtime_ns)
        self.last_directory = newest.parent
        return code, json.loads(newest.read_text()), stdout, stderr

    def test_default_pipeline_commits_people_roles_attributes_before_next_task(self):
        """Retain every fixture person and make graph growth visible before research finishes.

        Args:
            self: Test fixture with a roster unrelated to the answer's specialist scope.

        Returns:
            None. Assert real node execution, committed records, provenance and default budget.
        """
        code, session, _, err = self.run_capture()
        self.assertEqual(code, 0, err)
        self.assertEqual(session['request']['budget']['max_requests'], 100)
        self.assertEqual(session['request']['budget']['max_output_tokens'], 8192)
        self.assertIn('accumulate_knowledge', session['visited_nodes'])
        graph = self.snapshots[0]
        self.assertEqual(sorted(n['name'] for n in graph['nodes'] if n['kind'] == 'person'), sorted(self.names))
        self.assertEqual(graph['counts']['assertions'], 5)
        self.assertEqual(sum(a['role'] == 'visiting researcher' for a in graph['assertions']), 4)
        self.assertTrue(any(a['predicate'] == 'city' and a['value'] == 'Halifax' for a in graph['assertions']))
        self.assertTrue(all(a['answer_relevance'] == 'unassessed' for a in graph['assertions']))
        self.assertTrue(all(a['evidence']['quote'] in self.text for a in graph['assertions']))
        changes = read_knowledge(self.cli.output / 'knowledge.sqlite3', since=0, limit=2)
        self.assertEqual(len(changes['events']), 2)
        self.assertTrue(changes['has_more'])
        following = read_knowledge(self.cli.output / 'knowledge.sqlite3', since=changes['cursor'])
        self.assertTrue(all(e['seq'] > changes['cursor'] for e in following['events']))

    def test_console_shows_every_new_record_before_next_model_task(self):
        """Prove the normal terminal interface displays committed graph content during research, not just at exit.

        Args:
            self: Fixture exercising real graph, capture, monitor, console and SQLite paths.

        Returns:
            None. Assert every new node/edge/attribute is visible before answer extraction,
            roles and direction are readable, and private graph labels stay out of telemetry.
        """
        _, _, _, err = self.run_capture()
        during = self.console_snapshots[0]
        self.assertIn('S1: knowledge added — 5 nodes, 4 edges, 1 attribute', during)
        self.assertEqual(during.count('+ Node ['), 5)
        self.assertEqual(during.count('+ Edge:'), 4)
        self.assertEqual(during.count('+ Attribute:'), 1)
        for name in self.names:
            self.assertIn(f'+ Node [person]: {name}', during)
            self.assertIn(f'+ Edge: {name} — AFFILIATED_WITH → Cedar Institute [role: visiting researcher]', during)
        self.assertIn('+ Attribute: Cedar Institute — city = "Halifax"', during)
        self.assertIn('source-anchored, unreviewed', err)
        telemetry = (self.last_directory / 'events.jsonl').read_text()
        status = (self.last_directory / 'run_status.json').read_text()
        for private in [*self.names, 'Cedar Institute', 'Halifax', 'visiting researcher']:
            self.assertNotIn(private, telemetry)
            self.assertNotIn(private, status)
        event, = [json.loads(line) for line in telemetry.splitlines() if json.loads(line)['event'] == 'knowledge_committed']
        self.assertLess(event['details']['knowledge_cursor_start'], event['details']['knowledge_cursor_end'])

    def test_console_does_not_reannounce_replays_but_shows_new_assertions_on_existing_nodes(self):
        """Keep new-record announcements faithful when research revisits a known source.

        Args:
            self: Fixture preserving its SQLite database across three production terminal entry point runs.

        Returns:
            None. Assert replay has no new-record lines, while a later inserted assertion
            displays existing endpoint names without announcing those nodes again.
        """
        self.run_capture()
        _, _, _, repeated = self.run_capture()
        self.assertIn('no new nodes, edges or attributes', repeated)
        self.assertNotIn('+ Node [', repeated)
        self.assertNotIn('+ Edge:', repeated)
        self.assertNotIn('+ Attribute:', repeated)
        self.answer['assertions'][0]['predicate'] = 'WORKS_AT'
        _, _, _, changed = self.run_capture()
        self.assertIn('0 nodes, 1 edge, 0 attributes', changed)
        self.assertIn('+ Edge: Mira Chen — WORKS_AT → Cedar Institute [role: visiting researcher]', changed)
        self.assertNotIn('+ Node [', changed)
        self.assertEqual(changed.count('+ Edge:'), 1)

    def test_committed_additions_exclude_other_runs_and_later_batches(self):
        """Bound private console reads to the owning commit even when the store keeps growing.

        Args:
            self: Fixture supplying a temporary shared database for interleaved run writes.

        Returns:
            None. Assert the feed excludes other runs, later commits and repeat sightings,
            while resolving an edge whose subject node predates the selected batch.
        """
        database = self.cli.directory / 'isolated.sqlite3'
        source = {'location': 'fixture.txt', 'text_sha256': 'fixture', 'retrieved_at': 'fixture'}
        entity = {'entity_id': 'A', 'name': 'Alice', 'kind': 'person',
            'evidence': {'start': 0, 'end': 5, 'quote': 'Alice'}}
        first = commit_observations(database, 'run-a', source, [entity], [])
        other = {**entity, 'entity_id': 'B', 'name': 'Bob'}
        commit_observations(database, 'run-b', source, [other], [])
        edge = {'subject_id': 'A', 'object_id': 'B', 'predicate': 'KNOWS', 'role': None,
            'value': None, 'evidence': {'start': 0, 'end': 5, 'quote': 'Alice'}}
        batch = commit_observations(database, 'run-a', source, [entity, other], [edge])
        commit_observations(database, 'run-a', source, [{**entity, 'name': 'Later'}], [])
        additions = read_committed_additions(database, 'run-a', first['cursor'], batch['cursor'])
        self.assertEqual(additions['nodes'], [])
        self.assertEqual(len(additions['relationships']), 1)
        self.assertEqual(additions['relationships'][0]['subject_name'], 'Alice')
        self.assertEqual(additions['relationships'][0]['object_name'], 'Bob')
        self.assertEqual(additions['attributes'], [])

    def test_console_read_failure_does_not_fail_committed_research(self):
        """Keep a display-read failure separate from successful database capture.

        Args:
            self: Production terminal interface fixture with only the console's store reader made unavailable.

        Returns:
            None. Assert the terminal interface succeeds with saved graph records and an honest unavailable
            details message, rather than reporting zero additions or failing research.
        """
        with patch('projectclarity.terminal_run_progress.read_committed_additions', side_effect=OSError('unavailable')):
            _, _, _, err = self.run_capture()
        self.assertIn('knowledge saved; new-record details unavailable', err)
        self.assertEqual(self.snapshots[0]['counts']['nodes'], 5)
        self.assertNotIn('+ Node [', err)

    def test_dense_roster_continues_beyond_single_reply_capacity(self):
        """Exercise windows and continuation through the terminal interface so thirty entities is not a page cap.

        Args:
            self: Fixture supplying eighty synthetic names through the real saved-text path.

        Returns:
            None. Assert all eighty fixture people persist and are individually displayed,
            continuations carry prior records,
            and per-source request/token measurements agree with actual provider receipts.
            This checks orchestration, not live-model extraction recall.
        """
        names = [f'Researcher Person{index:03}' for index in range(80)]
        self.document.write_text('\n'.join(f'{name} is a faculty member.' for name in names))

        def capture_roster(payload):
            """Simulate bounded model replies from the actual window rather than a hidden roster.

            Args:
                payload: Serialized production capture input, including prior continuation data.

            Returns:
                At most thirty remaining source names with exact quotations and an explicit
                remaining flag; the application must request additional batches itself.
            """
            prior = {entity['name'] for batch in payload['already_captured'] for entity in batch['entities']}
            rows = re.findall(r'(Researcher Person\d{3}) is a faculty member\.', payload['spans'][0]['text'])
            remaining = [name for name in rows if name not in prior]
            return {'entities': [{'entity_id': name.replace(' ', '_'), 'name': name, 'kind': 'person',
                'quote': f'{name} is a faculty member.'} for name in remaining[:30]],
                'assertions': [], 'remaining': len(remaining) > 30}

        self.capture_override = capture_roster
        _, session, _, console = self.run_capture('--max-requests', '120')
        graph = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.assertEqual(sorted(node['name'] for node in graph['nodes']), names)
        self.assertEqual(console.count('+ Node [person]:'), 80)
        for name in names:
            self.assertIn('+ Node [person]: ' + name, console)
        self.assertTrue(any(payload['already_captured'] for payload in self.capture_payloads))
        self.assertTrue(all(w['status'] == 'captured' for w in session['knowledge']['windows']))
        profile = session['source_value']['S1']
        self.assertEqual(profile['capture_model_requests'], len(self.capture_payloads))
        calls = [c for c in session['processing_calls'] if c['task'] == 'accumulate_knowledge']
        self.assertEqual(profile['capture_tokens']['input_tokens'], sum(c['input_tokens'] for c in calls))

    def test_nonproductive_continuation_stops_with_partial_result(self):
        """Do not spend the whole run budget when a model repeats a batch without advancing.

        Args:
            self: Fixture whose model repeatedly claims more content remains.

        Returns:
            None. Assert two capture attempts, unchanged graph counts and an honest partial
            outcome instead of declaring complete capture or looping indefinitely.
        """
        self.answer['remaining'] = True
        _, session, _, _ = self.run_capture()
        self.assertEqual(len(self.capture_payloads), 2)
        self.assertEqual(session['knowledge']['objective_status'], 'incomplete')
        self.assertEqual(session['knowledge']['windows'][0]['error'], 'continuation_made_no_progress')
        self.assertEqual(read_knowledge(self.cli.output / 'knowledge.sqlite3')['counts']['nodes'], 5)

    def test_generic_network_and_cli_change_feed(self):
        """Persist non-roster relationships and inspect them using the public no-model command.

        Args:
            self: Fixture replacing its roster with a paper, researcher, event, funder and place.

        Returns:
            None. Assert generic node kinds, connected edges, numeric attributes and JSON
            change-feed access without a new provider request or credential lookup.
        """
        self.text = ('Elena Park authored Boundary Study.\nBoundary Study was presented at Safety Summit.\n'
            'Boundary Study was funded by Horizon Fund.\nSafety Summit took place in Halifax.\n'
            'Boundary Study was published in 2024.\n')
        self.document.write_text(self.text)
        definitions = [('author', 'Elena Park', 'person', 0), ('paper', 'Boundary Study', 'creation', 0),
            ('event', 'Safety Summit', 'event', 1), ('fund', 'Horizon Fund', 'organization', 2),
            ('city', 'Halifax', 'location', 3)]
        lines = self.text.splitlines()
        self.answer = {'entities': [{'entity_id': key, 'name': name, 'kind': kind, 'quote': lines[line]}
            for key, name, kind, line in definitions], 'assertions': [], 'remaining': False}
        for subject, predicate, target, line in [('author', 'AUTHORED', 'paper', 0),
            ('paper', 'PRESENTED_AT', 'event', 1), ('paper', 'FUNDED_BY', 'fund', 2),
            ('event', 'LOCATED_IN', 'city', 3)]:
            self.answer['assertions'].append({'subject_id': subject, 'predicate': predicate,
                'object_id': target, 'value': None, 'role': None, 'quote': lines[line]})
        self.answer['assertions'].append({'subject_id': 'paper', 'predicate': 'publication_year',
            'object_id': None, 'value': 2024, 'role': None, 'quote': lines[4]})
        self.run_capture('--max-output-tokens', '4096')
        graph = self.snapshots[0]
        self.assertEqual({n['kind'] for n in graph['nodes']}, {'person', 'creation', 'event', 'organization', 'location'})
        self.assertTrue(any(a['predicate'] == 'publication_year' and a['value'] == 2024 for a in graph['assertions']))
        ids = {n['id'] for n in graph['nodes']}
        self.assertTrue(all(a['subject'] in ids and (a['object'] is None or a['object'] in ids) for a in graph['assertions']))
        from projectclarity.command_line import main
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('projectclarity.nodes.plan_research.research_planner.read_api_key', side_effect=AssertionError('No model needed')):
            code = main(['knowledge', '--output-dir', str(self.cli.output), '--since', '0', '--limit', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output.getvalue())['events']), 2)

    def test_repeat_run_reuses_records_and_later_version_preserves_history(self):
        """Repeated observations do not duplicate nodes; changed source evidence stays versioned.

        Args:
            self: Fixture retaining the same database and source location across actual runs.

        Returns:
            None. Assert stable records, new sightings and append-only source/assertion history.
        """
        self.run_capture()
        first = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.run_capture()
        second = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.assertEqual(first['counts'], second['counts'])
        self.assertGreater(second['cursor'], first['cursor'])
        self.document.write_text(self.text.replace('Halifax', 'Ottawa'))
        self.answer['entities'][0]['quote'] = 'Cedar Institute is located in Ottawa.'
        self.answer['assertions'][-1].update(value='Ottawa', quote='Cedar Institute is located in Ottawa.')
        self.run_capture()
        third = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.assertEqual(third['counts']['nodes'], first['counts']['nodes'])
        self.assertEqual(third['counts']['sources'], 2)
        self.assertEqual({a['value'] for a in third['assertions'] if a['predicate'] == 'city'}, {'Halifax', 'Ottawa'})

    def test_ambiguous_date_does_not_erase_affiliations(self):
        """Reject the repeated date independently while retaining correctly anchored relationships.

        Args:
            self: Fixture with two identical update-date labels in one source window.

        Returns:
            None. Assert partial status, a rejected date and all four surviving affiliations;
            the console must not announce the rejected attribute as newly saved knowledge.
        """
        self.answer['assertions'].append({'subject_id': 'org', 'predicate': 'updated_at', 'object_id': None,
            'value': '2026-08', 'role': None, 'quote': 'Last updated August 2026'})
        code, session, _, console = self.run_capture()
        self.assertEqual(code, 0)
        self.assertEqual(session['knowledge']['objective_status'], 'incomplete')
        self.assertTrue(session['knowledge']['incomplete'])
        graph = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.assertEqual(sum(a['predicate'] == 'AFFILIATED_WITH' for a in graph['assertions']), 4)
        self.assertFalse(any(a['predicate'] == 'updated_at' for a in graph['assertions']))
        self.assertNotIn('+ Attribute: Cedar Institute — updated_at', console)
        self.assertEqual(console.count('+ Edge:'), 4)
        self.assertEqual(session['knowledge']['windows'][0]['rejected'][0]['reason'], 'ambiguous_quote')

    def test_budget_stop_exposes_pending_capture_windows(self):
        """A tight shared cap prioritizes research and retains unattempted auxiliary windows.

        Args:
            self: Fixture expanded into multiple windows with only one post-planning model call.

        Returns:
            None. Assert only two total calls, completed research and explicit unattempted capture.
        """
        self.document.write_text(self.text + ('Unexamined supporting material.\n' * 220))
        code, session, _, _ = self.run_capture('--max-requests', '2', expected=0)
        self.assertEqual(code, 0)
        self.assertEqual(session['usage']['requests_attempted'], 2)
        self.assertTrue(session['knowledge']['incomplete'])
        self.assertTrue(any(w['status'] == 'pending' for w in session['knowledge']['windows']))
        self.assertFalse((self.cli.output / 'knowledge.sqlite3').exists())
        self.assertEqual(session['extraction']['attempts'][0]['status'], 'completed')

    def test_same_name_different_sources_does_not_assert_identity(self):
        """Identical names from unrelated source locations remain separate unresolved nodes.

        Args:
            self: Fixture reusing labels but changing the source document's location.

        Returns:
            None. Assert no automatic homonym merge or unsupported same-as relationship.
        """
        self.run_capture()
        self.document = self.cli.directory / 'unrelated_roster.txt'
        self.document.write_text(self.text)
        self.run_capture()
        graph = read_knowledge(self.cli.output / 'knowledge.sqlite3')
        self.assertEqual(graph['counts']['nodes'], 10)
        self.assertTrue(all(n['identity_status'] == 'source_scoped_unresolved' for n in graph['nodes']))

    def test_changed_evidence_after_capture_reply_is_not_committed(self):
        """A stale model reply cannot attach graph observations to modified source evidence.

        Args:
            self: Fixture deliberately altering only its temporary saved text during response.

        Returns:
            None. Assert failed capture and no persistent graph records from altered evidence.
        """
        def mutate(payload):
            """Simulate evidence changing after the model read its input.

            Args:
                payload: Actual capture task containing source and window identifiers.

            Returns:
                The original proposed batch, now referencing stale source bytes.
            """
            path, = self.cli.output.glob('*/documents/S1/text.txt')
            path.write_text(path.read_text() + '\nAltered evidence.')
            return self.answer
        self.capture_override = mutate
        code, session, _, _ = self.run_capture(expected=1)
        self.assertEqual(code, 1)
        self.assertTrue(session['knowledge']['incomplete'])
        self.assertFalse((self.cli.output / 'knowledge.sqlite3').exists())

    def test_source_potential_distinguishes_directory_from_thin_page_without_truth_claim(self):
        """Structure signals can inform later scheduling without calling all short pages useless.

        Args:
            self: Test fixture supplying a real retrieved receipt and independent text shapes.

        Returns:
            None. Assert inspectable potential differences, not authority or correctness scores.
        """
        _, session, _, _ = self.run_capture()
        from projectclarity.research_schemas import RetrievedDocument
        document = RetrievedDocument.model_validate(session['documents'][0])
        rich = source_potential('\n'.join(f'Person {i}\nFaculty Member' for i in range(40)), document)
        thin = source_potential('Please enable JavaScript.', document)
        self.assertEqual(rich['potential'], 'high')
        self.assertEqual(thin['potential'], 'low')
        self.assertIn('not authority', rich['interpretation'])

    def test_windows_cover_all_text_and_overlap_split_records(self):
        """Window boundaries retain adjacent context without silently omitting source characters.

        Args:
            self: Test case using deterministic text longer than several capture windows.

        Returns:
            None. Assert complete character coverage and repeated boundary context.
        """
        text = 'A named researcher\nAn institutional affiliation\n' * 300
        windows = capture_windows(text)
        self.assertEqual(windows[0][0], 0)
        self.assertEqual(windows[-1][1], len(text))
        for prior, current in zip(windows, windows[1:]):
            self.assertLess(current[0], prior[1])
            self.assertGreater(current[1], prior[1])
