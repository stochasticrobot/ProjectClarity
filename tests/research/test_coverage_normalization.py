"""Check exact coverage compression without teaching a fake model to judge truth.

These component tests use deliberately repetitive qualification data and distinct
names, quotes and verdicts. They exercise the actual wire serializer and decoder,
not a tokenizer or live model. Production integration is tested separately by the
contract-completion cases; a private saved-run replay lives in evaluation.
"""
from copy import deepcopy
import json
import unittest

from projectclarity.coverage.payload_normalization import (
    ENCODING, FORMAT, TABLE, PREFIX, normalize_qualification_payload,
    expand_qualification_payload, prepare_coverage_messages, json_key)
from projectclarity.discovery.inventory_context import payload_messages


def repeated_payload(count=337):
    """Create large report entries sharing context but never sharing inferred identities.

    Args:
        count: Number of distinct rows, each retaining its own evidence/decision IDs.
    Returns:
        Synthetic coverage JSON with exact duplicates and important near-duplicates.
        Repetition models transport overhead, not factual research or model accuracy.
    """
    context = {'url': 'https://example.org/roster', 'title': 'Current roster',
               'scope': 'The source describes one specific population, not every organization. ' * 14}
    rows = []
    for index in range(count):
        name = ['John K Smith', 'John Kenneth Smith', 'John K. Smith'][index % 3]
        rows.append(dict(result_id=f'R{index}', fields={'name': name},
            candidate_ids=[f'C{index}'], status='pending', qualification_requirements=[{
                'requirement_id': 'G1_scope', 'description': 'Check population scope. ' * 20,
                'status': 'conflicting', 'assessments': [
                    {'candidate_id': f'C{index}', 'verdict': 'met', 'context': context,
                     'quote': f'{name} was a member in 2020.', 'record_index': index},
                    {'candidate_id': f'C{index}_other', 'verdict': 'not_met', 'context': context,
                     'quote': f'{name} is not a current member.', 'record_index': index}]}]))
    return dict(result_inventory={'G1': {'items': rows}},
                candidates=[{'candidate_id': f'C{i}'} for i in range(count)],
                acquired_work=[{'source_id': 'S1', 'unread_passages': 15}])


