"""Check that the benchmark measures recorded results rather than inventing a score.

The tests run the experiment tool with controlled model replies and review answers.
They check previews, repeated trials, failed steps, source preservation and scoring,
including cases with nothing to score. This tests the evaluator itself; the invented
answers must not be reported as evidence of a real model's research accuracy.
"""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from testing import research_benchmark


class FakeProvider:
    """Deterministic structural replies; reads only the actual request, not the key."""

    def __init__(self, failure=None):
        """Set up isolated request tracking and an optional stage failure.

        Args:
            failure: plan, extract, select, drop_select, or None.
        Returns:
            None. Does not read the answer key or contact any provider.
        """
        self.failure = failure
        self.requests = []
        self.messages = self

    def create(self, **request):
        """Return schema-shaped data to exercise real CLI wiring offline.

        Args:
            request: Exact request supplied by an existing module.
        Returns:
            Fake provider response, optionally truncated. Captures request copies;
            source-line facts are test doubles, not semantically verified outputs.
        """
        self.requests.append(copy.deepcopy(request))
        properties = request['output_config']['format']['schema']['properties']
        if 'information_needed' in properties:
            stage = 'plan'
            payload = {'information_needed': [{'kind': 'attribute', 'name': 'Identity',
                                               'description': 'Identify the subjects relevant to the question.'}]}
        else:
            data = json.loads(request['messages'][0]['content'])
            if 'entities' in properties:
                stage = 'extract'
                lines = data['document_chunk'].splitlines()
                payload = {'entities': [{'id': 'source', 'name': 'Source', 'type': 'document', 'subtype': None}],
                           'facts': [{'subject_id': 'source', 'predicate': 'states', 'value': line,
                                      'object_id': None, 'statement': line, 'quote': line} for line in lines]}
            else:
                stage = 'select'
                facts = data['facts'][1:] if self.failure == 'drop_select' else data['facts']
                payload = {'selected_facts': [{'fact_id': f['fact_id'], 'reason': 'Offline wiring fixture.'} for f in facts]}
        text = json.dumps(payload)
        reason = 'max_tokens' if self.failure == stage else 'end_turn'
        return SimpleNamespace(content=[SimpleNamespace(type='text', text=text)], stop_reason=reason,
                               model_dump=lambda **kwargs: {'stop_reason': reason, 'model': 'offline-fake', 'text': text})


