"""Offline input and graph invariants; no live or simulated semantic judgments."""
from copy import deepcopy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from projectclarity.coverage import bounded_tree as tree
from projectclarity.coverage.evidence_batches import fingerprint
from projectclarity.discovery.inventory_context import payload_messages
from projectclarity.prompt_context import record_task_context
from projectclarity.task_inputs import input_receipt


from projectclarity.coverage.bounded_contracts import BoundedDigest as FixtureDigest


def candidates(count):
    """Create distinct neutral transport records, without inferred identity matches.

    Args:
        count: Number of synthetic records.
    Returns:
        Ordered ID-to-record mapping for the pure graph planner.
    """
    return {f'C{i}': NS(candidate_id=f'C{i}', source_id='S1',
        proposal=NS(statement=f'Observation {i}', goal_ids=['G1'], research_roles=[], qualifications=[]),
        adjudication_status='supported', evidence=[NS(quote=f'Quoted observation {i}')]) for i in range(count)}


def complete(node):
    """Supply deterministic accounting-only output for a planner node.

    Args:
        node: Descriptor whose immediate inputs must be acknowledged.
    Returns:
        Completed receipt with intact hash and inherited blockers; no truth claim.
    """
    answer = dict(covered_input_ids=node['input_ids'], summary='Synthetic transport receipt',
        witness_candidate_ids=node['witness_pool'][:1], limitations=[], **node['required_flags'])
    return dict(batch_id=node['batch_id'], status='completed', error_code=None,
        assessment=answer, output_sha256=fingerprint(answer))