class CoverageNormalizationTests(unittest.TestCase):
    """Prove losslessness, bounded transport selection and fail-closed references."""

    def test_realistic_population_fits_without_losing_names_or_conflicts(self):
        """Shrink repeated qualification context while retaining every original JSON value.

        Args:
            self: Test owner using an independently counted 337-row population.
        Returns:
            None. Assert exact expansion, unchanged inputs and all distinct rows;
            this is not an LLM identity or currentness judgment.
        """
        payload = repeated_payload()
        before = deepcopy(payload)
        messages, receipt = prepare_coverage_messages('Coverage instructions', payload, 512_000)
        self.assertGreater(receipt['original_characters'], 512_000)
        self.assertLess(receipt['effective_characters'], 512_000)
        wire = json.loads(messages[1][1])
        self.assertEqual(receipt['encoding'], ENCODING)
        self.assertEqual(expand_qualification_payload(wire), before)
        self.assertEqual(payload, before)
        self.assertEqual(len(wire['result_inventory']['G1']['items']), 337)
        self.assertEqual([r['fields'] for r in wire['result_inventory']['G1']['items']],
                         [r['fields'] for r in before['result_inventory']['G1']['items']])
        self.assertEqual(receipt['effective_characters'], len(json.dumps(messages, ensure_ascii=False)))
        self.assertEqual(messages[0], ('system', 'Coverage instructions'))

    def test_exact_values_not_approximate_matching_or_type_coercion(self):
        """Preserve punctuation, Unicode, whitespace, numbers and conflicting values.

        Args:
            self: Test owner adding adversarial near-duplicates to qualification data.
        Returns:
            None. Exact canonical JSON and list order survive normalization/reordering.
        """
        payload = repeated_payload(12)
        variants = ['é', 'e\u0301', 'A B', 'A  B', 'A B ', 'a b', True, 1, 1.0, None, []]
        for index, row in enumerate(payload['result_inventory']['G1']['items']):
            row['qualification_requirements'][0]['variants'] = variants[index % len(variants)]
        for order in (payload['result_inventory']['G1']['items'],
                      list(reversed(payload['result_inventory']['G1']['items']))):
            payload['result_inventory']['G1']['items'] = order
            encoded = normalize_qualification_payload(payload)
            self.assertEqual(json_key(expand_qualification_payload(encoded)), json_key(payload))
            self.assertEqual(encoded, normalize_qualification_payload(deepcopy(payload)))

    def test_small_and_uncompressible_requests_keep_original_envelope(self):
        """Do not enlarge requests or use references when inline input already fits.

        Args:
            self: Test owner comparing exact serializer output for both fallback cases.
        Returns:
            None. No inferred reduction or changed prompt is accepted.
        """
        for payload, cap in [(repeated_payload(2), 512_000), ({'candidates': ['unique' * 500]}, 100)]:
            messages, receipt = prepare_coverage_messages('Verbatim prompt', payload, cap)
            self.assertEqual(messages, payload_messages('Verbatim prompt', payload))
            self.assertEqual(receipt['encoding'], 'inline')

    def test_reserved_literals_cannot_be_misread_as_generated_references(self):
        """Fall back rather than reinterpret pre-existing reserved data structures.

        Args:
            self: Test owner placing reference-like source data at encoding boundaries.
        Returns:
            None. Inputs remain byte-value equivalent even when normalization is unsafe.
        """
        for key in (TABLE, FORMAT):
            payload = repeated_payload(3)
            payload[key] = 'source-owned value'
            self.assertEqual(normalize_qualification_payload(payload), payload)
        payload = repeated_payload(3)
        payload['result_inventory']['G1']['items'][0]['qualification_requirements'].append(
            {'$ref': '#/not-an-application-reference'})
        self.assertEqual(normalize_qualification_payload(payload), payload)

    def test_missing_external_and_unknown_encoding_references_fail(self):
        """Reject corrupt inspection references rather than invent or fetch their values.

        Args:
            self: Test owner corrupting only copied encoded data.
        Returns:
            None. Decoder raises ValueError for unavailable or unsupported references.
        """
        encoded = normalize_qualification_payload(repeated_payload(3))
        for ref in (PREFIX + 'missing', 'https://example.org/value', '#/another-table/Q1'):
            broken = deepcopy(encoded)
            broken['result_inventory']['G1']['items'][0]['qualification_requirements'] = {'$ref': ref}
            with self.assertRaisesRegex(ValueError, 'invalid_coverage_reference'):
                expand_qualification_payload(broken)
        broken = deepcopy(encoded)
        broken[FORMAT] = 'future-encoding'
        with self.assertRaisesRegex(ValueError, 'unknown_coverage_payload_encoding'):
            expand_qualification_payload(broken)

    def test_changed_evidence_changes_wire_hash_without_reusing_old_values(self):
        """Bind encoding receipts to the actual evidence rather than names or row counts.

        Args:
            self: Test owner changing a verdict and quote after first normalization.
        Returns:
            None. Both source and wire hashes change; expanded new evidence is exact.
        """
        payload = repeated_payload(8)
        _, first = prepare_coverage_messages('Instructions', payload, 100)
        decision = payload['result_inventory']['G1']['items'][0]['qualification_requirements'][0]['assessments'][0]
        decision.update(verdict='unknown', quote='Evidence withdrawn, not confirmed.')
        messages, second = prepare_coverage_messages('Instructions', payload, 100)
        self.assertNotEqual(first['original_payload_sha256'], second['original_payload_sha256'])
        self.assertNotEqual(first['wire_payload_sha256'], second['wire_payload_sha256'])
        self.assertEqual(expand_qualification_payload(json.loads(messages[1][1])), payload)
