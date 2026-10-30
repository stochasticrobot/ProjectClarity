"""Check the isolated page-extraction command and its person–riding scorer.

Controlled model replies exercise production extraction, passage refinement and
resumption without planning or search. Scoring checks reject wrong associations
even when names and quotations look plausible. These are offline mechanics tests,
not measurements of a live model's extraction accuracy.
"""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage

from evaluation.page_extraction_benchmark import prepare, execute
from evaluation.page_extraction_scoring import person_name, score_page


class PageExtractionBenchmarkTests(unittest.TestCase):
    """Use synthetic provider output only to test wiring and comparator failures."""

    def test_only_extraction_nodes_execute_and_wrong_pairs_fail(self):
        """Keep planning/search out and reject a wrong pair despite an authentic quote.

        Args:
            self: Test instance with isolated temporary source and reference files.

        Returns:
            None; fails on unexpected node calls, missing persistence or false scoring.
            The synthetic reply does not demonstrate live-model accuracy.
        """
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.html'
            source.write_text('<main><table><tr><th>Name</th><th>Riding</th><th>Province</th></tr>'
                              '<tr><td>Aster</td><td>North</td><td>West</td></tr></table></main>')
            session = prepare(source, 'List the members and their ridings.', root / 'runs', 5)
            batch = {'entities': [{'entity_id': 'A', 'name': 'Aster', 'entity_type': 'person'},
                                 {'entity_id': 'N', 'name': 'North', 'entity_type': 'riding'}],
                     'claims': [{'claim_id': 'C', 'statement': 'Aster represents North.',
                                 'goal_ids': ['G1'], 'entity_ids': ['A', 'N'],
                                 'relationship': {'subject_id': 'A', 'predicate': 'represents', 'object_id': 'N'},
                                 'qualifications': [], 'evidence': [{'passage_id': 'P2',
                                     'quote': 'Name: Aster | Riding: North'}]}]}

            def invoke(messages):
                """Return the fixture only for its record-bearing production passage.

                Args:
                    messages: Actual production extraction messages, inspected for passage ID.

                Returns:
                    Synthetic provider-shaped raw/parsed response with no network access.
                """
                from projectclarity.extraction.extraction_contracts import ExtractionBatch
                payload = json.loads(messages[1][1])
                selected = batch if 'Name: Aster' in payload['passage']['text'] else {'entities': [], 'claims': []}
                if selected is batch:
                    batch['claims'][0]['evidence'][0]['passage_id'] = payload['passage']['passage_id']
                raw = AIMessage(content=json.dumps(selected), response_metadata={'stop_reason': 'end_turn'},
                                usage_metadata={'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30})
                return {'raw': raw, 'parsed': ExtractionBatch.model_validate(selected), 'parsing_error': None}

            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-not-real'}), patch(
                    'projectclarity.nodes.extract_knowledge.node.create_extractor',
                    return_value=SimpleNamespace(invoke=invoke)):
                session = execute(session, root / 'runs')
            self.assertEqual(session.visited_nodes, ['extract_knowledge', 'validate_evidence', 'persist_candidates'])
            directory = root / 'runs' / session.session_id
            reference = root / 'expected.json'
            reference.write_text(json.dumps({'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                'expected_pairs': [{'mp': 'Aster', 'riding': 'North', 'province': 'West'}], 'excluded_historical_pairs': []}))
            score = score_page(directory, reference)
            self.assertEqual(score['anchored_pairs']['f1'], 1)
            saved = json.loads((directory / 'extraction.json').read_text())
            for attempt in saved['attempts']:
                if attempt.get('result', {}).get('entities'):
                    attempt['result']['entities'][1]['name'] = 'Wrong region'
            (directory / 'extraction.json').write_text(json.dumps(saved))
            score = score_page(directory, reference)
            self.assertEqual(score['anchored_pairs']['correct_count'], 0)
            self.assertEqual(score['anchored_pairs']['unexpected'], [('aster', 'wrong region')])
            for label, expected_correct in [('North (West)', 1), ('North (East)', 0)]:
                for attempt in saved['attempts']:
                    if attempt.get('result', {}).get('entities'):
                        attempt['result']['entities'][1]['name'] = label
                (directory / 'extraction.json').write_text(json.dumps(saved))
                self.assertEqual(score_page(directory, reference)['anchored_pairs']['correct_count'], 0)
                adjusted = score_page(directory, reference, source_equivalences=True)
                self.assertEqual(adjusted['anchored_pairs']['correct_count'], expected_correct)

    def test_name_policy_does_not_merge_nicknames_or_strip_accents(self):
        """Permit display-order/title changes but retain identity distinctions.

        Args:
            self: Test instance supplying assertions.

        Returns:
            None; fails if the documented comparator becomes fuzzy.
        """
        self.assertEqual(person_name('Example, Hon. Éva'), person_name('Éva Example'))
        self.assertNotEqual(person_name('Éva Example'), person_name('Eva Example'))
        self.assertNotEqual(person_name('Robert Example'), person_name('Rob Example'))

    def test_refinement_resumes_without_repeating_parent_or_exceeding_quantum(self):
        """Persist bounded follow-ups and resume only their unattempted records.

        Args:
            self: Test instance with an isolated page and synthetic model responses.

        Returns:
            None; fails on replayed parent calls, lost child work or extra model attempts.
        """
        from projectclarity.extraction.extraction_contracts import ExtractionBatch
        from projectclarity.research_schemas import ResearchSession
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'rows.html'
            source.write_text('<table><tr><th>Name</th><th>Place</th></tr>'
                              '<tr><td>Aster</td><td>North</td></tr>'
                              '<tr><td>Birch</td><td>South</td></tr></table>')
            session = prepare(source, 'List people and places.', root / 'runs', 6)
            calls = []

            def invoke(messages):
                """Return prose-only multi-record claims and structured single-record claims.

                Args:
                    messages: Actual source passage and task messages from the production node.

                Returns:
                    Synthetic provider response for scheduling verification, not model accuracy.
                """
                payload = json.loads(messages[1][1])['passage']
                calls.append(payload['passage_id'])
                selected = [('Aster', 'North'), ('Birch', 'South')]
                selected = [(name, place) for name, place in selected if f'Name: {name}' in payload['text']]
                entities, claims = [], []
                for index, (name, place) in enumerate(selected):
                    person, location = f'E{index}a', f'E{index}b'
                    entities.extend([{'entity_id': person, 'name': name, 'entity_type': 'person'},
                                     {'entity_id': location, 'name': place, 'entity_type': 'place'}])
                    claims.append({'claim_id': f'C{index}', 'statement': f'{name} represents {place}.',
                        'goal_ids': ['G1'], 'entity_ids': [person, location], 'qualifications': [],
                        'relationship': {'subject_id': person, 'predicate': 'represents', 'object_id': location} if len(selected) == 1 else None,
                        'evidence': [{'passage_id': payload['passage_id'], 'quote': f'Name: {name} | Place: {place}'}]})
                batch = {'entities': entities, 'claims': claims}
                return {'raw': AIMessage(content=json.dumps(batch), response_metadata={'stop_reason': 'end_turn'},
                            usage_metadata={'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30}),
                        'parsed': ExtractionBatch.model_validate(batch), 'parsing_error': None}

            with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fixture-not-real'}), patch(
                    'projectclarity.nodes.extract_knowledge.node.create_extractor',
                    return_value=SimpleNamespace(invoke=invoke)):
                session = execute(session, root / 'runs', max_batches=2)
                self.assertEqual(session.usage.requests_attempted, 2)
                self.assertEqual(sum(a['status'] == 'not_attempted' for a in session.extraction['attempts']), 2)
                session = ResearchSession.model_validate_json((root / 'runs' / session.session_id / 'session.json').read_text())
                session = execute(session, root / 'runs', max_batches=1)
                self.assertEqual(session.usage.requests_attempted, 3)
                session = execute(session, root / 'runs')
            self.assertEqual(len(calls), 4)
            self.assertEqual(len(set(calls)), 4)
            self.assertEqual(len(session.extraction['attempts']), 4)
            self.assertTrue(all(a['status'] == 'completed' for a in session.extraction['attempts']))