class BoundedInputTests(unittest.TestCase):
    """Exercise explicit bounds, omission failures and immutable task projections."""

    def test_projection_preserves_user_constraints_without_mutation(self):
        """Keep human scope and record definitions while omitting global obligations.

        Args:
            self: Test owner.
        Returns:
            None; saved schema and guidance remain unchanged.
        """
        schema = dict(scope='Only current members', completion_checks=['Global completeness'],
            sections=[dict(fields=[{'name': 'riding'}], identity_fields=['name'],
                evidence_requirements=['Global qualification'])])
        before = deepcopy(schema)
        session = NS(review_decision={'answers': [{'question_id': 'Q1', 'answer': 'Both chambers'}]},
            plan=NS(clarifications=['Which chambers?'], assumptions=['Current means today']),
            created_at='2026-10-30', answer_schema=schema, coverage_study={'status': 'complete'})
        result = record_task_context(session)
        self.assertNotIn('early_coverage_study', result)
        self.assertNotIn('completion_checks', result['answer_schema'])
        self.assertNotIn('evidence_requirements', result['answer_schema']['sections'][0])
        self.assertEqual(result['human_guidance'], [{'question': 'Which chambers?', 'answer': 'Both chambers'}])
        self.assertEqual(result['answer_schema']['sections'][0]['identity_fields'], ['name'])
        self.assertEqual(schema, before)

    def test_preflight_rejects_inventory_unknown_fields_and_schema_overhead(self):
        """Detect forbidden global records and count the response schema in budgets.

        Args:
            self: Test owner.
        Returns:
            None; payloads are rejected without altering or truncating their content.
        """
        for payload, error in [({'result_inventory': {'G1': {'items': []}}}, 'input_contract_full_inventory_forbidden'),
                ({'result_inventory': {'G1': None}}, 'input_contract_invalid_inventory'),
                ({'result_inventory': []}, 'input_contract_invalid_inventory'),
                ({'early_coverage_study': {}}, 'input_contract_unknown_fields'),
                ({'original_question': 'x' * 64_000}, 'input_contract_too_large')]:
            messages = payload_messages('Unchanged instructions', payload)
            before = deepcopy(messages)
            receipt = input_receipt('bounded_coverage', messages, FixtureDigest)
            self.assertIn(error, receipt['errors'])
            self.assertEqual(messages, before)
            self.assertEqual(receipt['total_characters'], len(json.dumps(messages, ensure_ascii=False)) +
                len(json.dumps(FixtureDigest.model_json_schema(), ensure_ascii=False)))

    @patch.object(tree, 'source_context', return_value={'title': 'Synthetic source'})
    def test_scaling_has_complete_leaves_and_fixed_fan_in(self, source):
        """Account for 5,000 records without forwarding their full registry upward.

        Args:
            self: Test owner.
            source: Isolated source-context fixture; no source acquisition occurs.
        Returns:
            None; all leaves are covered once and each reduction remains bounded.
        """
        valid, receipts = candidates(5000), {}
        for _ in range(12):
            graph = tree.plan_tree(None, valid, {}, 'Test transport only', FixtureDigest, receipts)
            if graph['root']:
                break
            for node in graph['pending']:
                receipts[node['batch_id']] = complete(node)
        self.assertIsNotNone(graph['root'])
        leaves = [n for n in graph['nodes'] if n['stage'] == 'evidence']
        ids = [cid for n in leaves for cid in n['candidate_ids']]
        self.assertEqual(ids, list(valid))
        for leaf in leaves:
            self.assertEqual(leaf['payload']['sources'], {'S1': {'title': 'Synthetic source'}})
            self.assertTrue(all('source_context' not in row for row in leaf['payload']['inputs']))
        for node in graph['nodes']:
            self.assertLessEqual(len(node['input_ids']), 32 if node['stage'] == 'evidence' else 4)
            self.assertFalse(node['input_contract']['errors'])
            if node['stage'] == 'reduction':
                self.assertTrue(all('covered_input_ids' not in row for row in node['payload']['inputs']))

    @patch.object(tree, 'source_context', return_value={})
    def test_stale_tampered_and_incomplete_receipts_are_not_reused(self, source):
        """Invalidate cache entries when evidence, accounting or output hashes change.

        Args:
            self: Test owner.
            source: Local source fixture.
        Returns:
            None; stale or corrupted results cannot advance the graph.
        """
        valid = candidates(2)
        node = tree.plan_leaves(None, valid, {}, 'Test', FixtureDigest)[0]
        receipt = complete(node)
        self.assertTrue(tree.reusable(node, receipt, FixtureDigest))
        altered = deepcopy(receipt)
        altered['assessment']['summary'] = 'Tampered'
        self.assertFalse(tree.reusable(node, altered, FixtureDigest))
        altered = deepcopy(receipt)
        altered['assessment']['covered_input_ids'].pop()
        altered['output_sha256'] = fingerprint(altered['assessment'])
        self.assertFalse(tree.reusable(node, altered, FixtureDigest))
        valid['C1'].evidence[0].quote = 'Changed evidence'
        changed = tree.plan_leaves(None, valid, {}, 'Test', FixtureDigest)[0]
        self.assertFalse(tree.reusable(changed, receipt, FixtureDigest))

    def test_reducer_cannot_clear_child_blocker(self):
        """Keep uncertainty sticky even when a parent response claims resolution.

        Args:
            self: Test owner.
        Returns:
            None; mechanically cleared conflicts fail validation.
        """
        child = dict(input_id='BT_child', witness_candidate_ids=['C1'], unresolved_conflict=True,
            unresolved_identity=False, requires_detail=False, summary='Conflict')
        node = tree.descriptor('reduction', {}, [child], 'Test', FixtureDigest)
        answer = complete(node)['assessment']
        answer['unresolved_conflict'] = False
        with self.assertRaisesRegex(ValueError, 'bounded_cleared_child_blocker'):
            tree.validate_summary(node, answer, FixtureDigest)

    @patch.object(tree, 'source_context', return_value={})
    def test_indivisible_oversize_fails_without_clipping(self, source):
        """Reject a single oversized quote before creating any model requests.

        Args:
            self: Test owner.
            source: Local source fixture.
        Returns:
            None; the original oversized evidence remains intact.
        """
        valid = candidates(1)
        valid['C0'].evidence[0].quote = 'q' * 128_000
        with self.assertRaisesRegex(ValueError, 'bounded_item_too_large:C0'):
            tree.plan_leaves(None, valid, {}, 'Test', FixtureDigest)
        self.assertEqual(len(valid['C0'].evidence[0].quote), 128_000)

    def test_intermediate_output_is_bounded_not_just_its_input(self):
        """Reject output growth before it can become an unbounded parent request.

        Args:
            self: Test owner exercising the production response schema.
        Returns:
            None; oversized summaries, references and limitations cannot validate.
        """
        base = dict(covered_input_ids=['C1'], summary='Evidence', witness_candidate_ids=['C1'],
            limitations=[], unresolved_conflict=False, unresolved_identity=False, requires_detail=False)
        for field, value in [('summary', 'x'*6001), ('witness_candidate_ids', ['C1']*25),
                ('covered_input_ids', ['C1']*33), ('limitations', ['x'*501])]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                FixtureDigest.model_validate({**base, field:value})

    def test_executor_rejects_full_inventory_before_credentials_or_transport(self):
        """Exercise the production executor's preflight, not only the receipt helper.

        Args:
            self: Test owner using a temporary saved run and forbidden credential hook.
        Returns:
            None; zero attempts are charged and the rejection receipt survives on disk.
        """
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from projectclarity.providers.structured_model_call_execution import invoke_structured
        from projectclarity.coverage.coverage_contracts import CoverageAssessment
        from tests.research.research_test_fixtures import example_session
        session = example_session()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / session.session_id).mkdir()
            answer, receipt = invoke_structured(session, root, call_id='coverage_reject',
                task='bounded_coverage', prompt_version='test',
                messages=payload_messages('Test', {'result_inventory': {'G1': {'items': []}}}),
                schema=CoverageAssessment, key_reader=lambda: self.fail('Must not read credentials'))
            self.assertIsNone(answer)
            self.assertEqual(receipt['error_code'], 'input_contract_full_inventory_forbidden')
            self.assertEqual(session.usage.requests_attempted, 0)
            saved = json.loads((root/session.session_id/'input_receipts/coverage_reject.json').read_text())
            self.assertIn('input_contract_full_inventory_forbidden', saved['errors'])
