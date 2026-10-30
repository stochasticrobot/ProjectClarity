"""Test the knowledge-capture unit with fixed text and controlled provider HTTP replies.

These checks prove that the scorer rejects incorrect graphs, not that a live
model extracts the fixture correctly. The separately invoked evaluation uses the
same capture function without this HTTP substitution. No planner, search, answer
extraction or real network access is permitted in these offline unit tests.
"""
import copy
import io
import json
from unittest.mock import patch
import unittest

from evaluation.knowledge_graph_evaluation import CASE_DIRECTORY, main
from evaluation.knowledge_graph_scoring import score_experiment
from evaluation.graph_comparison_policy import ComparisonPolicy, comparison_key, read_policy
from tests.planning import test_planning_cli_integration as planning_tests


class KnowledgeGraphEvaluationTests(unittest.TestCase):
    """Protect actual graph comparisons and the separation between source and answer key."""

    def setUp(self):
        """Prepare independent handwritten replies and reuse the offline transport harness.

        Args:
            self: Test instance owning a temporary experiment root and mutable reply fixture.

        Returns:
            None. Register cleanup and construct model-shaped observations from known source
            sentences without loading the evaluator's expected_graph.json answer key.
        """
        self.cli = planning_tests.PlanningCLIIntegrationTests()
        self.cli.setUp()
        self.addCleanup(self.cli.doCleanups)
        self.source = (CASE_DIRECTORY / 'source.txt').read_text()
        lines = [line for line in self.source.splitlines() if line and not line.startswith('All people')]
        entities = [('alice', 'Alice Morgan', 'person', 0), ('ben', 'Ben Carter', 'person', 1),
            ('lab', 'Cedar Lab', 'organization', 2), ('fund', 'Harbor Fund', 'organization', 7),
            ('paper', 'Northstar Study', 'creation', 3), ('conference', 'Maple Conference', 'event', 6),
            ('city', 'Halifax', 'location', 2)]
        self.batch = {'entities': [{'entity_id': key, 'name': name, 'kind': kind, 'quote': lines[line]}
            for key, name, kind, line in entities], 'assertions': [], 'remaining': False}
        for subject, predicate, target, role, line in [
            ('alice', 'AFFILIATED_WITH', 'lab', 'researcher', 0),
            ('ben', 'AFFILIATED_WITH', 'lab', 'director', 1),
            ('lab', 'LOCATED_IN', 'city', None, 2),
            ('alice', 'AUTHORED', 'paper', None, 3), ('ben', 'AUTHORED', 'paper', None, 4),
            ('paper', 'PRESENTED_AT', 'conference', None, 6),
            ('paper', 'FUNDED_BY', 'fund', None, 7), ('conference', 'LOCATED_IN', 'city', None, 8)]:
            self.batch['assertions'].append({'subject_id': subject, 'predicate': predicate, 'object_id': target,
                'value': None, 'role': role, 'quote': lines[line]})
        self.batch['assertions'].extend([
            {'subject_id': 'paper', 'predicate': 'publication_year', 'object_id': None,
                'value': 2024, 'role': None, 'quote': lines[5]},
            {'subject_id': 'conference', 'predicate': 'start_date', 'object_id': None,
                'value': '2025-06-12', 'role': None, 'quote': lines[9]}])

    def provider(self, request):
        """Substitute external HTTP and reject any task other than the capture unit under test.

        Args:
            self: Fixture owning handwritten provider replies and source text.
            request: Real SDK HTTP request containing the model task's serialized input/schema.

        Returns:
            A provider-shaped capture reply. Any plan, answer-extraction task or answer-key
            leak fails the test instead of using a service or inventing supporting results.
        """
        body = json.loads(request.content)
        self.assertNotIn('PRIVATE_GRAPH_ANSWER_KEY_NOT_MODEL_INPUT', request.content.decode())
        self.assertNotIn('predicate_aliases', request.content.decode())
        self.assertNotIn('role_equivalences', request.content.decode())
        self.assertNotIn('research-network-variations-v2', request.content.decode())
        self.assertNotIn('expected_graph.json', request.content.decode())
        title = body['output_config']['format']['schema']['title']
        self.assertEqual(title, 'KnowledgeBatch', 'Only the capture unit may call a model')
        payload = json.loads(body['messages'][0]['content'])
        self.assertIn('Alice Morgan is a researcher at Cedar Lab.', payload['spans'][0]['text'])
        return self.cli.response(text=json.dumps(self.batch))

    def run_example(self, *options):
        """Invoke the focused unit runner using only the existing network-denying transport harness.

        Args:
            self: Fixture owning its independent provider response and temporary output root.
            *options: Additional evaluator terminal interface flags, such as a deliberately small request cap.

        Returns:
            Exit code, experiment directory, saved score and printed report output from one
            actual capture invocation. The test supplies saved text; only the production
            capture function writes graph records. Whole-workflow invocation is forbidden.
        """
        def entry(*args, **kwargs):
            """Replace only the harness module dispatch with the real evaluation entry point.

            Args:
                *args: Ignored runpy arguments; the evaluator command is fixed by this fixture.
                **kwargs: Ignored runpy keyword arguments accepted for harness compatibility.

            Returns:
                Never normally returns; SystemExit conveys the actual evaluation result.
            """
            raise SystemExit(main(['run', '--output-dir', str(self.cli.output), *options]))
        with patch('runpy.run_module', side_effect=entry), \
                patch('projectclarity.agentic_workflow.run_research', side_effect=AssertionError('No whole workflow')), \
                patch('projectclarity.nodes.plan_research.node.create_planner', side_effect=AssertionError('No planner')):
            code, output, error = self.cli.run_cli(self.provider)
        paths = list(self.cli.output.glob('research_network/*/score.json'))
        self.assertEqual(len(paths), 1, error)
        self.assertEqual(error.count('Unexpected network'), 0)
        return code, paths[0].parent, json.loads(paths[0].read_text()), output

    def test_correct_graph_and_free_rescore_with_no_answer_key_leak(self):
        """Require exact expected content and prove local re-scoring cannot call the model.

        Args:
            self: Fixture supplying all expected nodes, directed edges, roles and attributes.

        Returns:
            None. Assert a 7/8/2 match, readable report, one capture SDK request and identical
            free re-scoring through the public command with research invocation forbidden.
        """
        code, directory, score, output = self.run_example()
        self.assertEqual(code, 0, output)
        self.assertEqual(score['status'], 'PASS')
        self.assertEqual([score['categories'][key]['matched_count'] for key in ('nodes', 'relationships', 'attributes')], [7, 8, 2])
        self.assertEqual(len(self.cli.requests), 1)
        session_path, = directory.glob('runs/*/session.json')
        session = json.loads(session_path.read_text())
        self.assertIsNone(session['plan'])
        self.assertEqual(session['visited_nodes'], ['accumulate_knowledge'])
        self.assertEqual(session['usage']['total_tokens'], session['processing_calls'][0]['total_tokens'])
        self.assertGreater(session['usage']['total_tokens'], 0)
        self.assertEqual(score['execution_scope'], 'knowledge_capture_unit')
        self.assertIn('Alice Morgan — AFFILIATED_WITH → Cedar Lab [role: researcher]', (directory / 'report.md').read_text())
        with patch('evaluation.knowledge_graph_evaluation.capture_fixture', side_effect=AssertionError('No model call')), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['score', str(directory)]), 0)

    def test_reversed_edge_fails_despite_unchanged_counts(self):
        """Catch a reversed relation even when the graph still has eight edges and valid quotes.

        Args:
            self: Fixture reversing Alice's authorship while retaining the actual source quote.

        Returns:
            None. Assert a missing expected edge, an unexpected inverse and FAIL.
        """
        row = self.batch['assertions'][3]
        row['subject_id'], row['object_id'] = row['object_id'], row['subject_id']
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertEqual(score['status'], 'FAIL')
        self.assertEqual(score['categories']['relationships']['matched_count'], 7)
        self.assertIn('Northstar Study — AUTHORED → Alice Morgan', score['categories']['relationships']['unexpected'])

    def test_wrong_role_and_attribute_are_reported(self):
        """A valid name and quotation cannot conceal a wrong membership role or publication year.

        Args:
            self: Fixture mutating one role and one value without altering their source quotes.

        Returns:
            None. Assert each affected category loses its match and lists the unexpected value.
        """
        self.batch['assertions'][0]['role'] = 'director'
        self.batch['assertions'][8]['value'] = 2023
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertEqual(score['status'], 'FAIL')
        self.assertEqual(score['categories']['relationships']['matched_count'], 7)
        self.assertEqual(score['categories']['attributes']['matched_count'], 1)

    def test_extra_inferred_edge_prevents_pass(self):
        """Do not accept a complete expected graph if unsupported extra connections are added.

        Args:
            self: Fixture claiming Harbor Fund funds Cedar Lab using the paper funding sentence.

        Returns:
            None. Assert all eight expected edges still match but the extra edge causes FAIL.
        """
        extra = copy.deepcopy(self.batch['assertions'][6])
        extra['subject_id'] = 'lab'
        self.batch['assertions'].append(extra)
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertEqual(score['categories']['relationships']['matched_count'], 8)
        self.assertIn('Cedar Lab — FUNDED_BY → Harbor Fund', score['categories']['relationships']['unexpected'])

    def test_unrelated_real_quote_is_not_support(self):
        """Require the supporting sentence, not merely an exact quote somewhere in the document.

        Args:
            self: Fixture supplying the right authorship edge with a real but unrelated quote.

        Returns:
            None. Assert the matching edge is labelled unsupported and cannot yield PASS.
        """
        self.batch['assertions'][3]['quote'] = 'Cedar Lab is located in Halifax.'
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertTrue(any(row['status'] == 'unsupported' for row in score['categories']['relationships']['rows']))

    def test_missing_node_and_incident_edges_fail(self):
        """Check identities and kinds rather than merely the number of inserted node records.

        Args:
            self: Fixture omitting Ben and all statements requiring his endpoint.

        Returns:
            None. Assert missing person and missing relations are exposed in the saved score.
        """
        self.batch['entities'] = [e for e in self.batch['entities'] if e['entity_id'] != 'ben']
        self.batch['assertions'] = [a for a in self.batch['assertions'] if a['subject_id'] != 'ben']
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertEqual(score['categories']['nodes']['matched_count'], 6)
        self.assertEqual(score['categories']['relationships']['matched_count'], 6)

    def test_unfinished_capture_is_incomplete_even_with_correct_graph(self):
        """Do not report PASS when capture still declares omitted work after bounded continuation.

        Args:
            self: Fixture repeatedly returning the same batch while claiming more work remains.

        Returns:
            None. Assert correct graph counts remain visible alongside INCOMPLETE capture;
            no planner or answer extraction is introduced to manufacture a budget stop.
        """
        self.batch['remaining'] = True
        code, _, score, _ = self.run_example('--max-requests', '2')
        self.assertEqual(code, 1)
        self.assertEqual(score['status'], 'INCOMPLETE')
        self.assertEqual(score['categories']['relationships']['matched_count'], 8)

    def test_wrong_node_kind_fails_with_all_edges_present(self):
        """Node labels alone are insufficient when a paper is incorrectly classified as a company.

        Args:
            self: Fixture changing only the paper's node kind, leaving every relation intact.

        Returns:
            None. Assert the node category fails even though all expected edges still match.
        """
        next(e for e in self.batch['entities'] if e['entity_id'] == 'paper')['kind'] = 'organization'
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 1)
        self.assertEqual(score['categories']['nodes']['matched_count'], 6)
        self.assertEqual(score['categories']['relationships']['matched_count'], 8)
        self.assertIn('Northstar Study (organization)', score['categories']['nodes']['unexpected'])

    def test_published_predicate_alias_and_scalar_year_are_accepted(self):
        """Allow only documented harmless representation differences in otherwise correct records.

        Args:
            self: Fixture using WROTE instead of AUTHORED and a textual publication year.

        Returns:
            None. Assert the finite alias policy accepts these variants without a model judge.
        """
        self.batch['assertions'][3]['predicate'] = 'WROTE'
        self.batch['assertions'][8]['value'] = '2024'
        code, _, score, _ = self.run_example()
        self.assertEqual(code, 0)
        self.assertEqual(score['status'], 'PASS')

    def test_altered_saved_evidence_and_reference_are_rejected(self):
        """Re-scoring must not bless answers against changed evidence or a rewritten answer key.

        Args:
            self: Fixture altering temporary experiment copies after a valid production run.

        Returns:
            None. Assert saved-text hash validation and frozen-key validation reject each edit.
        """
        _, directory, _, _ = self.run_example()
        path, = directory.glob('runs/*/documents/S1/text.txt')
        original = path.read_bytes()
        path.write_bytes(original + b'\nChanged source.')
        with self.assertRaises(ValueError):
            score_experiment(directory)
        path.write_bytes(original)
        reference = directory / 'expected_graph.json'
        reference.write_text(reference.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Frozen source or answer key changed'):
            score_experiment(directory)

    def test_equivalent_roles_inverse_edges_and_dates_pass_without_rewriting_graph(self):
        """Accept declared meaning-preserving variants through the capture unit and real store.

        Args:
            self: Fixture varying authorship roles, predicate orientation and an event date.

        Returns:
            None. Assert all expected records match, the report exposes accepted variants,
            and raw inverse predicates and roles remain unchanged in the actual database.
        """
        self.batch['assertions'][0]['role'] = 'a researcher'
        self.batch['assertions'][3]['role'] = 'author'
        self.batch['assertions'][4]['role'] = 'co-author'
        row = self.batch['assertions'][3]
        row['subject_id'], row['object_id'] = row['object_id'], row['subject_id']
        row['predicate'] = 'WRITTEN_BY'
        self.batch['assertions'][9]['value'] = 'June 12, 2025'
        code, directory, score, _ = self.run_example()
        self.assertEqual(code, 0)
        self.assertEqual(score['comparison_policy_id'], 'research-network-variations-v2')
        self.assertEqual(score['categories']['relationships']['matched_count'], 8)
        self.assertEqual(score['categories']['attributes']['matched_count'], 2)
        self.assertIn('Accepted variant:', (directory / 'report.md').read_text())
        from projectclarity.knowledge.graph_store import read_knowledge
        graph = read_knowledge(directory / 'runs' / 'knowledge.sqlite3')
        self.assertTrue(any(a['predicate'] == 'WRITTEN_BY' and a['role'] == 'author' for a in graph['assertions']))

    def test_variations_do_not_erase_role_direction_or_value_errors(self):
        """Reject near-miss meanings so flexibility does not become an anything-goes comparison.

        Args:
            self: Test case comparing boundary examples under the same published policy.

        Returns:
            None. Assert omitted/wrong membership roles, editor roles, swapped authorship,
            unknown predicates, fractional years and ambiguous/wrong dates remain distinct.
        """
        policy = read_policy(CASE_DIRECTORY / 'comparison_policy.json')
        relationship = {'subject': 'Alice Morgan', 'predicate': 'AUTHORED', 'object': 'Northstar Study', 'role': None}
        key = comparison_key(relationship, 'relationships', policy)
        for changes in [{'role': 'editor'}, {'subject': 'Northstar Study', 'object': 'Alice Morgan'},
                {'predicate': 'AUTHORED_BY'}, {'predicate': 'EDITED'}, {'predicate': 'SIGNED'}]:
            with self.subTest(changes=changes):
                self.assertNotEqual(comparison_key({**relationship, **changes}, 'relationships', policy), key)
        membership = {'subject': 'Alice Morgan', 'predicate': 'AFFILIATED_WITH', 'object': 'Cedar Lab', 'role': 'researcher'}
        for role in (None, 'director', 'principal investigator'):
            self.assertNotEqual(comparison_key({**membership, 'role': role}, 'relationships', policy),
                comparison_key(membership, 'relationships', policy))
        attribute = {'subject': 'Maple Conference', 'predicate': 'start_date', 'value': '2025-06-12'}
        for value in ('06/12/2025', '2025-12-06', '2025-02-30', 'June 13, 2025'):
            self.assertNotEqual(comparison_key({**attribute, 'value': value}, 'attributes', policy),
                comparison_key(attribute, 'attributes', policy))
        year = {'subject': 'Northstar Study', 'predicate': 'publication_year', 'value': 2024}
        for value in (2023, 2024.5, True, 'unknown'):
            self.assertNotEqual(comparison_key({**year, 'value': value}, 'attributes', policy),
                comparison_key(year, 'attributes', policy))

    def test_declared_variations_share_one_model_independent_key(self):
        """Allow a finite range of equivalent outputs without a model-specific acceptance branch.

        Args:
            self: Test case exercising each supported role/date/numeric representation.

        Returns:
            None. Assert declared aliases, correctly inverted edges, date formats and exact
            integer-year representations normalize identically while raw inputs remain intact.
        """
        policy = read_policy(CASE_DIRECTORY / 'comparison_policy.json')
        base = {'subject': 'Alice Morgan', 'predicate': 'AUTHORED', 'object': 'Northstar Study', 'role': None}
        expected = comparison_key(base, 'relationships', policy)
        for role in (None, 'author', 'AUTHOR', 'co-author', 'coauthor'):
            for predicate in ('AUTHORED', 'WROTE', 'author of'):
                self.assertEqual(comparison_key({**base, 'predicate': predicate, 'role': role}, 'relationships', policy), expected)
        inverse = {**base, 'subject': base['object'], 'object': base['subject'], 'predicate': 'authored-by', 'role': 'author'}
        self.assertEqual(comparison_key(inverse, 'relationships', policy), expected)
        date = {'subject': 'Maple Conference', 'predicate': 'start_date', 'value': '2025-06-12'}
        for value in ('2025-06-12', 'June 12, 2025', '12 June 2025'):
            self.assertEqual(comparison_key({**date, 'value': value}, 'attributes', policy), comparison_key(date, 'attributes', policy))
        year = {'subject': 'Northstar Study', 'predicate': 'publication_year', 'value': 2024}
        for value in (2024, '2024', 2024.0, '2024.0'):
            self.assertEqual(comparison_key({**year, 'value': value}, 'attributes', policy), comparison_key(year, 'attributes', policy))

    def test_old_policy_score_is_preserved_when_new_policy_is_explicitly_applied(self):
        """Reassess identical outputs under a new policy without rewriting the historical result.

        Args:
            self: Fixture constructing a pre-policy experiment with otherwise correct author roles.

        Returns:
            None. Assert legacy scoring remains FAIL, explicit new-policy scoring passes in
            a separate folder, and all original evidence, references and reports stay unchanged.
        """
        self.batch['assertions'][3]['role'] = 'author'
        self.batch['assertions'][4]['role'] = 'author'
        _, directory, _, _ = self.run_example()
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        del manifest['comparison_policy_sha256']
        manifest_path.write_text(json.dumps(manifest))
        with patch('evaluation.knowledge_graph_evaluation.capture_fixture', side_effect=AssertionError('No capture')), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['score', str(directory)]), 1)
            protected = ['manifest.json', 'source.txt', 'expected_graph.json', 'score.json', 'report.md', 'runs/knowledge.sqlite3']
            before = {name: (directory / name).read_bytes() for name in protected}
            self.assertEqual(main(['score', str(directory), '--policy', str(CASE_DIRECTORY / 'comparison_policy.json')]), 0)
        self.assertEqual(before, {name: (directory / name).read_bytes() for name in protected})
        path, = directory.glob('policy_rescores/*/score.json')
        result = json.loads(path.read_text())
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['comparison_policy_origin'], 'explicit_rescore')
        self.assertEqual(score_experiment(directory)['status'], 'FAIL')

    def test_conflicting_or_tampered_policy_is_rejected(self):
        """Prevent ambiguous equivalences or changed frozen policies from silently altering scores.

        Args:
            self: Fixture corrupting policy definitions and a temporary experiment's frozen copy.

        Returns:
            None. Assert conflicting aliases/roles and hash mismatches raise explicit input errors.
        """
        raw = json.loads((CASE_DIRECTORY / 'comparison_policy.json').read_text())
        bad = copy.deepcopy(raw)
        bad['predicate_aliases']['FUNDED_BY'].append('WROTE')
        with self.assertRaisesRegex(ValueError, 'Ambiguous predicate alias'):
            ComparisonPolicy.model_validate(bad)
        bad = copy.deepcopy(raw)
        bad['role_equivalences'].append({'predicate': 'AUTHORED', 'canonical_role': 'editor', 'accepted_roles': ['author', 'editor']})
        with self.assertRaisesRegex(ValueError, 'Ambiguous role equivalence'):
            ComparisonPolicy.model_validate(bad)
        _, directory, _, _ = self.run_example()
        path = directory / 'comparison_policy.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'Frozen comparison policy changed'):
            score_experiment(directory)

    def test_show_is_offline_and_documents_match_reference(self):
        """Keep the reviewable fixture and its independent answer key internally consistent.

        Args:
            self: Fixture using tracked source/reference files, not an invented live result.

        Returns:
            None. Assert inspection cannot start research, every reference quote occurs once
            in the document and its published 7/8/2 counts and endpoints are valid.
        """
        with patch('evaluation.knowledge_graph_evaluation.capture_fixture', side_effect=AssertionError('No capture')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['show']), 0)
        self.assertIn('Expected relationships: 8', output.getvalue())
        reference = json.loads((CASE_DIRECTORY / 'expected_graph.json').read_text())
        self.assertEqual([len(reference[k]) for k in ('nodes', 'relationships', 'attributes')], [7, 8, 2])
        names = {node['name'] for node in reference['nodes']}
        for row in reference['relationships'] + reference['attributes']:
            self.assertEqual(self.source.count(row['quote']), 1)
            self.assertIn(row['subject'], names)
            if 'object' in row:
                self.assertIn(row['object'], names)