class BenchmarkTests(unittest.TestCase):
    """Verify full routing, no-key leakage, failure accounting and score denominators."""

    def run_fixture(self, root, failure=None, repeat=1):
        """Run the real orchestrator and CLI modules with only provider I/O replaced.

        Args:
            root: Temporary output parent.
            failure: Optional fake failure mode.
            repeat: Number of independent repetitions to exercise offline.
        Returns:
            Exit code, run folder, fake provider and captured stdout. Creates only
            temporary artifacts; fake credentials cannot access a real provider.
        """
        provider = FakeProvider(failure)
        output = io.StringIO()
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'offline-placeholder'}), \
             patch('testing.extraction_test_rig.Anthropic') as upstream, patch('projectclarity.evidence_selection.relevance_selection.Anthropic') as selector, \
             contextlib.redirect_stdout(output):
            upstream.return_value.__enter__.return_value = provider
            selector.return_value.__enter__.return_value = provider
            result = research_benchmark.main(['run', '--live', '--model', 'offline-fake', '--output-root', str(root),
                                     '--repeat', str(repeat), '--max-calls', str(39 * repeat)])
        return result, next(root.iterdir()), provider, output.getvalue()

    def completed_review(self, folder):
        """Supply controlled judgments to test arithmetic, not judge a live model.

        Args:
            folder: Fake-model run directory.
        Returns:
            Complete review matching the fixture's source-line records and key.
            Reads the key only after execution; never modifies any model request.
        """
        review = research_benchmark.read_json(folder / 'review.json')
        review['reviewer'] = 'OFFLINE TEST DOUBLE — NOT A SEMANTIC AUDIT'
        gold = research_benchmark.read_json(folder / 'packet/answer_key.json')['assertions']
        for trial, judged in zip(research_benchmark.read_json(folder / 'trials.json')['trials'], review['trials']):
            facts, _, _, _ = research_benchmark.trial_records(folder, trial)
            judged['requirements'] = {key: True for key in judged['requirements']}
            for fid, item in facts.items():
                assertion = next(a for a in gold if a['assertion'] == item['fact']['statement'])
                judged['facts'][fid] = {'meaning_correct': True, 'evidence_complete': True,
                                       'relevance': assertion['relevance'][trial['question_id']], 'notes': 'Test double'}
                judged['assertions'][assertion['id']]['supporting_fact_ids'] = [fid]
        return review

    def score_review(self, folder, review):
        """Write an editable test review and score it without touching evidence.

        Args:
            folder: Fake-model run directory.
            review: Controlled review object.
        Returns:
            New assessment summary; errors propagate for negative test assertions.
        """
        path = folder / 'test-review.json'
        path.write_text(json.dumps(review))
        with contextlib.redirect_stdout(io.StringIO()):
            return research_benchmark.assess(folder, path)

    def test_preview_and_budget_make_no_calls(self):
        """Prove default preview and over-budget rejection cannot instantiate clients.

        Args: None.
        Returns: None; asserts 39-call preview and 117-call repeat cap behavior.
        """
        with patch('testing.extraction_test_rig.Anthropic') as client, patch('projectclarity.evidence_selection.relevance_selection.Anthropic') as selector, \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(research_benchmark.main(['run']), 0)
            self.assertEqual(research_benchmark.main(['run', '--repeat', '3', '--max-calls', '117']), 0)
            with self.assertRaises(SystemExit):
                research_benchmark.main(['run', '--live', '--repeat', '3'])
            client.assert_not_called()
            selector.assert_not_called()

    def test_real_entry_points_handoff_and_no_key_leakage(self):
        """Exercise all 39 calls with exact handoff, chunk coverage and private evidence.

        Args: None.
        Returns: None; verifies complete routing while semantic scores stay pending.
        """
        with tempfile.TemporaryDirectory() as temp:
            code, folder, provider, _ = self.run_fixture(Path(temp))
            self.assertEqual(code, 0)
            self.assertEqual(len(provider.requests), 39)
            reports = list(folder.glob('assessment-*/scores.json'))
            summary = research_benchmark.read_json(reports[0])
            self.assertEqual(summary['attempted_calls'], 39)
            self.assertEqual(summary['semantic_status'], 'pending_review')
            self.assertTrue(all(r['scores'] is None for r in summary['trials']))
            self.assertTrue(all(r['record_integrity'] is True for r in summary['trials']))
            for request in provider.requests:
                wire = json.dumps(request)
                for forbidden in ('supporting_fact_ids', 'answer_key', 'C1A1', 'meaning_correct', 'criteria'):
                    self.assertNotIn(forbidden, wire)
            for trial in research_benchmark.read_json(folder / 'trials.json')['trials']:
                requirements = research_benchmark.read_json(folder / trial['rig'] / 'requirements.json')['information_needed']
                self.assertEqual(len(trial['chunks']), 6)
                for entry in trial['chunks']:
                    receipt = research_benchmark.read_json(folder / entry['extraction'])
                    payload = json.loads(receipt['request']['messages'][0]['content'])
                    self.assertEqual(payload['requested_information'], requirements)
                    selection = research_benchmark.read_json(folder / entry['selection'] / 'selection.json')
                    selected_input = json.loads(selection['request']['messages'][0]['content'])
                    self.assertEqual(selected_input['REQUESTED INFORMATION'], requirements)
                    self.assertEqual([f['fact'] for f in selected_input['facts']], receipt['parsed']['facts'])
            self.assertEqual((folder / 'review.json').stat().st_mode & 0o777, 0o600)

    def test_stage_failures_are_incomplete_not_empty_success(self):
        """Stop failed prerequisites and preserve failed selection outcomes.

        Args: None.
        Returns: None; checks call counts for plan, extraction and selection failures.
        """
        for failure, count in [('plan', 3), ('extract', 21), ('select', 39)]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                code, folder, provider, _ = self.run_fixture(Path(temp), failure)
                self.assertEqual(code, 1)
                self.assertEqual(len(provider.requests), count)
                summary = research_benchmark.read_json(next(folder.glob('assessment-*/scores.json')))
                self.assertEqual(summary['workflow_status'], 'incomplete')
                self.assertTrue(all(t['record_integrity'] is None for t in summary['trials']))
                self.assertTrue(all(t['scores'] is None for t in summary['trials']))

    def test_scoring_and_loss_attribution(self):
        """Distinguish perfect capture, over-selection, selector loss and invalid meaning.

        Args: None.
        Returns: None; arithmetic assertions use manually supplied test judgments.
        """
        with tempfile.TemporaryDirectory() as temp:
            _, folder, _, _ = self.run_fixture(Path(temp), 'drop_select')
            review = self.completed_review(folder)
            summary = self.score_review(folder, review)
            first = summary['trials'][0]
            self.assertEqual(first['scores']['extraction_coverage']['fraction'], 1)
            self.assertLess(first['scores']['end_to_end_recall']['fraction'], 1)
            self.assertLess(first['scores']['selection_precision']['fraction'], 1)
            self.assertIn('selection_missed', {r['status'] for r in first['assertion_ledger']})
            assertion = review['trials'][0]['assertions']['C1A2']
            fid = assertion['supporting_fact_ids'][0]
            review['trials'][0]['facts'][fid]['meaning_correct'] = False
            changed = self.score_review(folder, review)['trials'][0]
            row = next(r for r in changed['assertion_ledger'] if r['assertion_id'] == 'C1A2')
            self.assertEqual(row['status'], 'extraction_invalid')
            self.assertEqual(changed['scores']['extraction_coverage']['matched'], 23)
            self.assertEqual(len(list(folder.glob('assessment-*'))), 3)

    def test_frozen_evidence_and_cross_chunk_mapping_rejected(self):
        """Prevent changed receipts or unrelated supporting IDs from producing scores.

        Args: None.
        Returns: None; both tampering and wrong-chunk evidence raise ValueError.
        """
        with tempfile.TemporaryDirectory() as temp:
            _, folder, _, _ = self.run_fixture(Path(temp))
            review = self.completed_review(folder)
            review['trials'][0]['assertions']['C1A1']['supporting_fact_ids'] = \
                review['trials'][0]['assertions']['C2A1']['supporting_fact_ids']
            with self.assertRaisesRegex(ValueError, 'another chunk'):
                self.score_review(folder, review)
            path = folder / 'packet/01_companies.txt'
            path.write_text(path.read_text() + 'Changed.\n')
            with self.assertRaisesRegex(ValueError, 'Frozen evidence changed'):
                self.score_review(folder, review)

    def test_repetitions_are_independent_and_report_ranges(self):
        """Exercise three fresh passes and report only reviewed-complete score ranges.

        Args: None.
        Returns: None; asserts 117 fake calls, nine trials and three observations per question.
        """
        with tempfile.TemporaryDirectory() as temp:
            code, folder, provider, _ = self.run_fixture(Path(temp), repeat=3)
            self.assertEqual(code, 0)
            self.assertEqual(len(provider.requests), 117)
            review = self.completed_review(folder)
            summary = self.score_review(folder, review)
            self.assertEqual(len(summary['trials']), 9)
            for comparison in summary['repeat_comparison'].values():
                self.assertEqual(comparison['reviewed_complete_trials'], 3)
                self.assertEqual(comparison['metric_ranges']['end_to_end_recall'],
                                 {'observed_repetitions': 3, 'min': 1.0, 'max': 1.0})

    def test_null_denominator_is_not_perfect_accuracy(self):
        """Keep empty denominators visibly undefined.

        Args: None.
        Returns: None; no score or artifact mutations occur.
        """
        self.assertIsNone(research_benchmark.ratio(0, 0)['fraction'])
        self.assertEqual(research_benchmark.ratio(1, 2)['fraction'], 0.5)


if __name__ == '__main__':
    unittest.main()
